import Foundation

/// Single source of truth for which window owns the keyboard and which
/// surfaces are suspended. This is a pure state machine: it never touches
/// AppKit. Callers translate its callbacks into `orderFront`/`orderOut`/
/// `NSApp.activate`, so the rules can be unit-tested without a window server.
@MainActor
final class WindowCoordinator {
    private struct Entry {
        let role: WindowRole
        let lease: WindowLease
        let sequence: UInt64
        let activation: ActivationPolicy
        let exclusiveGroup: String?
    }

    private struct SuspendedEntry {
        let entry: Entry
        let reason: String
    }

    private var generation: UInt64 = 0
    private var entries: [Entry] = []
    private var suspended: [WindowRole: SuspendedEntry] = [:]

    private(set) var keyOwner: WindowRole?

    private var debugEnabled: Bool {
        ProcessInfo.processInfo.environment["QMX_WINDOW_DEBUG"] == "1"
    }

    private func debugLog(_ message: @autoclosure () -> String) {
        guard debugEnabled else { return }
        NSLog("[QMXWindow] %@", message())
    }

    var onPresent: ((WindowRole, ActivationPolicy) -> Void)?
    var onDismiss: ((WindowRole) -> Void)?
    var onSuspend: ((WindowRole, String) -> Void)?
    var onResume: ((WindowRole) -> Void)?
    var onKeyOwnerChanged: ((WindowRole?) -> Void)?

    // MARK: - Present / dismiss

    @discardableResult
    func present(_ request: WindowRequest) -> WindowLease {
        if let group = request.exclusiveGroup,
           let existing = entries.first(where: {
               $0.role == request.role && $0.exclusiveGroup == group
           }) {
            return existing.lease
        }

        generation += 1
        let lease = WindowLease(role: request.role, generation: generation)
        let entry = Entry(
            role: request.role,
            lease: lease,
            sequence: generation,
            activation: request.activation,
            exclusiveGroup: request.exclusiveGroup
        )

        if !request.role.allowsMultipleInstances {
            entries.removeAll { $0.role == request.role }
        }
        entries.append(entry)

        // Decision A (plan §5.4): QuickEntry over Settings suspends Settings
        // until QuickEntry closes, then Settings comes back.
        if request.role == .quickEntry, isActive(.settings) {
            suspend(.settings, reason: "quick_entry_over_settings")
        }
        if request.role.suspendsQuickEntryWhenPresented {
            suspend(.quickEntry, reason: "presented:\(request.role.rawValue)")
        }

        recomputeKeyOwner()
        onPresent?(request.role, request.activation)
        debugLog(
            "present \(request.role.rawValue) gen=\(lease.generation) "
                + "key=\(keyOwner?.rawValue ?? "nil") active=\(activeRoles.map(\.rawValue))"
        )
        return lease
    }

    func dismiss(_ lease: WindowLease) {
        guard let index = entries.firstIndex(where: {
            $0.role == lease.role && $0.lease.generation == lease.generation
        }) else {
            // Stale lease: a delayed close callback from an older presentation
            // must not close the window that replaced it.
            return
        }
        let entry = entries.remove(at: index)
        // Closing the surface that caused a suspension brings the suspended
        // surface back (Settings -> QuickEntry and QuickEntry -> Settings).
        var toResume: [WindowRole] = []
        if entry.role == .quickEntry, suspended[.settings] != nil {
            toResume.append(.settings)
        }
        let presentedReason = "presented:\(entry.role.rawValue)"
        for (role, stored) in suspended where stored.reason == presentedReason {
            toResume.append(role)
        }
        for role in toResume {
            resume(role)
        }
        recomputeKeyOwner()
        onDismiss?(entry.role)
        debugLog(
            "dismiss \(entry.role.rawValue) key=\(keyOwner?.rawValue ?? "nil") "
                + "active=\(activeRoles.map(\.rawValue))"
        )
    }

    // MARK: - Suspend / resume

    func suspend(_ role: WindowRole, reason: String) {
        guard let index = entries.firstIndex(where: { $0.role == role }) else { return }
        let entry = entries.remove(at: index)
        suspended[role] = SuspendedEntry(entry: entry, reason: reason)
        recomputeKeyOwner()
        onSuspend?(role, reason)
        debugLog("suspend \(role.rawValue) reason=\(reason) key=\(keyOwner?.rawValue ?? "nil")")
    }

    func resume(_ role: WindowRole) {
        guard let stored = suspended.removeValue(forKey: role) else { return }
        generation += 1
        let entry = Entry(
            role: stored.entry.role,
            // Fresh generation invalidates old leases, but the original
            // sequence is kept so a resumed window does not jump ahead of a
            // content window that was already on screen.
            lease: WindowLease(role: role, generation: generation),
            sequence: stored.entry.sequence,
            activation: stored.entry.activation,
            exclusiveGroup: stored.entry.exclusiveGroup
        )
        entries.append(entry)
        recomputeKeyOwner()
        onResume?(role)
        debugLog("resume \(role.rawValue) key=\(keyOwner?.rawValue ?? "nil")")
    }

    // MARK: - Queries

    func isActive(_ role: WindowRole) -> Bool {
        entries.contains { $0.role == role }
    }

    func isSuspended(_ role: WindowRole) -> Bool {
        suspended[role] != nil
    }

    func suspensionReason(for role: WindowRole) -> String? {
        suspended[role]?.reason
    }

    var activeRoles: [WindowRole] {
        entries.map(\.role)
    }

    // MARK: - Key ownership

    private func recomputeKeyOwner() {
        let previous = keyOwner
        keyOwner = entries
            .filter { $0.role.canOwnKey }
            .max { lhs, rhs in
                if lhs.role.priority != rhs.role.priority {
                    return lhs.role.priority < rhs.role.priority
                }
                return lhs.sequence < rhs.sequence
            }?
            .role
        if keyOwner != previous {
            onKeyOwnerChanged?(keyOwner)
        }
    }
}

extension WindowRole {
    /// Roles that can actually hold the keyboard. Wings attached to QuickEntry
    /// and passive/transient overlays never become key owners on their own;
    /// the security guide is highest priority but deliberately never takes key.
    var canOwnKey: Bool {
        switch self {
        case .quickEntry, .workspaceWindow, .mediaDetached, .settings,
             .pinnedNote, .approval:
            return true
        default:
            return false
        }
    }
}
