import AppKit

/// Every window/panel owner the coordinator knows about. Roles are grouped by
/// priority; see `WindowPriority` and `PanelFactory`.
enum WindowRole: String, CaseIterable, Sendable {
    // passive: never steal key, never suspend others
    case taskToast
    case sidecarSuggestion

    // transient: non-activating overlays
    case dropPocket
    case spatialLasso
    case walkieTalkie
    case menuBarPopover
    case quickLookPanel

    // content: at most one key owner; large surfaces suspend QuickEntry
    case quickEntry
    case workspaceWing
    case workspaceWindow
    case mediaAttached
    case mediaDetached
    case settings
    case pinnedNote

    // modal: takes key, does not suspend content
    case approval

    // security: highest priority, never steals focus on its own
    case permissionGuide

    var priority: WindowPriority {
        switch self {
        case .taskToast, .sidecarSuggestion:
            return .passive
        case .dropPocket, .spatialLasso, .walkieTalkie, .menuBarPopover, .quickLookPanel:
            return .transient
        case .quickEntry, .workspaceWing, .workspaceWindow,
             .mediaAttached, .mediaDetached, .settings, .pinnedNote:
            return .content
        case .approval:
            return .modal
        case .permissionGuide:
            return .security
        }
    }

    /// Only pinned notes may have several live instances at once.
    var allowsMultipleInstances: Bool {
        self == .pinnedNote
    }

    /// Opening one of these suspends QuickEntry (plan §5.4).
    var suspendsQuickEntryWhenPresented: Bool {
        switch self {
        case .settings, .workspaceWindow, .mediaDetached:
            return true
        default:
            return false
        }
    }
}

enum WindowPriority: Int, CaseIterable, Comparable, Sendable {
    case passive = 0
    case transient = 10
    case content = 20
    case modal = 30
    case security = 40

    static func < (lhs: WindowPriority, rhs: WindowPriority) -> Bool {
        lhs.rawValue < rhs.rawValue
    }
}

enum ActivationPolicy: Sendable {
    /// Never call `NSApp.activate`; `orderFrontRegardless` is enough.
    case none
    /// This role genuinely needs the app active (QuickEntry, Settings, ...).
    case required
    /// Activate, then hand focus back to the previous app when dismissed.
    case restorePrevious
}

struct WindowRequest: Sendable {
    let role: WindowRole
    let reason: String
    let activation: ActivationPolicy
    /// Optional grouping key for roles that must open at most once per group.
    let exclusiveGroup: String?

    init(
        role: WindowRole,
        reason: String = "",
        activation: ActivationPolicy = .required,
        exclusiveGroup: String? = nil
    ) {
        self.role = role
        self.reason = reason
        self.activation = activation
        self.exclusiveGroup = exclusiveGroup
    }
}

struct WindowLease: Equatable, Sendable {
    let role: WindowRole
    let generation: UInt64
}
