import AppKit
import SwiftUI

enum TaskToastStyle: Equatable, Sendable {
    case success
    case warning
    case error
    case info
}

struct TaskToastBannerView: View {
    let title: String
    let message: String
    let style: TaskToastStyle
    let actionTitle: String?
    let onOpen: () -> Void
    let onClose: () -> Void
    var onHoverChanged: ((Bool) -> Void)? = nil

    @Environment(\.colorScheme) private var colorScheme
    @State private var isCloseHovered = false
    @State private var isActionHovered = false

    private var isDark: Bool { colorScheme == .dark }

    private var outerCornerRadius: CGFloat { 24 }
    private var innerCornerRadius: CGFloat { 20 }

    private var badgeTopColor: Color {
        switch style {
        case .success:
            return Color(red: 0.30, green: 0.90, blue: 0.49)
        case .warning:
            return Color(red: 1.00, green: 0.71, blue: 0.24)
        case .error:
            return Color(red: 1.00, green: 0.44, blue: 0.42)
        case .info:
            return Color(red: 0.36, green: 0.78, blue: 1.00)
        }
    }

    private var badgeBottomColor: Color {
        switch style {
        case .success:
            return Color(red: 0.09, green: 0.75, blue: 0.31)
        case .warning:
            return Color(red: 0.95, green: 0.49, blue: 0.11)
        case .error:
            return Color(red: 0.87, green: 0.22, blue: 0.24)
        case .info:
            return Color(red: 0.11, green: 0.52, blue: 0.98)
        }
    }

    private var badgeSymbolName: String {
        switch style {
        case .success: return "checkmark"
        case .warning, .error: return "exclamationmark"
        case .info: return "sparkles"
        }
    }

    private var badgeShadowColor: Color {
        badgeBottomColor.opacity(isDark ? 0.42 : 0.36)
    }

    var body: some View {
        ZStack {
            // Outer Liquid Glass rim
            RoundedRectangle(cornerRadius: outerCornerRadius, style: .continuous)
                .fill(.clear)
                .background(outerBackdrop)
                .clipShape(RoundedRectangle(cornerRadius: outerCornerRadius, style: .continuous))
                .overlay(outerSpecularRim)

            // Inner frosted glass card inset by 4pt for the double-bezel specular depth
            ZStack {
                RoundedRectangle(cornerRadius: innerCornerRadius, style: .continuous)
                    .fill(innerSurfaceGradient)
                    .overlay(innerSpecularRim)

                contentLayout
                    .padding(.leading, 18)
                    .padding(.trailing, 14)
                    .padding(.vertical, 12)
            }
            .padding(4)
        }
        .frame(width: TaskToastController.bannerSize.width, height: TaskToastController.bannerSize.height)
        .onHover { hovering in
            onHoverChanged?(hovering)
        }
    }

    @ViewBuilder
    private var outerBackdrop: some View {
        if QMXTheme.isHeadlessDump {
            LinearGradient(
                colors: isDark
                    ? [Color.white.opacity(0.14), Color.white.opacity(0.08)]
                    : [Color.white.opacity(0.46), Color.white.opacity(0.32)],
                startPoint: .topLeading,
                endPoint: .bottomTrailing
            )
        } else {
            ZStack {
                VisualEffectBlur(
                    material: .hudWindow,
                    blendingMode: .behindWindow,
                    state: .active,
                    cornerRadius: outerCornerRadius
                )
                LinearGradient(
                    colors: isDark
                        ? [Color.white.opacity(0.08), Color.white.opacity(0.02)]
                        : [Color.white.opacity(0.42), Color.white.opacity(0.18)],
                    startPoint: .topLeading,
                    endPoint: .bottomTrailing
                )
            }
        }
    }

    private var outerSpecularRim: some View {
        RoundedRectangle(cornerRadius: outerCornerRadius, style: .continuous)
            .strokeBorder(
                LinearGradient(
                    colors: isDark
                        ? [Color.white.opacity(0.36), Color.white.opacity(0.12), Color.white.opacity(0.22)]
                        : [Color.white.opacity(0.88), Color.white.opacity(0.45), Color.white.opacity(0.70)],
                    startPoint: .topLeading,
                    endPoint: .bottomTrailing
                ),
                lineWidth: 1.0
            )
    }

