// axstream-bar: voice-driven computer use in a single process.
// Hold ⌃⌥ to speak; Parakeet transcribes live; a stable partial transcript
// starts acting mid-speech; Groq streams JSONL actions that execute through
// cua-driver as the lines complete. Port of axstream's Python pipeline.

import AppKit
import Foundation

// MARK: - Voice session (port of bridge.py's Bridge)

/// Compare transcripts ignoring case, punctuation, and spacing.
func normalizedTranscript(_ text: String) -> String {
    String(text.lowercased().unicodeScalars.filter { CharacterSet.alphanumerics.contains($0) })
}

@MainActor
final class VoiceSession {
    private let bar: Bar
    private let driver = Driver()
    private let stt = Stt()
    private let mic = Mic()

    private var partialTask: Task<Void, Never>?
    private var runTask: Task<Void, Never>?
    private var runGeneration = 0
    private var spokenText = ""  // transcript continuous mode is already acting on
    private var recording = false
    private var ready = false

    private static let partialInterval: UInt64 = 600_000_000  // 600ms
    private static let taskTimeout: TimeInterval = 90

    init(bar: Bar) {
        self.bar = bar
    }

    func prepare() async {
        do {
            bar.setTranscript("starting cua-driver…", partial: true)
            try await driver.start()
            try await stt.prepare { [bar] message in
                Task { @MainActor in bar.setTranscript(message, partial: true) }
            }
            ready = true
            bar.setStatus(.idle)
            bar.setTranscript("hold ⌃⌥ and speak", partial: true)
        } catch {
            bar.setStatus(.idle)
            bar.setTranscript("startup failed: \(error)", partial: false)
        }
    }

    // MARK: push-to-talk

    func talkStart() {
        guard ready, !recording else { return }
        do {
            try mic.start()
        } catch {
            bar.setTranscript("mic error: \(error.localizedDescription)", partial: false)
            return
        }
        recording = true
        spokenText = ""
        bar.setStatus(.listening)
        bar.setTranscript("listening…", partial: true)
        partialTask = Task { [weak self] in await self?.partialLoop() }
    }

    func talkStop() {
        guard recording else { return }
        recording = false
        partialTask?.cancel()
        partialTask = nil
        let audio = mic.stop()
        bar.setStatus(.thinking)

        Task { [weak self] in
            guard let self else { return }
            let t0 = Date()
            let text = (try? await self.stt.transcribe(audio)) ?? ""
            let sttMs = Date().timeIntervalSince(t0) * 1000
            self.bar.setTranscript(text.isEmpty ? "(heard nothing)" : text, partial: false)
            self.bar.setTiming(String(format: "stt %.0fms", sttMs))

            if !text.isEmpty,
               normalizedTranscript(text) == normalizedTranscript(self.spokenText) {
                // continuous mode already acting on exactly this command
            } else if !text.isEmpty {
                // latest voice command wins: cancel any running task
                self.startRun(text)
            } else if self.runTask == nil {
                self.bar.setStatus(.idle)
            }
        }
    }

    /// 600ms live re-transcription loop with the stability rule from
    /// bridge.py: same partial twice in a row + >=3 words -> eager start.
    private func partialLoop() async {
        var last = ""
        var stableCount = 0
        while !Task.isCancelled {
            try? await Task.sleep(nanoseconds: Self.partialInterval)
            if Task.isCancelled { return }
            if await stt.inFlight > 0 { continue }  // never queue behind an in-flight transcribe
            var audio = mic.snapshot()
            if audio.count < Int(sttSampleRate) / 4 { continue }
            audio = Array(audio.suffix(Int(sttSampleRate) * 10))  // cap partial cost on long holds
            guard let text = try? await stt.transcribe(audio), !text.isEmpty else { continue }
            if Task.isCancelled { return }
            if text != last {
                last = text
                stableCount = 0
                bar.setTranscript(text, partial: true)
                continue
            }
            // The transcript stopped changing while the user is still holding
            // the key -- start acting on it now. talkStop reconciles with the
            // final transcript (continue if equal, cancel + rerun if it grew).
            stableCount += 1
            if stableCount == 2,
               text.split(whereSeparator: { $0.isWhitespace }).count >= 3,
               normalizedTranscript(text) != normalizedTranscript(spokenText) {
                spokenText = text
                bar.setTranscript(text + " ⚡", partial: true)
                startRun(text)
            }
        }
    }

    // MARK: task running

    private func startRun(_ task: String) {
        runTask?.cancel()
        runGeneration += 1
        let generation = runGeneration
        bar.clearChips()
        bar.setStatus(.acting)

        runTask = Task { [weak self] in
            guard let self else { return }
            let t0 = Date()
            let bar = self.bar
            let onEvent: @Sendable (RunnerEvent) -> Void = { event in
                Task { @MainActor in
                    guard generation == self.runGeneration else { return }
                    switch event {
                    case .executed(let op, let ms):
                        bar.addChip(opString(op))
                        bar.setTiming(String(format: "%.1fs · %.0fms", Date().timeIntervalSince(t0), ms))
                    case .actionFailed(let op, _):
                        bar.addChip("✗ " + opString(op))
                    case .done(let status, _):
                        bar.addChip(status == "success" ? "✓ done" : "✗ \(status)")
                    case .aborted(let reason):
                        bar.addChip("✗ " + String(reason.prefix(40)))
                    case .narration, .invalidLine, .burstStart, .observeRequested:
                        break
                    }
                }
            }
            do {
                let driver = self.driver
                try await withTimeout(seconds: Self.taskTimeout) {
                    try await Runner.run(driver: driver, task: task, onEvent: onEvent)
                }
            } catch is CancellationError {
                // interrupted by a newer command, timed out, or wedged: move on
            } catch {
                let message = String(describing: error)
                if generation == self.runGeneration {
                    bar.setTranscript("error: \(message.prefix(80))", partial: false)
                }
            }
            guard generation == self.runGeneration else { return }
            bar.setTiming(String(format: "%.1fs", Date().timeIntervalSince(t0)))
            if !self.recording { bar.setStatus(.idle) }
            self.runTask = nil
        }
    }
}

// MARK: - App bootstrap

final class AppDelegate: NSObject, NSApplicationDelegate {
    private var bar: Bar!
    private var session: VoiceSession!
    private let hotkey = Hotkey()

    func applicationDidFinishLaunching(_ notification: Notification) {
        Env.load()
        bar = Bar()
        session = VoiceSession(bar: bar)
        let session = self.session!
        hotkey.onTalkStart = { Task { @MainActor in session.talkStart() } }
        hotkey.onTalkStop = { Task { @MainActor in session.talkStop() } }
        hotkey.install()
        Task { @MainActor in await session.prepare() }
    }
}

let app = NSApplication.shared
app.setActivationPolicy(.accessory)
let delegate = AppDelegate()
app.delegate = delegate
app.run()
