// axstream-bar: voice-driven computer use in a single process.
// Hold ⌃⌥ to speak; Parakeet streams the transcript live (partial callbacks
// per 320ms chunk); an in-band end-of-utterance signal starts acting
// mid-speech; Groq streams JSONL actions that execute through cua-driver as
// the lines complete. Port of axstream's Python pipeline.

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

    private var runTask: Task<Void, Never>?
    private var runGeneration = 0
    private var utteranceGeneration = 0  // guards stale partial/EOU callbacks
    private var spokenText = ""  // transcript continuous mode is already acting on
    private var recording = false
    private var ready = false

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
        utteranceGeneration += 1
        let generation = utteranceGeneration
        bar.setStatus(.listening)
        bar.setTranscript("listening…", partial: true)

        // Open the streaming utterance and wire the mic tap straight into it.
        // Chunks captured before the sink lands are flushed by Mic.setSink.
        Task { [weak self] in
            guard let self else { return }
            let sink = await self.stt.beginUtterance(
                onPartial: { text in
                    Task { @MainActor in self.handlePartial(text, generation: generation) }
                },
                onEou: { text in
                    Task { @MainActor in self.handleEou(text, generation: generation) }
                }
            )
            self.mic.setSink(sink)
        }
    }

    func talkStop() {
        guard recording else { return }
        recording = false
        _ = mic.stop()
        bar.setStatus(.thinking)

        Task { [weak self] in
            guard let self else { return }
            let t0 = Date()
            // Close the feed, drain the decoder, take the final transcript.
            let text = await self.stt.finishUtterance()
            let sttMs = Date().timeIntervalSince(t0) * 1000
            self.bar.setTranscript(text.isEmpty ? "(heard nothing)" : text, partial: false)
            self.bar.setTiming(String(format: "stt flush %.0fms", sttMs))

            if !text.isEmpty,
               normalizedTranscript(text) == normalizedTranscript(self.spokenText) {
                // eager start (EOU) already acting on exactly this command
            } else if !text.isEmpty {
                // latest voice command wins: cancel any running task
                self.startRun(text)
            } else if self.runTask == nil {
                self.bar.setStatus(.idle)
            }
        }
    }

    /// Streaming partial (new tokens decoded): live-update the bar.
    private func handlePartial(_ text: String, generation: Int) {
        guard generation == utteranceGeneration, recording, !text.isEmpty else { return }
        bar.setTranscript(text, partial: true)
    }

    /// In-band end-of-utterance while the user is still holding the key:
    /// the model says the command is complete, so start acting on it now.
    /// talkStop reconciles with the final transcript (continue if equal,
    /// cancel + rerun if the user kept talking).
    private func handleEou(_ text: String, generation: Int) {
        guard generation == utteranceGeneration, recording else { return }
        let text = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard text.split(whereSeparator: { $0.isWhitespace }).count >= 3,
              normalizedTranscript(text) != normalizedTranscript(spokenText)
        else { return }
        spokenText = text
        bar.setTranscript(text + " ⚡", partial: true)
        startRun(text)
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
