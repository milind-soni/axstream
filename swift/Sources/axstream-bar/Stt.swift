// Local speech-to-text via FluidAudio's Parakeet realtime EOU model
// (parakeet-realtime-eou-120m, CoreML / Neural Engine) in TRUE streaming mode:
// StreamingEouAsrManager keeps encoder caches across 320ms chunks, decodes
// incrementally, emits partial transcripts as tokens land, and raises an
// in-band end-of-utterance signal after `eouDebounceMs` of sustained silence.
//
// Concurrency contract: StreamingEouAsrManager is an actor, but audio chunks
// must arrive in order. The mic tap yields into an AsyncStream and a single
// pump task consumes it, so `process` calls are strictly FIFO. Consecutive
// utterances are chained (`chain`) so a new hold can never interleave with the
// previous hold's drain/finish.

import AVFoundation
import FluidAudio
import Foundation

actor Stt {
    /// Sustained-silence window before the model's EOU prediction is confirmed.
    /// Low on purpose (default is 1280ms): with 320ms chunks the callback fires
    /// on the first silent chunk after the debounce window elapses.
    static let eouDebounceMs = 300

    private var manager: StreamingEouAsrManager?
    private var feed: AsyncStream<[Float]>.Continuation?
    private var chain: Task<String, Never>?

    var isReady: Bool { manager != nil }

    /// Download + load the Parakeet EOU streaming models once, then burn the
    /// first-call CoreML compile with a short silent stream.
    func prepare(onProgress: @escaping @Sendable (String) -> Void) async throws {
        guard manager == nil else { return }
        onProgress("downloading STT model…")
        let asr = StreamingEouAsrManager(chunkSize: .ms320, eouDebounceMs: Self.eouDebounceMs)
        try await asr.loadModels(to: nil, configuration: nil) { progress in
            let pct = Int(progress.fractionCompleted * 100)
            onProgress("downloading STT model… \(pct)%")
        }
        onProgress("warming STT…")
        _ = try? await asr.process(audioBuffer: Self.pcmBuffer(from: [Float](repeating: 0, count: 16_000)))
        _ = try? await asr.finish()
        await asr.reset()
        manager = asr
    }

    /// Start a fresh streaming utterance. Returns a thread-safe sink for
    /// converted 16 kHz mono chunks (call it straight from the mic tap).
    /// `onPartial` fires whenever new tokens are decoded (~every 320ms chunk
    /// that contains speech); `onEou` fires once per utterance when the model
    /// signals end-of-utterance and the debounce window has elapsed.
    func beginUtterance(
        onPartial: @escaping @Sendable (String) -> Void,
        onEou: @escaping @Sendable (String) -> Void
    ) -> @Sendable ([Float]) -> Void {
        feed?.finish()  // orphan any stale feed (missed talkStop)
        let (stream, continuation) = AsyncStream<[Float]>.makeStream()
        feed = continuation

        let previous = chain
        let manager = manager
        chain = Task { () -> String in
            _ = await previous?.value  // wait out the prior utterance, fully drained
            guard let manager else { return "" }
            await manager.reset()  // fresh caches + decoder state + EOU latch per hold
            await manager.setPartialCallback(onPartial)
            await manager.setEouCallback(onEou)
            for await chunk in stream {
                _ = try? await manager.process(audioBuffer: Self.pcmBuffer(from: chunk))
            }
            // Feed closed (key released): pad + decode the tail, take the final text.
            let text = (try? await manager.finish()) ?? ""
            return text.trimmingCharacters(in: .whitespacesAndNewlines)
        }
        return { continuation.yield($0) }
    }

    /// Close the feed, drain remaining audio through the decoder, and return
    /// the final transcript for the utterance.
    func finishUtterance() async -> String {
        feed?.finish()
        feed = nil
        return await chain?.value ?? ""
    }

    /// Wrap converted samples in the 16 kHz mono Float32 buffer the manager
    /// expects (its AudioConverter fast-paths this format untouched).
    private static func pcmBuffer(from samples: [Float]) -> AVAudioPCMBuffer {
        let format = AVAudioFormat(
            commonFormat: .pcmFormatFloat32, sampleRate: sttSampleRate,
            channels: 1, interleaved: false
        )!
        let buffer = AVAudioPCMBuffer(
            pcmFormat: format, frameCapacity: AVAudioFrameCount(max(samples.count, 1))
        )!
        buffer.frameLength = AVAudioFrameCount(samples.count)
        if let channel = buffer.floatChannelData, !samples.isEmpty {
            samples.withUnsafeBufferPointer { source in
                channel[0].update(from: source.baseAddress!, count: samples.count)
            }
        }
        return buffer
    }
}