    private var innerSurfaceGradient: LinearGradient {
        LinearGradient(
            colors: isDark
                ? [Color(red: 0.13, green: 0.15, blue: 0.19).opacity(0.66), Color(red: 0.10, green: 0.12, blue: 0.16).opacity(0.56)]
                : [Color.white.opacity(0.54), Color.white.opacity(0.34)],
            startPoint: .topLeading,
            endPoint: .bottomTrailing
        )
    }

    private var innerSpecularRim: some View {
        RoundedRectangle(cornerRadius: innerCornerRadius, style: .continuous)
            .strokeBorder(
                LinearGradient(
                    colors: isDark
                        ? [Color.white.opacity(0.26), Color.white.opacity(0.07), Color.white.opacity(0.14)]
                        : [Color.white.opacity(0.85), Color.white.opacity(0.35), Color.white.opacity(0.65)],
                    startPoint: .topLeading,
                    endPoint: .bottomTrailing
                ),
                lineWidth: 0.9
            )
    }

    private var contentLayout: some View {
        HStack(alignment: .center, spacing: 15) {
            iconBadge

            VStack(alignment: .leading, spacing: 3) {
                Text(title.isEmpty ? "后台任务" : title)
                    .font(.system(size: 17.5, weight: .bold))
                    .foregroundStyle(isDark ? Color.white.opacity(0.96) : Color(red: 0.08, green: 0.08, blue: 0.09))
                    .lineLimit(1)
                    .truncationMode(.tail)

                if !message.isEmpty {
                    Text(message)
                        .font(.system(size: 13, weight: .regular))
                        .foregroundStyle(isDark ? Color.white.opacity(0.70) : Color.black.opacity(0.58))
                        .lineLimit(2)
                        .truncationMode(.tail)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)

            VStack(alignment: .trailing, spacing: 0) {
                dismissButton
                Spacer(minLength: 6)
                if let actionTitle, !actionTitle.isEmpty {
                    actionPillButton(title: actionTitle)
                }
            }
        }
    }

    private var iconBadge: some View {
        ZStack {
            Circle()
                .fill(
                    LinearGradient(
                        colors: [badgeTopColor, badgeBottomColor],
                        startPoint: .top,
                        endPoint: .bottom
                    )
                )
                .shadow(color: badgeShadowColor, radius: 7, x: 0, y: 3)

            Circle()
                .strokeBorder(
                    LinearGradient(
                        colors: [Color.white.opacity(0.55), Color.white.opacity(0.08)],
                        startPoint: .top,
                        endPoint: .bottom
                    ),
                    lineWidth: 1.0
                )

            Image(systemName: badgeSymbolName)
                .font(.system(size: 21, weight: .bold))
                .foregroundStyle(.white)
                .shadow(color: Color.black.opacity(0.14), radius: 1, x: 0, y: 1)
        }
        .frame(width: 48, height: 48)
    }

    private var dismissButton: some View {
        Button(action: onClose) {
            HStack(spacing: 4) {
                Image(systemName: "xmark")
                    .font(.system(size: 10, weight: .semibold))
                Text("关闭")
                    .font(.system(size: 12, weight: .medium))
            }
            .foregroundStyle(
                isDark
                    ? Color.white.opacity(isCloseHovered ? 0.88 : 0.56)
                    : Color.black.opacity(isCloseHovered ? 0.80 : 0.52)
            )
            .padding(.horizontal, 6)
            .padding(.vertical, 3)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .onHover { hovering in
            isCloseHovered = hovering
        }
    }

    private func actionPillButton(title: String) -> some View {
        Button(action: onOpen) {
            Text(title)
                .font(.system(size: 13, weight: .medium))
                .foregroundStyle(isDark ? Color.white.opacity(0.95) : Color(red: 0.14, green: 0.14, blue: 0.16))
                .padding(.horizontal, 14)
                .frame(height: 28)
                .background(
                    Capsule(style: .continuous)
                        .fill(
                            isDark
                                ? Color.white.opacity(isActionHovered ? 0.22 : 0.15)
                                : Color.white.opacity(isActionHovered ? 0.90 : 0.74)
                        )
                )
                .overlay(
                    Capsule(style: .continuous)
                        .strokeBorder(
                            isDark
                                ? Color.white.opacity(0.24)
                                : Color.white.opacity(0.90),
                            lineWidth: 0.8
                        )
                )
                .shadow(
                    color: Color.black.opacity(isDark ? 0.25 : 0.08),
                    radius: 4,
                    x: 0,
                    y: 2
                )
        }
        .buttonStyle(.plain)
        .onHover { hovering in
            isActionHovered = hovering
        }
    }
}

/// A non-activating Liquid Glass toast banner. It complements UNUserNotification
/// (which can be suppressed by Focus or denied permission) and optionally mirrors
/// notifications to macOS Notification Center via `nativeNotificationHandler`.
@MainActor
final class TaskToastController {
    static let shared = TaskToastController()
    static let bannerSize = NSSize(width: 436, height: 106)

    var nativeNotificationHandler: ((String, String, NotificationRoute, String?) -> Void)?

    private var panel: NSPanel?
    private var dismissTask: Task<Void, Never>?

    func show(
        title: String,
        message: String,
        isError: Bool = false,
        style: TaskToastStyle? = nil,
        actionTitle: String? = "查看结果",
        route: NotificationRoute = .quickEntry,
        mirrorToSystem: Bool = true,
        onOpen: @escaping () -> Void = {}
    ) {
        let resolvedStyle = style ?? (isError ? .error : .success)

        if mirrorToSystem {
            nativeNotificationHandler?(title, message, route, actionTitle)
        }

        guard DesktopActions.inAppToastEnabled || QMXTheme.isHeadlessDump else {
            return
        }

        close()

        let width = Self.bannerSize.width
        let height = Self.bannerSize.height
        let panel = NSPanel(
            contentRect: NSRect(x: 0, y: 0, width: width, height: height),
            styleMask: [.nonactivatingPanel, .borderless],
            backing: .buffered,
            defer: false
        )
        panel.level = .statusBar
        panel.isOpaque = false
        panel.backgroundColor = .clear
        panel.hasShadow = true
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .ignoresCycle]
        panel.isMovableByWindowBackground = false
        panel.hidesOnDeactivate = false
        panel.isReleasedWhenClosed = false
        panel.contentMinSize = Self.bannerSize
        panel.contentMaxSize = Self.bannerSize

        let view = TaskToastBannerView(
            title: title,
            message: message,
            style: resolvedStyle,
            actionTitle: actionTitle,
            onOpen: { [weak self] in
                self?.close()
                onOpen()
            },
            onClose: { [weak self] in
                self?.close()
            },
            onHoverChanged: { [weak self] hovering in
                self?.handleHoverChanged(hovering)
            }
        )

        let hosting = NSHostingView(rootView: view)
        hosting.frame = NSRect(x: 0, y: 0, width: width, height: height)
        hosting.wantsLayer = true
        hosting.layer?.backgroundColor = NSColor.clear.cgColor
        panel.contentView = hosting

        let targetOrigin: NSPoint
        if let screen = activeToastScreen() {
            let frame = screen.visibleFrame
            targetOrigin = NSPoint(x: frame.midX - width / 2, y: frame.maxY - height - 20)
        } else {
            panel.center()
            targetOrigin = panel.frame.origin
        }

        let startOrigin = NSPoint(x: targetOrigin.x, y: targetOrigin.y + 10)
        panel.setFrameOrigin(startOrigin)
        panel.alphaValue = 0
        panel.orderFrontRegardless()

        NSAnimationContext.runAnimationGroup { context in
            context.duration = 0.22
            context.timingFunction = CAMediaTimingFunction(name: .easeOut)
            panel.animator().alphaValue = 1
            panel.animator().setFrameOrigin(targetOrigin)
        }

        self.panel = panel
        scheduleAutoDismiss(seconds: 6.5)
    }

    func close() {
        dismissTask?.cancel()
        dismissTask = nil
        guard let panel else { return }
        self.panel = nil
        panel.alphaValue = 0
        panel.orderOut(nil)
    }

    private func handleHoverChanged(_ hovering: Bool) {
        guard panel != nil else { return }
        if hovering {
            dismissTask?.cancel()
            dismissTask = nil
        } else {
            scheduleAutoDismiss(seconds: 2.5)
        }
    }

    private func scheduleAutoDismiss(seconds: TimeInterval) {
        dismissTask?.cancel()
        let nanos = UInt64(max(0.5, seconds) * 1_000_000_000)
        dismissTask = Task { @MainActor [weak self] in
            try? await Task.sleep(nanoseconds: nanos)
            guard !Task.isCancelled else { return }
            self?.close()
        }
    }

    private func activeToastScreen() -> NSScreen? {
        let mouse = NSEvent.mouseLocation
        if let hovered = NSScreen.screens.first(where: { NSMouseInRect(mouse, $0.frame, false) }) {
            return hovered
        }
        return NSScreen.main ?? NSScreen.screens.first
    }
}
