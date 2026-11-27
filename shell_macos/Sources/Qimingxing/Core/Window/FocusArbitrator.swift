import AppKit

/// The only type allowed to call `NSApp.activate`. It remembers which app was
/// frontmost before 启明星 took focus and hands focus back only when doing so
/// cannot steal the user's attention from another app (plan §5.4, rule 4).
@MainActor
final class FocusArbitrator {
    struct Environment {
        var frontmostApp: () -> NSRunningApplication?
        var isCurrentApp: (NSRunningApplication) -> Bool
        var appIsActive: () -> Bool
        var activateApp: () -> Void
        var activateExternalApp: (NSRunningApplication) -> Void

        @MainActor
        static func live() -> Environment {
            Environment(
                frontmostApp: { NSWorkspace.shared.frontmostApplication },
                isCurrentApp: { $0 == NSRunningApplication.current },
                appIsActive: { NSApp.isActive },
                activateApp: { NSApp.activate(ignoringOtherApps: true) },
                activateExternalApp: { $0.activate(options: []) }
            )
        }
    }

    private let environment: Environment
    private var previousApp: NSRunningApplication?
    private(set) var lastActivatedRole: WindowRole?

    init(environment: Environment? = nil) {
        self.environment = environment ?? Environment.live()
    }

    /// Call before showing a window that needs key. No-op for `.none`.
    func activate(for role: WindowRole, policy: ActivationPolicy) {
        guard policy != .none else { return }
        if previousApp == nil {
            let frontmost = environment.frontmostApp()
            // Only remember an *external* app; if 启明星 is already frontmost
            // there is nothing to hand back.
            if let frontmost, !environment.isCurrentApp(frontmost) {
                previousApp = frontmost
            }
        }
        environment.activateApp()
        lastActivatedRole = role
    }

    /// Returns true when focus was handed back to the previous app.
    @discardableResult
    func restoreIfAppropriate() -> Bool {
        guard let previous = previousApp else { return false }
        guard environment.appIsActive() else {
            // The user switched to another app themselves: never steal focus.
            previousApp = nil
            return false
        }
        environment.activateExternalApp(previous)
        previousApp = nil
        return true
    }

    var rememberedApp: NSRunningApplication? { previousApp }
}
