import Carbon
import Foundation

// MARK: - Dual-State Hotkey State Machine (Tap < 220ms vs Hold >= 220ms)

struct DualStateHotkeyState: Equatable {
    static let holdThreshold: TimeInterval = 0.22

    enum Phase: Equatable {
        case idle
        case pendingTap
        case holding
    }

    enum Action: Equatable {
        case none
        case tap
        case holdStart
        case holdEnd
        case holdCancel
    }

    private(set) var phase: Phase = .idle
    private var pressedAt: TimeInterval = 0

    mutating func handlePress(at now: TimeInterval) {
        guard phase == .idle else { return }
        phase = .pendingTap
        pressedAt = now
    }

    mutating func handleThresholdTick(at now: TimeInterval) -> Action {
        guard phase == .pendingTap else { return .none }
        guard now - pressedAt >= Self.holdThreshold - 0.001 else { return .none }
        phase = .holding
        return .holdStart
    }

    mutating func handleRelease(at now: TimeInterval) -> Action {
        switch phase {
        case .idle:
            return .none
        case .pendingTap:
            phase = .idle
            return .tap
        case .holding:
            phase = .idle
            return .holdEnd
        }
    }

    mutating func cancel() -> Action {
        let wasHolding = (phase == .holding)
        phase = .idle
        return wasHolding ? .holdCancel : .none
    }
}

@MainActor
final class DualStateHotkeyController {
    private var state = DualStateHotkeyState()
    private nonisolated(unsafe) var thresholdTimer: Timer?

    var onTap: (@MainActor () -> Void)?
    var onHoldStart: (@MainActor () -> Void)?
    var onHoldEnd: (@MainActor () -> Void)?
    var onHoldCancel: (@MainActor () -> Void)?

    deinit {
        thresholdTimer?.invalidate()
    }

    func handlePress() {
        thresholdTimer?.invalidate()
        let now = ProcessInfo.processInfo.systemUptime
        state.handlePress(at: now)
        thresholdTimer = Timer.scheduledTimer(
            withTimeInterval: DualStateHotkeyState.holdThreshold,
            repeats: false
        ) { [weak self] _ in
            Task { @MainActor [weak self] in
                self?.checkThreshold()
            }
        }
    }

    func handleRelease() {
        thresholdTimer?.invalidate()
        thresholdTimer = nil
        switch state.handleRelease(at: ProcessInfo.processInfo.systemUptime) {
        case .tap:
            onTap?()
        case .holdEnd:
            onHoldEnd?()
        case .none, .holdStart, .holdCancel:
            break
        }
    }

    func cancel() {
        thresholdTimer?.invalidate()
        thresholdTimer = nil
        if state.cancel() == .holdCancel {
            onHoldCancel?()
        }
    }

    private func checkThreshold() {
        thresholdTimer = nil
        if state.handleThresholdTick(at: ProcessInfo.processInfo.systemUptime) == .holdStart {
            onHoldStart?()
        }
    }
}

// MARK: - Carbon Hotkey Manager

@MainActor
final class HotkeyManager {
    private var hotKeyRefs: [UInt32: EventHotKeyRef] = [:]
    private var handlerRef: EventHandlerRef?
    private var pressCallbacks: [UInt32: @MainActor () -> Void] = [:]
    private var releaseCallbacks: [UInt32: @MainActor () -> Void] = [:]
    private var dualStateQuickEntry: DualStateHotkeyController?

    private static weak var sharedInstance: HotkeyManager?

