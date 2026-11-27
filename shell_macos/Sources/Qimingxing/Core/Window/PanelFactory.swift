import AppKit

/// Declarative panel traits per role. Kept separate from `NSPanel` so the
/// rules can be asserted in XCTest without a window server.
struct PanelConfiguration: Equatable {
    let level: NSWindow.Level
    let styleMask: NSWindow.StyleMask
    let collectionBehavior: NSWindow.CollectionBehavior
    let hidesOnDeactivate: Bool
    let becomesKeyOnlyIfNeeded: Bool

    static func forRole(_ role: WindowRole) -> PanelConfiguration {
        if role == .quickEntry {
            return PanelConfiguration(
                level: .floating,
                styleMask: [.nonactivatingPanel, .borderless],
                collectionBehavior: [.canJoinAllSpaces, .fullScreenAuxiliary],
                hidesOnDeactivate: false,
                becomesKeyOnlyIfNeeded: false
            )
        }
        switch role.priority {
        case .passive:
            return PanelConfiguration(
                level: .statusBar,
                styleMask: [.nonactivatingPanel, .borderless],
                collectionBehavior: [.canJoinAllSpaces, .fullScreenAuxiliary],
                hidesOnDeactivate: false,
                becomesKeyOnlyIfNeeded: true
            )
        case .transient:
            return PanelConfiguration(
                level: .floating,
                styleMask: [.nonactivatingPanel, .borderless],
                collectionBehavior: [.canJoinAllSpaces, .fullScreenAuxiliary],
                hidesOnDeactivate: false,
                becomesKeyOnlyIfNeeded: true
            )
        case .content:
            return PanelConfiguration(
                level: .normal,
                styleMask: [.titled, .closable, .miniaturizable, .resizable, .fullSizeContentView],
                collectionBehavior: [.moveToActiveSpace],
                hidesOnDeactivate: false,
                becomesKeyOnlyIfNeeded: false
            )
        case .modal:
            return PanelConfiguration(
                level: .floating,
                styleMask: [.borderless],
                collectionBehavior: [.moveToActiveSpace],
                hidesOnDeactivate: false,
                becomesKeyOnlyIfNeeded: false
            )
        case .security:
            return PanelConfiguration(
                level: .floating,
                styleMask: [.nonactivatingPanel, .borderless],
                collectionBehavior: [.canJoinAllSpaces, .fullScreenAuxiliary],
                hidesOnDeactivate: false,
                becomesKeyOnlyIfNeeded: true
            )
        }
    }
}

@MainActor
enum PanelFactory {
    static func makePanel(for role: WindowRole, contentRect: NSRect) -> NSPanel {
        let config = PanelConfiguration.forRole(role)
        let panel = NSPanel(
            contentRect: contentRect,
            styleMask: config.styleMask,
            backing: .buffered,
            defer: true
        )
        apply(config, to: panel)
        return panel
    }

    static func apply(_ config: PanelConfiguration, to panel: NSPanel) {
        panel.level = config.level
        panel.collectionBehavior = config.collectionBehavior
        panel.hidesOnDeactivate = config.hidesOnDeactivate
        panel.becomesKeyOnlyIfNeeded = config.becomesKeyOnlyIfNeeded
    }
}
