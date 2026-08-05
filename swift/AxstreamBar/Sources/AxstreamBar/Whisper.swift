import Foundation
import CWhisper

// Thin wrapper over whisper.cpp, model resident for the app's lifetime.
final class Whisper {
    private let ctx: OpaquePointer

    // AXSTREAM_WHISPER_MODEL overrides; otherwise the best model present
    // in ~/.axstream/models wins (small.en beats base.en on proper nouns —
    // "delhi" transcribed as "daily" cost real commands).
    static let defaultModelPath: String = {
        if let override = ProcessInfo.processInfo.environment["AXSTREAM_WHISPER_MODEL"] {
            return NSString(string: override).expandingTildeInPath
        }
        let dir = NSString(string: "~/.axstream/models").expandingTildeInPath
        // best-present wins: large-v3-turbo (q5) ≈ large accuracy on proper
        // nouns at ~600MB; small.en fallback; base.en last resort
        for name in ["ggml-large-v3-turbo-q5_0.bin", "ggml-small.en.bin",
                     "ggml-base.en.bin"] {
            let path = (dir as NSString).appendingPathComponent(name)
            if FileManager.default.fileExists(atPath: path) { return path }
        }
        return (dir as NSString).appendingPathComponent("ggml-base.en.bin")
    }()

    init?(modelPath: String = Whisper.defaultModelPath) {
        // Silence whisper/ggml logging — stdout is our timing log.
        whisper_log_set({ _, _, _ in }, nil)
        var params = whisper_context_default_params()
        params.use_gpu = true
        guard let ctx = whisper_init_from_file_with_params(modelPath, params) else {
            return nil
        }
        self.ctx = ctx
    }

    deinit { whisper_free(ctx) }

    /// samples: mono float32 @ 16kHz. Returns "" for silence/noise.
    func transcribe(_ samples: [Float]) -> String {
        // whisper wants ≥1s of audio and benefits from a quiet tail.
        var padded = samples
        let minCount = Int(16000 * 1.2)
        if padded.count < minCount {
            padded.append(contentsOf: [Float](repeating: 0, count: minCount - padded.count))
        } else {
            padded.append(contentsOf: [Float](repeating: 0, count: 3200))
        }
        var params = whisper_full_default_params(WHISPER_SAMPLING_GREEDY)
        params.print_progress = false
        params.print_realtime = false
        params.print_timestamps = false
        params.print_special = false
        params.no_timestamps = true
        params.single_segment = true
        params.suppress_blank = true
        params.n_threads = Int32(min(8, ProcessInfo.processInfo.activeProcessorCount))
        let rc = padded.withUnsafeBufferPointer { buffer in
            whisper_full(ctx, params, buffer.baseAddress, Int32(buffer.count))
        }
        guard rc == 0 else { return "" }
        var text = ""
        for i in 0..<whisper_full_n_segments(ctx) {
            text += String(cString: whisper_full_get_segment_text(ctx, i))
        }
        text = text.trimmingCharacters(in: .whitespacesAndNewlines)
        // whisper's silence/noise markers, e.g. [BLANK_AUDIO], (wind blowing)
        if text.hasPrefix("[") || text.hasPrefix("(") { return "" }
        if text.hasSuffix(".") { text = String(text.dropLast()) }
        // the matcher fine-tune was trained on lowercase voice transcripts;
        // whisper's capitalization is out-of-distribution and flips matches
        return text.lowercased()
    }
}
