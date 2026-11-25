import AVFoundation
import Foundation

@MainActor
final class AudioStreamPlayer {
    private let engine = AVAudioEngine()
    private let playerNode = AVAudioPlayerNode()
    private var currentFormat: AVAudioFormat?
    private var isConfigured = false

    func startStream(sampleRate: Double, channels: AVAudioChannelCount) {
        stop()
        let sr = max(8000, sampleRate)
        let ch = max(1, channels)
        guard let fmt = AVAudioFormat(
            commonFormat: .pcmFormatFloat32,
            sampleRate: sr,
            channels: ch,
            interleaved: false
        ) else {
            return
        }

        if !isConfigured {
            engine.attach(playerNode)
            isConfigured = true
        } else {
            engine.disconnectNodeOutput(playerNode)
        }

        engine.connect(playerNode, to: engine.mainMixerNode, format: fmt)
        currentFormat = fmt

        do {
            if !engine.isRunning {
                try engine.start()
            }
            if !playerNode.isPlaying {
                playerNode.play()
            }
        } catch {
            NSLog("AudioStreamPlayer failed to start AVAudioEngine: \(error)")
        }
    }

    func enqueuePCM16LE(_ data: Data) {
        guard let fmt = currentFormat, !data.isEmpty else { return }
        let sampleCount = data.count / 2
        guard sampleCount > 0,
              let buffer = AVAudioPCMBuffer(pcmFormat: fmt, frameCapacity: AVAudioFrameCount(sampleCount))
        else { return }
        buffer.frameLength = AVAudioFrameCount(sampleCount)
        guard let channelData = buffer.floatChannelData?[0] else { return }

        data.withUnsafeBytes { rawBuf in
            let int16Ptr = rawBuf.bindMemory(to: Int16.self)
            for i in 0..<sampleCount {
                let sample = Int16(littleEndian: int16Ptr[i])
                channelData[i] = Float(sample) / 32768.0
            }
        }
        playerNode.scheduleBuffer(buffer, completionHandler: nil)
        if !playerNode.isPlaying {
            playerNode.play()
        }
    }

    func finishStream() {
        // Buffers already scheduled continue playing naturally.
    }

    func stop() {
        if playerNode.isPlaying {
            playerNode.stop()
        }
        if engine.isRunning {
            engine.stop()
        }
        currentFormat = nil
    }
}
