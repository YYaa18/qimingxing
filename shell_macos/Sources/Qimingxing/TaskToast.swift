import AppKit

/// A non-activating task-completion banner. It complements UNUserNotification
/// (which can be suppressed by Focus or denied permission) so background work
/// is always perceptible in the app itself.
@MainActor
final class TaskToastController {
    static let shared = TaskToastController()

    private var panel: NSPanel?
    private var dismissTask: Task<Void, Never>?

    func show(title: String, message: String, isError: Bool, onOpen: @escaping () -> Void) {
        close()

        let width: CGFloat = 420
        let height: CGFloat = 82
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

        let effect = NSVisualEffectView(frame: NSRect(x: 0, y: 0, width: width, height: height))
        effect.material = .popover
        effect.blendingMode = .behindWindow
        effect.state = .active
        effect.wantsLayer = true
        effect.layer?.cornerRadius = 15
        effect.layer?.masksToBounds = true
        effect.layer?.borderWidth = 0.5
        effect.layer?.borderColor = NSColor.separatorColor.withAlphaComponent(0.45).cgColor

        let icon = NSImageView()
        icon.translatesAutoresizingMaskIntoConstraints = false
        icon.image = NSImage(
            systemSymbolName: isError ? "exclamationmark.triangle.fill" : "checkmark.circle.fill",
            accessibilityDescription: isError ? "失败" : "完成"
        )
        icon.contentTintColor = isError ? .systemOrange : .systemGreen

        let titleLabel = NSTextField(labelWithString: title.isEmpty ? "后台任务" : title)
        titleLabel.font = .systemFont(ofSize: 13, weight: .semibold)
        titleLabel.lineBreakMode = .byTruncatingTail
        titleLabel.maximumNumberOfLines = 1

        let bodyLabel = NSTextField(labelWithString: message)
        bodyLabel.font = .systemFont(ofSize: 11)
        bodyLabel.textColor = .secondaryLabelColor
        bodyLabel.lineBreakMode = .byTruncatingTail
        bodyLabel.maximumNumberOfLines = 2
        bodyLabel.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        titleLabel.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)

        let openButton = NSButton(title: "查看结果", target: nil, action: nil)
        openButton.bezelStyle = .rounded
        openButton.controlSize = .small
        openButton.font = .systemFont(ofSize: 11, weight: .medium)
        openButton.translatesAutoresizingMaskIntoConstraints = false

        let closeButton = NSButton(title: "关闭", target: nil, action: nil)
        closeButton.bezelStyle = .inline
        closeButton.isBordered = false
        closeButton.controlSize = .small
        closeButton.contentTintColor = .tertiaryLabelColor
        closeButton.font = .systemFont(ofSize: 10.5)
        closeButton.translatesAutoresizingMaskIntoConstraints = false

        let stack = NSStackView(views: [titleLabel, bodyLabel])
        stack.orientation = .vertical
        stack.spacing = 2
        stack.alignment = .leading
        stack.translatesAutoresizingMaskIntoConstraints = false

        effect.addSubview(icon)
        effect.addSubview(stack)
        effect.addSubview(openButton)
        effect.addSubview(closeButton)
        NSLayoutConstraint.activate([
            icon.leadingAnchor.constraint(equalTo: effect.leadingAnchor, constant: 15),
            icon.topAnchor.constraint(equalTo: effect.topAnchor, constant: 16),
            icon.widthAnchor.constraint(equalToConstant: 20),
            icon.heightAnchor.constraint(equalToConstant: 20),

            stack.leadingAnchor.constraint(equalTo: icon.trailingAnchor, constant: 10),
            stack.trailingAnchor.constraint(equalTo: closeButton.leadingAnchor, constant: -8),
            stack.topAnchor.constraint(equalTo: effect.topAnchor, constant: 13),
            stack.widthAnchor.constraint(equalToConstant: 320),

            closeButton.trailingAnchor.constraint(equalTo: effect.trailingAnchor, constant: -10),
            closeButton.topAnchor.constraint(equalTo: effect.topAnchor, constant: 10),
            closeButton.widthAnchor.constraint(equalToConstant: 34),

            openButton.trailingAnchor.constraint(equalTo: effect.trailingAnchor, constant: -13),
            openButton.bottomAnchor.constraint(equalTo: effect.bottomAnchor, constant: -11),
            openButton.heightAnchor.constraint(equalToConstant: 22),
        ])
        panel.contentView = effect

        let onOpenBox = ActionBox(onOpen)
        let onCloseBox = ActionBox { [weak self] in self?.close() }
        openButton.target = onOpenBox
        openButton.action = #selector(ActionBox.fire)
        closeButton.target = onCloseBox
        closeButton.action = #selector(ActionBox.fire)
        panel.isReleasedWhenClosed = false
        objc_setAssociatedObject(panel, &TaskToastController.associations, [onOpenBox, onCloseBox], .OBJC_ASSOCIATION_RETAIN_NONATOMIC)
        panel.contentMinSize = NSSize(width: width, height: height)
        panel.contentMaxSize = NSSize(width: width, height: height)

        if let screen = NSScreen.main ?? NSScreen.screens.first {
            let frame = screen.visibleFrame
            panel.setFrameOrigin(NSPoint(x: frame.midX - width / 2, y: frame.maxY - height - 18))
        } else {
            panel.center()
        }
        panel.alphaValue = 0
        panel.orderFrontRegardless()
        NSAnimationContext.runAnimationGroup { context in
            context.duration = 0.18
            panel.animator().alphaValue = 1
        }
        self.panel = panel

        dismissTask = Task { @MainActor [weak self] in
            try? await Task.sleep(nanoseconds: 7_000_000_000)
            guard !Task.isCancelled else { return }
            self?.close()
        }
    }

    func close() {
        dismissTask?.cancel()
        dismissTask = nil
        guard let panel else { return }
        self.panel = nil
        panel.alphaValue = 0
        panel.orderOut(nil)
    }

    private static var associations: UInt8 = 0
}

private final class ActionBox: NSObject {
    private let action: () -> Void
    init(_ action: @escaping () -> Void) { self.action = action }
    @objc func fire() { action() }
}
