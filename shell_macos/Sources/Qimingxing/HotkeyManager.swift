import Carbon
import Foundation

@MainActor
final class HotkeyManager {
    private var hotKeyRefs: [UInt32: EventHotKeyRef] = [:]
    private var handlerRef: EventHandlerRef?
    private var callbacks: [UInt32: @MainActor () -> Void] = [:]

    private static weak var sharedInstance: HotkeyManager?

    func registerDefaults(
        onQuickEntry: @escaping @MainActor () -> Void,
        onSettings: @escaping @MainActor () -> Void,
        onEmergencyStop: @escaping @MainActor () -> Void
    ) {
        unregisterAll()
        Self.sharedInstance = self
        installHandlerIfNeeded()

        register(id: 1, keyCode: UInt32(kVK_Space), modifiers: UInt32(optionKey), action: onQuickEntry)
        register(id: 2, keyCode: UInt32(kVK_ANSI_K), modifiers: UInt32(optionKey), action: onSettings)
        register(id: 3, keyCode: UInt32(kVK_Escape), modifiers: UInt32(optionKey), action: onEmergencyStop)
    }

    func updateFromStrings(
        chatInput: String,
        settings: String,
        cancel: String,
        onQuickEntry: @escaping @MainActor () -> Void,
        onSettings: @escaping @MainActor () -> Void,
        onEmergencyStop: @escaping @MainActor () -> Void
    ) {
        unregisterAll()
        Self.sharedInstance = self
        installHandlerIfNeeded()

        if let (kc, mods) = Self.parseHotkey(chatInput) {
            register(id: 1, keyCode: kc, modifiers: mods, action: onQuickEntry)
        } else {
            register(id: 1, keyCode: UInt32(kVK_Space), modifiers: UInt32(optionKey), action: onQuickEntry)
        }
        if let (kc, mods) = Self.parseHotkey(settings) {
            register(id: 2, keyCode: kc, modifiers: mods, action: onSettings)
        } else {
            register(id: 2, keyCode: UInt32(kVK_ANSI_K), modifiers: UInt32(optionKey), action: onSettings)
        }
        if let (kc, mods) = Self.parseHotkey(cancel) {
            register(id: 3, keyCode: kc, modifiers: mods, action: onEmergencyStop)
        } else {
            register(id: 3, keyCode: UInt32(kVK_Escape), modifiers: UInt32(optionKey), action: onEmergencyStop)
        }
    }

    func unregisterAll() {
        for (_, ref) in hotKeyRefs {
            UnregisterEventHotKey(ref)
        }
        hotKeyRefs.removeAll()
        callbacks.removeAll()
    }

    private func installHandlerIfNeeded() {
        guard handlerRef == nil else { return }
        var eventType = EventTypeSpec(
            eventClass: OSType(kEventClassKeyboard),
            eventKind: UInt32(kEventHotKeyPressed)
        )
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
                if status == noErr {
                    let targetID = hkID.id
                    Task { @MainActor in
                        HotkeyManager.sharedInstance?.callbacks[targetID]?()
                    }
                }
                return noErr
            },
            1,
            &eventType,
            nil,
            &handlerRef
        )
    }

    private func register(
        id: UInt32,
        keyCode: UInt32,
        modifiers: UInt32,
        action: @escaping @MainActor () -> Void
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
            callbacks[id] = action
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
