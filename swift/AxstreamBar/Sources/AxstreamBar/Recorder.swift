import AVFoundation

// Mic capture: opens on key-down, closes on key-up. Converts the device
// format to whisper's 16kHz mono float on the fly.
final class Recorder {
    // Recreated on every start: a cached engine binds to the input device it
    // was created with, and plugging in a new mic (default-input change)
    // leaves it reporting a dead 0Hz format that can never start.
    private var engine = AVAudioEngine()
    private var samples: [Float] = []
    private let lock = NSLock()
    private let outFormat = AVAudioFormat(commonFormat: .pcmFormatFloat32,
                                          sampleRate: 16_000, channels: 1,
                                          interleaved: false)!

    func start() throws {
        lock.lock(); samples.removeAll(); lock.unlock()
        engine = AVAudioEngine()  // bind to the CURRENT default input
        let input = engine.inputNode
        let inFormat = input.outputFormat(forBus: 0)
        guard inFormat.sampleRate > 0, inFormat.channelCount > 0,
              let converter = AVAudioConverter(from: inFormat, to: outFormat) else {
            throw NSError(domain: "axstream", code: 1, userInfo: [
                NSLocalizedDescriptionKey:
                    "mic format unavailable (\(inFormat.sampleRate)Hz/"
                    + "\(inFormat.channelCount)ch) — input device changed?"])
        }
        input.installTap(onBus: 0, bufferSize: 2048, format: inFormat) { [weak self] buffer, _ in
            self?.append(buffer, converter: converter)
        }
        engine.prepare()
        try engine.start()
    }

    private func append(_ buffer: AVAudioPCMBuffer, converter: AVAudioConverter) {
        let ratio = outFormat.sampleRate / buffer.format.sampleRate
        let capacity = AVAudioFrameCount(Double(buffer.frameLength) * ratio) + 16
        guard let out = AVAudioPCMBuffer(pcmFormat: outFormat, frameCapacity: capacity)
        else { return }
        var fed = false
        converter.convert(to: out, error: nil) { _, status in
            if fed { status.pointee = .noDataNow; return nil }
            fed = true
            status.pointee = .haveData
            return buffer
        }
        guard out.frameLength > 0, let channel = out.floatChannelData else { return }
        let chunk = Array(UnsafeBufferPointer(start: channel[0],
                                              count: Int(out.frameLength)))
        lock.lock(); samples.append(contentsOf: chunk); lock.unlock()
    }

    func stop() -> [Float] {
        engine.inputNode.removeTap(onBus: 0)
        engine.stop()
        lock.lock(); defer { lock.unlock() }
        return samples
    }
}
