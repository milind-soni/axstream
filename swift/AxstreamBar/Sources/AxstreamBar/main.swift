import AppKit
import ApplicationServices
import AVFoundation
import Darwin

// line-buffer stdout so timing logs reach bar.log while detached
setvbuf(stdout, nil, _IOLBF, 0)

// Headless test modes (no mic, no menu bar):
//   axstream-bar --transcribe <audio-file>   print whisper's transcription
//   axstream-bar --pipeline <audio-file>     transcribe -> match -> print hit
//   axstream-bar --match "<utterance>"       match typed text -> print hit
func loadAudio(_ path: String) -> [Float]? {
    guard let file = try? AVAudioFile(forReading: URL(fileURLWithPath: path)) else {
        return nil
    }
    let inFormat = file.processingFormat
    guard let buffer = AVAudioPCMBuffer(pcmFormat: inFormat,
                                        frameCapacity: AVAudioFrameCount(file.length)),
          (try? file.read(into: buffer)) != nil else { return nil }
    let outFormat = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: 16_000,
                                  channels: 1, interleaved: false)!
    guard let converter = AVAudioConverter(from: inFormat, to: outFormat) else { return nil }
    let capacity = AVAudioFrameCount(
        Double(buffer.frameLength) * 16_000 / inFormat.sampleRate) + 64
    guard let out = AVAudioPCMBuffer(pcmFormat: outFormat, frameCapacity: capacity)
    else { return nil }
    var fed = false
    converter.convert(to: out, error: nil) { _, status in
        if fed { status.pointee = .endOfStream; return nil }
        fed = true
        status.pointee = .haveData
        return buffer
    }
    guard let channel = out.floatChannelData else { return nil }
    return Array(UnsafeBufferPointer(start: channel[0], count: Int(out.frameLength)))
}

func import_status() {
    let mic: String
    switch AVCaptureDevice.authorizationStatus(for: .audio) {
    case .authorized: mic = "granted"
    case .denied: mic = "DENIED — System Settings → Privacy & Security → Microphone"
    case .restricted: mic = "restricted"
    case .notDetermined: mic = "not requested yet (launch the app once)"
    @unknown default: mic = "unknown"
    }
    let trusted = AXIsProcessTrusted() ? "granted"
        : "DENIED — System Settings → Privacy & Security → Accessibility"
    print("accessibility (⌃⌥ hotkey): \(trusted)")
    print("microphone:                \(mic)")
    print("whisper model:             "
          + (FileManager.default.fileExists(atPath: Whisper.defaultModelPath)
             ? "ok (\(Whisper.defaultModelPath))" : "MISSING"))
    let matcher = Matcher()
    print("matcher (\(matcher.baseURL)): \(matcher.available() ? "ok" : "OFFLINE")")
    print("macros:                    \(MacroLibrary.load().count)")
}

func matchAndPrint(_ text: String) {
    let workflows = MacroLibrary.voiceWorkflows()
    let matcher = Matcher()
    guard matcher.available() else {
        print("matcher offline (\(matcher.baseURL))")
        exit(2)
    }
    let t0 = Date()
    let candidates = Prerank().shortlist(text, from: workflows)
        ?? Array(workflows.prefix(25))
    let hit = matcher.match(text, templates: candidates.map(MatchTemplate.from))
    let ms = Int(Date().timeIntervalSince(t0) * 1000)
    if let hit {
        print("match: \(hit.template) slots=\(hit.slots) (\(ms)ms, cands=\(candidates.count))")
    } else {
        print("match: none (\(ms)ms, cands=\(candidates.count))")
    }
}

let arguments = CommandLine.arguments
if arguments.count >= 2, arguments[1] == "--status" {
    import_status()
    exit(0)
}
if arguments.count >= 3 {
    switch arguments[1] {
    case "--transcribe", "--pipeline":
        guard let samples = loadAudio(arguments[2]) else {
            print("cannot read audio: \(arguments[2])")
            exit(2)
        }
        var whisper = Whisper()
        guard whisper != nil else {
            print("cannot load model: \(Whisper.defaultModelPath)")
            exit(2)
        }
        let t0 = Date()
        let text = whisper!.transcribe(samples)
        let ms = Int(Date().timeIntervalSince(t0) * 1000)
        print("heard: \"\(text)\" (\(ms)ms, \(samples.count / 16_000)s audio)")
        if arguments[1] == "--pipeline", !text.isEmpty {
            matchAndPrint(text)
        }
        // free the Metal context before exit — ggml's atexit teardown
        // asserts if a device still holds residency sets
        whisper = nil
        exit(0)
    case "--match":
        matchAndPrint(arguments[2])
        exit(0)
    case "--say":
        // full text path incl. execution: match -> replay, else engine fallback
        let text = arguments[2].lowercased()
        let workflows = MacroLibrary.voiceWorkflows()
        let candidates = Prerank().shortlist(text, from: workflows)
            ?? Array(workflows.prefix(25))
        let templates = candidates.map(MatchTemplate.from)
        if let hit = Matcher().match(text, templates: templates),
           let workflow = workflows.first(where: { $0.name == hit.template }) {
            print("macro \(hit.template) \(hit.slots)")
            let outcome = Replay.run(workflow: workflow, slots: hit.slots)
            print(String(format: "%@ in %.1fs %@", outcome.ok ? "ok" : "FAILED",
                         outcome.seconds, outcome.detail))
        } else {
            print("no macro -> engine fallback")
            let outcome = Replay.utterance(text)
            print(String(format: "%@ in %.1fs %@", outcome.ok ? "ok" : "FAILED",
                         outcome.seconds, outcome.detail))
        }
        exit(0)
    case "--plan":
        // resolve a (possibly compound) utterance to its workflow chain
        // without executing anything
        let text = arguments[2].lowercased()
        let workflows = MacroLibrary.voiceWorkflows()
        let matcher = Matcher()
        let prerank = Prerank()
        let clauses = BarController.splitClauses(text)
        print("clauses: \(clauses)")
        for clause in clauses {
            let candidates = prerank.shortlist(clause, from: workflows)
                ?? Array(workflows.prefix(25))
            if let hit = matcher.match(clause,
                                       templates: candidates.map(MatchTemplate.from)) {
                print("  \(clause)  ->  \(hit.template) \(hit.slots)")
            } else {
                print("  \(clause)  ->  none (chain would fall back)")
            }
        }
        exit(0)
    case "--prompt":
        let templates = MacroLibrary.voiceWorkflows().prefix(25).map(MatchTemplate.from)
        try? Matcher().buildPrompt(Array(templates))
            .write(toFile: arguments[2], atomically: true, encoding: .utf8)
        print(templates.map(\.id).prefix(6))
        exit(0)
    default:
        break
    }
}

let app = NSApplication.shared
let controller = BarController()
app.delegate = controller
app.setActivationPolicy(.accessory)
app.run()
