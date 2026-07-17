// Local speech-to-text via FluidAudio's Parakeet (CoreML / Neural Engine).
// Follows the same flow as BlueyLite's ParakeetTranscriptionBackend:
// AsrModels.downloadAndLoad -> AsrManager.loadModels -> transcribe.
//
// Parakeet is NOT safe under concurrent transcribe calls, so every call is
// chained behind the previous one (hard FIFO serialization -- an actor alone
// is not enough because actor methods interleave at suspension points).
// The partial loop additionally checks `inFlight` and skips a cycle instead
// of queueing up behind a slow transcribe, mirroring bridge.py.

import FluidAudio
import Foundation

actor Stt {
    private var manager: AsrManager?
    private var chain: Task<String, Error>?
    private(set) var inFlight = 0

    var isReady: Bool { manager != nil }

    /// Download + load the Parakeet models once, then burn the first-call
    /// compile with a short silent buffer.
    func prepare(onProgress: @escaping @Sendable (String) -> Void) async throws {
        guard manager == nil else { return }
        onProgress("downloading STT model…")
        let models = try await AsrModels.downloadAndLoad(version: .v3) { progress in
            let pct = Int(progress.fractionCompleted * 100)
            onProgress("downloading STT model… \(pct)%")
        }
        onProgress("loading STT model…")
        let asr = AsrManager(config: .default)
        try await asr.loadModels(models)
        manager = asr
        onProgress("warming STT…")
        _ = try? await run(samples: [Float](repeating: 0, count: 8000))
    }

    /// Transcribe 16 kHz mono Float32 samples. Serialized: begins only after
    /// every previously requested transcription has finished.
    func transcribe(_ samples: [Float]) async throws -> String {
        let previous = chain
        let task = Task { () throws -> String in
            _ = try? await previous?.value  // wait out the prior call, success or not
            return try await self.run(samples: samples)
        }
        chain = task
        inFlight += 1
        defer { inFlight -= 1 }
        return try await task.value
    }

    private func run(samples: [Float]) async throws -> String {
        guard let manager else {
            throw NSError(domain: "Stt", code: 1, userInfo: [
                NSLocalizedDescriptionKey: "STT not loaded yet",
            ])
        }
        // each hold is a self-contained utterance: fresh decoder state per call
        var decoderState = TdtDecoderState.make(decoderLayers: await manager.decoderLayerCount)
        let result = try await manager.transcribe(samples, decoderState: &decoderState)
        return result.text.trimmingCharacters(in: .whitespacesAndNewlines)
    }
}
