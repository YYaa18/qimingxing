import AppKit
import AVKit
import SwiftUI

struct MediaStripView: View {
    enum Mode {
        case compact
        case expanded
    }

    let media: [MediaItem]
    var mode: Mode = .compact

    @StateObject private var audio = MediaAudioPlayer()

    private var visible: [MediaItem] {
        mode == .compact ? Array(media.prefix(4)) : media
    }

    private var overflow: Int {
        max(0, media.count - visible.count)
    }

    var body: some View {
        if !media.isEmpty {
            Group {
                if mode == .compact {
                    compactStrip
                } else {
                    expandedList
                }
            }
            .onDisappear { audio.stop() }
            .onChange(of: media.map(\.mediaId)) { _ in audio.stop() }
        }
    }

    private var compactStrip: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 8) {
                ForEach(visible) { item in
                    if item.kind == "audio" {
                        audioPill(item, expanded: false)
                    } else {
                        thumbnailTile(item)
                    }
                }
                if overflow > 0 {
                    Text("+\(overflow)")
                        .font(.system(size: 12, weight: .semibold))
                        .foregroundStyle(QMXTheme.textSecondary)
                        .frame(width: 44, height: 76)
                        .background(
                            RoundedRectangle(cornerRadius: 10, style: .continuous)
                                .fill(QMXTheme.surfaceMuted.opacity(0.7))
                        )
                }
            }
            .padding(.vertical, 4)
        }
        .frame(height: 84)
    }

    private var expandedList: some View {
        VStack(alignment: .leading, spacing: 10) {
            let images = media.filter { $0.kind == "image" }
            if !images.isEmpty {
                LazyVGrid(columns: [GridItem(.adaptive(minimum: 150), spacing: 10)], spacing: 10) {
                    ForEach(images) { item in
                        VStack(alignment: .leading, spacing: 4) {
                            thumbnailTile(item, width: nil, height: 150)
                            if let caption = attributionCaption(item) {
                                Text(caption)
                                    .font(.system(size: 9))
                                    .foregroundStyle(QMXTheme.textTertiary)
                                    .lineLimit(1)
                                    .help(caption)
                            }
                        }
                    }
                }
            }
            ForEach(media.filter { $0.kind == "audio" }) { item in
                audioPill(item, expanded: true)
            }
            ForEach(media.filter { $0.kind == "video" }) { item in
                videoPlayer(item)
            }
            ForEach(media.filter { $0.kind == "file" }) { item in
                fileRow(item)
            }
        }
    }

    @ViewBuilder
    private func thumbnailTile(_ item: MediaItem, width: CGFloat? = 112, height: CGFloat = 76) -> some View {
        Button {
            if let path = item.fileURL?.path { _ = DesktopActions().quickLook(path) }
        } label: {
            ZStack {
                RoundedRectangle(cornerRadius: 10, style: .continuous)
                    .fill(QMXTheme.surfaceMuted)
                MediaThumbnailView(media: item)
                    .clipShape(RoundedRectangle(cornerRadius: 10, style: .continuous))
                if item.kind == "video" {
                    Image(systemName: "play.fill")
                        .font(.system(size: 15, weight: .semibold))
                        .foregroundStyle(.white)
                        .padding(11)
                        .background(Circle().fill(Color.black.opacity(0.45)))
                    VStack {
                        Spacer()
                        HStack {
                            Spacer()
                            if item.durationSec > 0 {
                                Text(durationLabel(item.durationSec))
                                    .font(.system(size: 9, weight: .semibold, design: .monospaced))
                                    .foregroundStyle(.white)
                                    .padding(.horizontal, 5)
                                    .padding(.vertical, 2)
                                    .background(Capsule().fill(Color.black.opacity(0.55)))
                            }
                        }
                    }
                    .padding(6)
                }
            }
            .frame(width: width, height: height)
            .frame(maxWidth: width == nil ? .infinity : nil)
            .clipped()
            .contentShape(RoundedRectangle(cornerRadius: 10, style: .continuous))
        }
        .buttonStyle(TactileButtonStyle(haptic: .alignment))
        .help(attributionCaption(item) ?? item.title)
        .contextMenu {
            if let path = item.fileURL?.path {
                Button("快速预览") { _ = DesktopActions().quickLook(path) }
                Button("复制文件") { _ = DesktopActions().copyFile(path) }
                Button("在访达中显示") { _ = DesktopActions().revealPath(path) }
            }
            if !item.sourceURL.isEmpty, let url = URL(string: item.sourceURL),
               let scheme = url.scheme?.lowercased(), scheme == "http" || scheme == "https" {
                Button("打开来源页面") { NSWorkspace.shared.open(url) }
            }
        }
    }

    private func audioPill(_ item: MediaItem, expanded: Bool) -> some View {
        HStack(spacing: 8) {
            Button {
                audio.toggle(item)
            } label: {
                Image(systemName: audio.isPlaying && audio.loadedID == item.mediaId ? "pause.fill" : "play.fill")
                    .font(.system(size: 10, weight: .semibold))
                    .foregroundStyle(QMXTheme.textPrimary)
                    .frame(width: 24, height: 24)
                    .background(Circle().fill(QMXTheme.surfaceMuted))
            }
            .buttonStyle(.plain)

            VStack(alignment: .leading, spacing: 3) {
                if expanded {
                    Text(item.title)
                        .font(.system(size: 11, weight: .medium))
                        .foregroundStyle(QMXTheme.textSecondary)
                        .lineLimit(1)
                }
                if expanded {
                    Slider(
                        value: Binding(
                            get: { progress(item) },
                            set: { audio.seek(fraction: $0) }
                        ),
                        in: 0...1
                    )
                    .controlSize(.mini)
                    .tint(QMXTheme.accent)
                    .frame(height: 12)
                } else {
                    GeometryReader { geo in
                        ZStack(alignment: .leading) {
                            Capsule().fill(QMXTheme.hairline).frame(height: 2)
                            Capsule()
                                .fill(QMXTheme.accent)
                                .frame(width: max(2, geo.size.width * progress(item)), height: 2)
                        }
                    }
                    .frame(height: 2)
                }
            }
            Text(timeLabel(item, expanded: expanded))
                .font(.system(size: 9, design: .monospaced))
                .foregroundStyle(QMXTheme.textTertiary)
                .monospacedDigit()
            if expanded {
                Menu {
                    ForEach([0.75, 1.0, 1.25, 1.5, 2.0], id: \.self) { rate in
                        Button(String(format: "%gx", rate)) {
                            audio.setRate(Float(rate))
                        }
                    }
                } label: {
                    Text(String(format: "%gx", audio.playbackRate))
                        .font(.system(size: 9, weight: .semibold, design: .monospaced))
                        .foregroundStyle(QMXTheme.textSecondary)
                        .frame(width: 28, height: 20)
                        .background(Capsule().fill(QMXTheme.surfaceMuted))
                }
                .menuStyle(.borderlessButton)
                .fixedSize()
            }
        }
        .padding(.horizontal, 8)
        .frame(width: expanded ? nil : 210, height: 44)
        .frame(maxWidth: expanded ? .infinity : nil, alignment: .leading)
        .background(
            RoundedRectangle(cornerRadius: 10, style: .continuous)
                .fill(QMXTheme.surfaceMuted.opacity(0.55))
        )
        .overlay(
            RoundedRectangle(cornerRadius: 10, style: .continuous)
                .strokeBorder(QMXTheme.hairline, lineWidth: 0.5)
        )
    }

    @ViewBuilder
    private func videoPlayer(_ item: MediaItem) -> some View {
        if QMXTheme.isHeadlessDump {
            thumbnailTile(item, width: nil, height: 240)
        } else if let url = item.fileURL {
            VideoPlayer(player: AVPlayer(url: url))
                .frame(height: 240)
                .clipShape(RoundedRectangle(cornerRadius: 12, style: .continuous))
        }
    }

    private func fileRow(_ item: MediaItem) -> some View {
        HStack(spacing: 8) {
            Image(systemName: "doc")
            Text(item.title)
                .font(.system(size: 12))
            Spacer()
            if let path = item.fileURL?.path {
                Button("快速预览") { _ = DesktopActions().quickLook(path) }
                    .buttonStyle(.qmxSecondary)
            }
        }
        .padding(10)
        .qmxCard(fill: QMXTheme.surfaceRaised)
    }

    private func progress(_ item: MediaItem) -> Double {
        guard audio.loadedID == item.mediaId, audio.duration > 0 else { return 0 }
        return min(1, max(0, audio.progress / audio.duration))
    }

    private func timeLabel(_ item: MediaItem, expanded: Bool) -> String {
        guard audio.loadedID == item.mediaId else {
            return expanded ? durationLabel(item.durationSec) : "0:00 / \(durationLabel(item.durationSec))"
        }
        return "\(durationLabel(audio.progress)) / \(durationLabel(max(audio.duration, item.durationSec)))"
    }

    private func attributionCaption(_ item: MediaItem) -> String? {
        let source = item.sourceTitle.isEmpty ? item.sourceURL : item.sourceTitle
        let license = item.license.isEmpty ? "" : " · \(item.license)"
        if source.isEmpty && license.isEmpty { return nil }
        return source + license
    }

    private func durationLabel(_ seconds: Double) -> String {
        guard seconds.isFinite, seconds > 0 else { return "0:00" }
        let total = Int(seconds.rounded())
        return String(format: "%d:%02d", total / 60, total % 60)
    }
}

private struct MediaThumbnailView: View {
    let media: MediaItem
    @State private var image: NSImage?

    init(media: MediaItem) {
        self.media = media
        if QMXTheme.isHeadlessDump {
            _image = State(initialValue: MediaThumbnailCache.shared.synchronousThumbnail(for: media))
        }
    }

    var body: some View {
        Group {
            if let image {
                Image(nsImage: image)
                    .resizable()
                    .aspectRatio(contentMode: .fill)
                    .clipped()
            } else {
                Image(systemName: placeholderSymbol)
                    .font(.system(size: 18, weight: .regular))
                    .foregroundStyle(QMXTheme.textTertiary)
            }
        }
        .task(id: media.mediaId) {
            image = await MediaThumbnailCache.shared.thumbnail(for: media)
        }
    }

    private var placeholderSymbol: String {
        switch media.kind {
        case "video": return "film"
        case "audio": return "waveform"
        case "file": return "doc"
        default: return "photo"
        }
    }
}