    func registerDefaults(
        onQuickEntry: @escaping @MainActor () -> Void,
        onQuickEntryHoldStart: (@MainActor () -> Void)? = nil,
        onQuickEntryHoldEnd: (@MainActor () -> Void)? = nil,
        onQuickEntryHoldCancel: (@MainActor () -> Void)? = nil,
        onSettings: @escaping @MainActor () -> Void,
        onEmergencyStop: @escaping @MainActor () -> Void,
        onDropPocket: (@MainActor () -> Void)? = nil,
        onSpatialLasso: (@MainActor () -> Void)? = nil
    ) {
        unregisterAll()
        Self.sharedInstance = self
        installHandlerIfNeeded()

        registerQuickEntry(
            keyCode: UInt32(kVK_Space),
            modifiers: UInt32(optionKey),
            onQuickEntry: onQuickEntry,
            onHoldStart: onQuickEntryHoldStart,
            onHoldEnd: onQuickEntryHoldEnd,
            onHoldCancel: onQuickEntryHoldCancel
        )
        register(id: 2, keyCode: UInt32(kVK_ANSI_K), modifiers: UInt32(optionKey), onPress: onSettings)
        register(id: 3, keyCode: UInt32(kVK_Escape), modifiers: UInt32(optionKey), onPress: onEmergencyStop)
        if let onDropPocket {
            register(id: 4, keyCode: UInt32(kVK_ANSI_D), modifiers: UInt32(optionKey), onPress: onDropPocket)
        }
        if let onSpatialLasso {
            register(
                id: 5,
                keyCode: UInt32(kVK_Space),
                modifiers: UInt32(optionKey | shiftKey),
                onPress: onSpatialLasso
            )
        }
    }

    func updateFromStrings(
        chatInput: String,
        settings: String,
        cancel: String,
        dropPocket: String = "option+d",
        spatialLasso: String = "option+shift+space",
        onQuickEntry: @escaping @MainActor () -> Void,
        onQuickEntryHoldStart: (@MainActor () -> Void)? = nil,
        onQuickEntryHoldEnd: (@MainActor () -> Void)? = nil,
        onQuickEntryHoldCancel: (@MainActor () -> Void)? = nil,
        onSettings: @escaping @MainActor () -> Void,
        onEmergencyStop: @escaping @MainActor () -> Void,
        onDropPocket: (@MainActor () -> Void)? = nil,
        onSpatialLasso: (@MainActor () -> Void)? = nil
    ) {
        unregisterAll()
        Self.sharedInstance = self
        installHandlerIfNeeded()

        let (chatKC, chatMods) = Self.parseHotkey(chatInput) ?? (UInt32(kVK_Space), UInt32(optionKey))
        registerQuickEntry(
            keyCode: chatKC,
            modifiers: chatMods,
            onQuickEntry: onQuickEntry,
            onHoldStart: onQuickEntryHoldStart,
            onHoldEnd: onQuickEntryHoldEnd,
            onHoldCancel: onQuickEntryHoldCancel
        )

        if let (kc, mods) = Self.parseHotkey(settings) {
            register(id: 2, keyCode: kc, modifiers: mods, onPress: onSettings)
        } else {
            register(id: 2, keyCode: UInt32(kVK_ANSI_K), modifiers: UInt32(optionKey), onPress: onSettings)
        }
        if let (kc, mods) = Self.parseHotkey(cancel) {
            register(id: 3, keyCode: kc, modifiers: mods, onPress: onEmergencyStop)
        } else {
            register(id: 3, keyCode: UInt32(kVK_Escape), modifiers: UInt32(optionKey), onPress: onEmergencyStop)
        }
        if let onDropPocket {
            if let (kc, mods) = Self.parseHotkey(dropPocket) {
                register(id: 4, keyCode: kc, modifiers: mods, onPress: onDropPocket)
            } else {
                register(id: 4, keyCode: UInt32(kVK_ANSI_D), modifiers: UInt32(optionKey), onPress: onDropPocket)
            }
        }
        if let onSpatialLasso {
            if let (kc, mods) = Self.parseHotkey(spatialLasso) {
                register(id: 5, keyCode: kc, modifiers: mods, onPress: onSpatialLasso)
            } else {
                register(id: 5, keyCode: UInt32(kVK_Space), modifiers: UInt32(optionKey | shiftKey), onPress: onSpatialLasso)
            }
        }
    }

    func cancelQuickEntryHold() {
        dualStateQuickEntry?.cancel()
    }

    func unregisterAll() {
        for (_, ref) in hotKeyRefs {
            UnregisterEventHotKey(ref)
        }
        hotKeyRefs.removeAll()
        pressCallbacks.removeAll()
        releaseCallbacks.removeAll()
        dualStateQuickEntry?.cancel()
        dualStateQuickEntry = nil
    }

