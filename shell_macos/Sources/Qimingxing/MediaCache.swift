import AppKit
import AVFoundation
import ImageIO

/// Background thumbnail loading for Live Line and Workspace. No third-party
/// dependencies: ImageIO for stills, AVAssetImageGenerator for video frames.
@MainActor
final class MediaThumbnailCache {
    static let shared = MediaThumbnailCache()

    private let cache = NSCache<NSString, NSImage>()

    private init() {
        cache.countLimit = 120
        cache.totalCostLimit = 64 * 1024 * 1024
    }

    func thumbnail(for media: MediaItem, maxPixel: CGFloat = 320) async -> NSImage? {
        let key = "\(media.mediaId)-\(Int(maxPixel))" as NSString
        if let cached = cache.object(forKey: key) { return cached }
        guard let url = media.fileURL else { return nil }

        let cgImage: CGImage?
        if media.kind == "image" {
            cgImage = await Task.detached(priority: .utility) {
                Self.imageThumbnail(url: url, maxPixel: maxPixel)
            }.value
        } else if media.kind == "video" {
            cgImage = await Task.detached(priority: .utility) {
                Self.videoThumbnail(url: url, maxPixel: maxPixel)
            }.value
        } else {
            cgImage = nil
        }
        guard let cgImage else { return nil }
        let image = NSImage(cgImage: cgImage, size: NSSize(width: cgImage.width, height: cgImage.height))
        cache.setObject(image, forKey: key, cost: cgImage.width * cgImage.height * 4)
        return image
    }

    func synchronousThumbnail(for media: MediaItem, maxPixel: CGFloat = 320) -> NSImage? {
        let key = "\(media.mediaId)-\(Int(maxPixel))" as NSString
        if let cached = cache.object(forKey: key) { return cached }
        guard let url = media.fileURL else { return nil }
        let cgImage: CGImage?
        if media.kind == "image" {
            cgImage = Self.imageThumbnail(url: url, maxPixel: maxPixel)
        } else if media.kind == "video" {
            cgImage = Self.videoThumbnail(url: url, maxPixel: maxPixel)
        } else {
            cgImage = nil
        }
        guard let cgImage else { return nil }
        let image = NSImage(cgImage: cgImage, size: NSSize(width: cgImage.width, height: cgImage.height))
        cache.setObject(image, forKey: key, cost: cgImage.width * cgImage.height * 4)
        return image
    }

    nonisolated private static func imageThumbnail(url: URL, maxPixel: CGFloat) -> CGImage? {
        guard let source = CGImageSourceCreateWithURL(url as CFURL, nil) else { return nil }
        let options: [CFString: Any] = [
            kCGImageSourceCreateThumbnailFromImageAlways: true,
            kCGImageSourceCreateThumbnailWithTransform: true,
            kCGImageSourceThumbnailMaxPixelSize: maxPixel,
        ]
        return CGImageSourceCreateThumbnailAtIndex(source, 0, options as CFDictionary)
    }

    nonisolated private static func videoThumbnail(url: URL, maxPixel: CGFloat) -> CGImage? {
        let asset = AVURLAsset(url: url)
        let generator = AVAssetImageGenerator(asset: asset)
        generator.appliesPreferredTrackTransform = true
        generator.maximumSize = CGSize(width: maxPixel, height: maxPixel)
        return try? generator.copyCGImage(at: .zero, actualTime: nil)
    }
}

@MainActor
final class MediaAudioPlayer: ObservableObject {
    private static weak var activePlayer: MediaAudioPlayer?
    @Published private(set) var isPlaying = false
    @Published private(set) var progress: Double = 0
    @Published private(set) var duration: Double = 0
    @Published private(set) var loadedID = ""
    @Published private(set) var playbackRate: Float = 1.0

    private var player: AVPlayer?
    private var timeObserver: Any?
    private var endObserver: NSObjectProtocol?

    func toggle(_ media: MediaItem) {
        if let active = Self.activePlayer, active !== self {
            active.stop()
        }
        Self.activePlayer = self
        if loadedID != media.mediaId {
            load(media)
        }
        guard let player else { return }
        if isPlaying {
            player.pause()
            isPlaying = false
        } else {
            if duration > 0, progress >= duration - 0.05 {
                player.seek(to: .zero)
                progress = 0
            }
            player.play()
            if playbackRate != 1.0 {
                player.rate = playbackRate
            }
            isPlaying = true
        }
    }

    func load(_ media: MediaItem) {
        stop()
        guard let url = media.fileURL else { return }
        loadedID = media.mediaId
        let item = AVPlayerItem(url: url)
        let player = AVPlayer(playerItem: item)
        self.player = player
        let interval = CMTime(seconds: 0.25, preferredTimescale: 600)
        timeObserver = player.addPeriodicTimeObserver(forInterval: interval, queue: .main) { [weak self] time in
            let seconds = time.seconds
            let itemDuration = item.duration.seconds
            Task { @MainActor in
                guard let self else { return }
                self.progress = max(0, seconds.isFinite ? seconds : 0)
                if itemDuration.isFinite, itemDuration > 0 {
                    self.duration = itemDuration
                }
            }
        }
        endObserver = NotificationCenter.default.addObserver(
            forName: .AVPlayerItemDidPlayToEndTime,
            object: item,
            queue: .main
        ) { [weak self] _ in
            Task { @MainActor in
                self?.isPlaying = false
                self?.progress = 0
                self?.player?.seek(to: .zero)
            }
        }
        duration = media.durationSec
    }

    func seek(fraction: Double) {
        guard let player, duration > 0 else { return }
        let clamped = min(1, max(0, fraction))
        player.seek(to: CMTime(seconds: clamped * duration, preferredTimescale: 600))
        progress = clamped * duration
    }

    func setRate(_ rate: Float) {
        playbackRate = min(3.0, max(0.5, rate))
        if isPlaying {
            player?.rate = playbackRate
        }
    }

    func stop() {
        if Self.activePlayer === self {
            Self.activePlayer = nil
        }
        player?.pause()
        if let timeObserver, let player {
            player.removeTimeObserver(timeObserver)
        }
        timeObserver = nil
        if let endObserver {
            NotificationCenter.default.removeObserver(endObserver)
        }
        endObserver = nil
        player = nil
        isPlaying = false
        progress = 0
        duration = 0
        loadedID = ""
    }

}
