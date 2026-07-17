// Microphone capture: AVAudioEngine input tap converted to 16 kHz mono
// Float32 and accumulated while the push-to-talk keys are held.

import AVFoundation
import Foundation

let sttSampleRate: Double = 16_000

final class Mic {
    private let engine = AVAudioEngine()
    private var converter: AVAudioConverter?
    private let lock = NSLock()
    private var samples: [Float] = []
    private var running = false

    func start() throws {
        guard !running else { return }
        lock.lock()
        samples = []
        lock.unlock()

        let input = engine.inputNode
        let hardwareFormat = input.outputFormat(forBus: 0)
        guard hardwareFormat.sampleRate > 0 else {
            throw NSError(domain: "Mic", code: 1, userInfo: [
                NSLocalizedDescriptionKey: "no input device (or mic permission denied)",
            ])
        }
        guard let targetFormat = AVAudioFormat(
            commonFormat: .pcmFormatFloat32, sampleRate: sttSampleRate,
            channels: 1, interleaved: false
        ) else {
            throw NSError(domain: "Mic", code: 2, userInfo: [
                NSLocalizedDescriptionKey: "could not build 16kHz mono format",
            ])
        }
        converter = AVAudioConverter(from: hardwareFormat, to: targetFormat)

        input.installTap(onBus: 0, bufferSize: 2048, format: hardwareFormat) { [weak self] buffer, _ in
            self?.append(buffer, targetFormat: targetFormat)
        }
        engine.prepare()
        try engine.start()
        running = true
    }

    /// Stop capture and return everything recorded during the hold.
    func stop() -> [Float] {
        if running {
            engine.inputNode.removeTap(onBus: 0)
            engine.stop()
            running = false
        }
        lock.lock()
        defer { lock.unlock() }
        return samples
    }

    /// Snapshot of the accumulated buffer (for the 600ms partial loop).
    func snapshot() -> [Float] {
        lock.lock()
        defer { lock.unlock() }
        return samples
    }

    private func append(_ buffer: AVAudioPCMBuffer, targetFormat: AVAudioFormat) {
        guard let converter else { return }
        let ratio = targetFormat.sampleRate / buffer.format.sampleRate
        let capacity = AVAudioFrameCount(Double(buffer.frameLength) * ratio) + 64
        guard let converted = AVAudioPCMBuffer(pcmFormat: targetFormat, frameCapacity: capacity)
        else { return }

        var consumed = false
        var error: NSError?
        let status = converter.convert(to: converted, error: &error) { _, outStatus in
            if consumed {
                outStatus.pointee = .noDataNow
                return nil
            }
            consumed = true
            outStatus.pointee = .haveData
            return buffer
        }
        guard status != .error, error == nil,
              let channel = converted.floatChannelData, converted.frameLength > 0
        else { return }

        let chunk = Array(UnsafeBufferPointer(start: channel[0], count: Int(converted.frameLength)))
        lock.lock()
        samples.append(contentsOf: chunk)
        lock.unlock()
    }
}