    private func registerQuickEntry(
        keyCode: UInt32,
        modifiers: UInt32,
        onQuickEntry: @escaping @MainActor () -> Void,
        onHoldStart: (@MainActor () -> Void)?,
        onHoldEnd: (@MainActor () -> Void)?,
        onHoldCancel: (@MainActor () -> Void)?
    ) {
        if let onHoldStart, let onHoldEnd {
            let dual = DualStateHotkeyController()
            dual.onTap = onQuickEntry
            dual.onHoldStart = onHoldStart
            dual.onHoldEnd = onHoldEnd
            dual.onHoldCancel = onHoldCancel
            dualStateQuickEntry = dual
            register(
                id: 1,
                keyCode: keyCode,
                modifiers: modifiers,
                onPress: { [weak dual] in dual?.handlePress() },
                onRelease: { [weak dual] in dual?.handleRelease() }
            )
        } else {
            register(id: 1, keyCode: keyCode, modifiers: modifiers, onPress: onQuickEntry)
        }
    }

    private func installHandlerIfNeeded() {
        guard handlerRef == nil else { return }
        let eventTypes = [
            EventTypeSpec(eventClass: OSType(kEventClassKeyboard), eventKind: UInt32(kEventHotKeyPressed)),
            EventTypeSpec(eventClass: OSType(kEventClassKeyboard), eventKind: UInt32(kEventHotKeyReleased))
        ]
        InstallEventHandler(
            GetApplicationEventTarget(),
            { _, event, _ in
                var hkID = EventHotKeyID()
                let status = GetEventParameter(
                    event,
                    EventParamName(kEventParamDirectObject),
                    EventParamType(typeEventHotKeyID),
                    nil,
                    MemoryLayout<EventHotKeyID>.size,
                    nil,
                    &hkID
                )
                guard status == noErr, let event else { return noErr }
                let kind = GetEventKind(event)
                let targetID = hkID.id
                Task { @MainActor in
                    guard let mgr = HotkeyManager.sharedInstance else { return }
                    if kind == UInt32(kEventHotKeyReleased) {
                        mgr.releaseCallbacks[targetID]?()
                    } else {
                        mgr.pressCallbacks[targetID]?()
                    }
                }
                return noErr
            },
            eventTypes.count,
            eventTypes,
            nil,
            &handlerRef
        )
    }

    private func register(
        id: UInt32,
        keyCode: UInt32,
        modifiers: UInt32,
        onPress: @escaping @MainActor () -> Void,
        onRelease: (@MainActor () -> Void)? = nil
    ) {
        var ref: EventHotKeyRef?
        let hotKeyID = EventHotKeyID(signature: OSType(0x514D5831), id: id)
        let status = RegisterEventHotKey(
            keyCode,
            modifiers,
            hotKeyID,
            GetApplicationEventTarget(),
            0,
            &ref
        )
        if status == noErr, let ref {
            hotKeyRefs[id] = ref
            pressCallbacks[id] = onPress
            if let onRelease {
                releaseCallbacks[id] = onRelease
            }
        }
    }

    private static func parseHotkey(_ spec: String) -> (UInt32, UInt32)? {
        let parts = spec.lowercased()
            .split(separator: "+")
            .map { $0.trimmingCharacters(in: .whitespaces) }
        guard let keyPart = parts.last else { return nil }
        var mods: UInt32 = 0
        for p in parts.dropLast() {
            switch p {
            case "option", "alt": mods |= UInt32(optionKey)
            case "cmd", "command": mods |= UInt32(cmdKey)
            case "ctrl", "control": mods |= UInt32(controlKey)
            case "shift": mods |= UInt32(shiftKey)
            default: break
            }
        }
        guard mods != 0 else { return nil }
        let keyMap: [String: Int] = [
            "space": kVK_Space,
            "esc": kVK_Escape,
            "escape": kVK_Escape,
            "d": kVK_ANSI_D,
            "h": kVK_ANSI_H,
            "k": kVK_ANSI_K,
            "j": kVK_ANSI_J,
            "l": kVK_ANSI_L,
            "q": kVK_ANSI_Q,
            "return": kVK_Return,
            "enter": kVK_Return,
        ]
        guard let kc = keyMap[keyPart] else { return nil }
        return (UInt32(kc), mods)
    }
}
