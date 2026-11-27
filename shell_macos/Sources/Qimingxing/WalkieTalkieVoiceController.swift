import AppKit
import AVFoundation
import Speech
import SwiftUI

// MARK: - Non-Activating Walkie-Talkie Capsule Panel

@MainActor
final class WalkieTalkieCapsulePanel: NSPanel {
    override var canBecomeKey: Bool { false }
    override var canBecomeMain: Bool { false }
}

private final class AudioRecognitionBridge: @unchecked Sendable {
    let request: SFSpeechAudioBufferRecognitionRequest
    let onLevel: @Sendable (Float) -> Void

    init(
        request: SFSpeechAudioBufferRecognitionRequest,
        onLevel: @escaping @Sendable (Float) -> Void
    ) {
        self.request = request
        self.onLevel = onLevel
    }

    func handleAudioBuffer(_ buffer: AVAudioPCMBuffer) {
        request.append(buffer)
        guard let channelData = buffer.floatChannelData?[0], buffer.frameLength > 0 else { return }
        let count = Int(buffer.frameLength)
        var sumSq: Float = 0
        for i in 0..<count {
            let s = channelData[i]
            sumSq += s * s
        }
        let rms = sqrt(sumSq / Float(count))
        let normalized = min(1.0, max(0.0, rms * 14.0))
        onLevel(normalized)
    }
}

// MARK: - Walkie-Talkie Voice Controller

@MainActor
final class WalkieTalkieVoiceController: ObservableObject {
    @Published private(set) var isRecording: Bool = false
    @Published private(set) var transcript: String = ""
    @Published private(set) var statusText: String = "正在聆听… 松开发送 · Esc 取消"
    @Published private(set) var audioLevel: CGFloat = 0

    var onTranscriptReady: ((String) -> Void)?
    var onCancelled: (() -> Void)?

    private var panel: WalkieTalkieCapsulePanel?
    private var audioEngine: AVAudioEngine?
    private var speechRecognizer: SFSpeechRecognizer?
    private var recognitionRequest: SFSpeechAudioBufferRecognitionRequest?
    private var recognitionTask: SFSpeechRecognitionTask?
    private var submitDelayTask: Task<Void, Never>?
    private var isClickMode = false

    private nonisolated(unsafe) var localEscMonitor: Any?
    private nonisolated(unsafe) var globalEscMonitor: Any?

    deinit {
        if let localEscMonitor { NSEvent.removeMonitor(localEscMonitor) }
        if let globalEscMonitor { NSEvent.removeMonitor(globalEscMonitor) }
    }

    func startHoldRecording() {
        isClickMode = false
        beginSession()
    }

    func toggleClickRecording() {
        if isRecording {
            endHoldAndSubmit()
        } else {
            isClickMode = true
            statusText = "正在聆听… 再次点击或按 Esc"
            beginSession()
        }
    }

    func endHoldAndSubmit() {
        guard isRecording else { return }
        stopAudioCaptureOnly()
        isRecording = false

        submitDelayTask?.cancel()
        submitDelayTask = Task { [weak self] in
            try? await Task.sleep(nanoseconds: 160_000_000)
            guard let self else { return }
            let final = self.transcript.trimmingCharacters(in: .whitespacesAndNewlines)
            self.teardownRecognitionAndPanel()
            if final.isEmpty {
                TaskToastController.shared.show(
                    title: "未识别到语音内容",
                    message: "请检查麦克风输入后重试",
                    style: .warning,
                    actionTitle: nil,
                    route: .none,
                    mirrorToSystem: false
                ) {}
            } else {
                self.onTranscriptReady?(final)
            }
        }
    }

    func cancelRecording() {
        submitDelayTask?.cancel()
        submitDelayTask = nil
        stopAudioCaptureOnly()
        isRecording = false
        teardownRecognitionAndPanel()
        onCancelled?()
    }

    func configureForPreview(transcript: String, level: CGFloat) {
        self.transcript = transcript
        self.audioLevel = level
        self.isRecording = true
    }

    // MARK: - Private Audio + Speech Pipeline

    private func beginSession() {
        guard !isRecording else { return }
        submitDelayTask?.cancel()
        submitDelayTask = nil
        transcript = ""
        audioLevel = 0
        if !isClickMode {
            statusText = "正在聆听… 松开发送 · Esc 取消"
        }

        isRecording = true
        showCapsulePanel()
        installEscMonitors()

        Task { [weak self] in
            guard let self else { return }
            let ok = await self.ensurePermissions()
            guard ok else {
                self.cancelRecording()
                return
            }
            guard self.isRecording else { return }
            self.startAudioAndSpeechEngine()
        }
    }

    private func ensurePermissions() async -> Bool {
        let micStatus = AVCaptureDevice.authorizationStatus(for: .audio)
        var micGranted = (micStatus == .authorized)
        if micStatus == .notDetermined {
            micGranted = await AVCaptureDevice.requestAccess(for: .audio)
        }
        guard micGranted else {
            let prefsURL = "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone"
            TaskToastController.shared.show(
                title: "需要麦克风权限",
                message: "请在系统设置 → 隐私与安全性 → 麦克风 中允许启明星",
                style: .error,
                actionTitle: "打开设置",
                route: .systemURL(prefsURL)
            ) {
                if let url = URL(string: prefsURL) {
                    NSWorkspace.shared.open(url)
                }
            }
            return false
        }

        let speechStatus = SFSpeechRecognizer.authorizationStatus()
        var speechGranted = (speechStatus == .authorized)
        if speechStatus == .notDetermined {
            let status = await withCheckedContinuation { continuation in
                SFSpeechRecognizer.requestAuthorization { authStatus in
                    continuation.resume(returning: authStatus)
                }
            }
            speechGranted = (status == .authorized)
        }
        guard speechGranted else {
            let prefsURL = "x-apple.systempreferences:com.apple.preference.security?Privacy_SpeechRecognition"
            TaskToastController.shared.show(
                title: "需要语音识别权限",
                message: "请在系统设置 → 隐私与安全性 → 语音识别 中允许启明星",
                style: .error,
                actionTitle: "打开设置",
                route: .systemURL(prefsURL)
            ) {
                if let url = URL(string: prefsURL) {
                    NSWorkspace.shared.open(url)
                }
            }
            return false
        }
        return true
    }

    private func startAudioAndSpeechEngine() {
        let recognizer = SFSpeechRecognizer(locale: Locale(identifier: "zh-CN")) ?? SFSpeechRecognizer()
        guard let recognizer, recognizer.isAvailable else {
            statusText = "语音识别服务暂不可用"
            return
        }
        speechRecognizer = recognizer

        let request = SFSpeechAudioBufferRecognitionRequest()
        request.shouldReportPartialResults = true
        if recognizer.supportsOnDeviceRecognition {
            request.requiresOnDeviceRecognition = true
        }
        recognitionRequest = request

        let engine = AVAudioEngine()
        let inputNode = engine.inputNode
        let format = inputNode.outputFormat(forBus: 0)
        guard format.channelCount > 0, format.sampleRate > 0 else {
            statusText = "未检测到可用麦克风设备"
            return
        }

        let bridge = AudioRecognitionBridge(request: request) { [weak self] level in
            Task { @MainActor [weak self] in
                guard let self, self.isRecording else { return }
                self.audioLevel = self.audioLevel * 0.45 + CGFloat(level) * 0.55
            }
        }
        inputNode.installTap(onBus: 0, bufferSize: 1024, format: format) { buffer, _ in
            bridge.handleAudioBuffer(buffer)
        }

        do {
            engine.prepare()
            try engine.start()
            audioEngine = engine
        } catch {
            inputNode.removeTap(onBus: 0)
            statusText = "无法启动音频采集：\(error.localizedDescription)"
            return
        }

        recognitionTask = recognizer.recognitionTask(with: request) { [weak self] result, error in
            let text = result?.bestTranscription.formattedString
            let isFinal = result?.isFinal ?? false
            let hasError = error != nil
            Task { @MainActor [weak self] in
                guard let self else { return }
                if let text, !text.isEmpty {
                    self.transcript = text
                }
                if isFinal || hasError {
                    self.recognitionTask = nil
                }
            }
        }
    }

    private func stopAudioCaptureOnly() {
        audioEngine?.stop()
        audioEngine?.inputNode.removeTap(onBus: 0)
        audioEngine = nil
        recognitionRequest?.endAudio()
    }

    private func teardownRecognitionAndPanel() {
        recognitionTask?.cancel()
        recognitionTask = nil
        recognitionRequest = nil
        speechRecognizer = nil
        audioLevel = 0
        removeEscMonitors()
        panel?.orderOut(nil)
    }

    private func installEscMonitors() {
        removeEscMonitors()
        localEscMonitor = NSEvent.addLocalMonitorForEvents(matching: .keyDown) { [weak self] event in
            if event.keyCode == 53 {
                Task { @MainActor [weak self] in
                    self?.cancelRecording()
                }
                return nil
            }
            return event
        }
        globalEscMonitor = NSEvent.addGlobalMonitorForEvents(matching: .keyDown) { [weak self] event in
            if event.keyCode == 53 {
                Task { @MainActor [weak self] in
                    self?.cancelRecording()
                }
            }
        }
    }

    private func removeEscMonitors() {
        if let localEscMonitor {
            NSEvent.removeMonitor(localEscMonitor)
            self.localEscMonitor = nil
        }
        if let globalEscMonitor {
            NSEvent.removeMonitor(globalEscMonitor)
            self.globalEscMonitor = nil
        }
    }

    // MARK: - Floating Capsule Panel

    private func showCapsulePanel() {
        let size = NSSize(width: 360, height: 58)
        if panel == nil {
            let p = WalkieTalkieCapsulePanel(
                contentRect: NSRect(origin: .zero, size: size),
                styleMask: [.nonactivatingPanel, .borderless],
                backing: .buffered,
                defer: false
            )
            p.title = "对讲机语音"
            p.isFloatingPanel = true
            p.level = .statusBar
            p.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .ignoresCycle]
            p.isMovableByWindowBackground = false
            p.hidesOnDeactivate = false
            p.isOpaque = false
            p.backgroundColor = .clear
            p.hasShadow = true
            p.appearance = NSAppearance(named: .darkAqua)

            let view = WalkieTalkieCapsuleView(
                controller: self,
                onCancel: { [weak self] in self?.cancelRecording() },
                onDone: { [weak self] in self?.endHoldAndSubmit() }
            )
            let hosting = NSHostingController(rootView: view)
            hosting.sizingOptions = []
            if #available(macOS 13.3, *) {
                hosting.safeAreaRegions = []
            }
            hosting.view.wantsLayer = true
            hosting.view.layer?.backgroundColor = NSColor.clear.cgColor
            hosting.view.frame = NSRect(origin: .zero, size: size)
            p.contentView = hosting.view
            p.contentViewController = hosting
            panel = p
        }

        guard let panel else { return }
        let mouse = NSEvent.mouseLocation
        let screen = NSScreen.screens.first(where: { NSMouseInRect(mouse, $0.frame, false) }) ?? NSScreen.main
        let visible = screen?.visibleFrame ?? NSRect(x: 0, y: 0, width: 1440, height: 900)
        let origin = NSPoint(
            x: visible.midX - size.width / 2,
            y: visible.minY + 48
        )
        panel.setFrame(NSRect(origin: origin, size: size), display: true)
        panel.orderFrontRegardless()
    }
}

// MARK: - SwiftUI Capsule View

struct WalkieTalkieCapsuleView: View {
    @ObservedObject var controller: WalkieTalkieVoiceController
    let onCancel: () -> Void
    var onDone: (() -> Void)? = nil
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    private let barScales: [CGFloat] = [0.45, 0.80, 1.0, 0.72, 0.50]

    var body: some View {
        let shape = RoundedRectangle(cornerRadius: 29, style: .continuous)
        ZStack {
            if !QMXTheme.isHeadlessDump {
                VisualEffectBlur(
                    material: .hudWindow,
                    blendingMode: .behindWindow,
                    state: .active,
                    cornerRadius: 0
                )
            }
            Color(red: 0.067, green: 0.078, blue: 0.102)
                .opacity(QMXTheme.isHeadlessDump ? 0.94 : 0.84)

            HStack(spacing: 12) {
                waveformIndicator

                Group {
                    if controller.transcript.isEmpty {
                        Text(controller.statusText)
                            .foregroundStyle(Color.white.opacity(0.65))
                    } else {
                        Text(controller.transcript)
                            .foregroundStyle(Color.white.opacity(0.95))
                    }
                }
                .font(.system(size: 13, weight: .medium))
                .lineLimit(2)
                .multilineTextAlignment(.leading)
                .frame(maxWidth: .infinity, alignment: .leading)

                if onDone != nil && !controller.transcript.isEmpty {
                    Button(action: { onDone?() }) {
                        Image(systemName: "arrow.up.circle.fill")
                            .font(.system(size: 18))
                            .foregroundStyle(Color.white)
                    }
                    .buttonStyle(.plain)
                    .help("立即发送")
                }

                Button(action: onCancel) {
                    Image(systemName: "xmark")
                        .font(.system(size: 10, weight: .semibold))
                        .foregroundStyle(Color.white.opacity(0.65))
                        .frame(width: 22, height: 22)
                        .background(Color.white.opacity(0.10), in: Circle())
                }
                .buttonStyle(.plain)
                .help("取消 (Esc)")
            }
            .padding(.horizontal, 16)
        }
        .frame(width: 360, height: 58)
        .clipShape(shape)
        .overlay(
            AmbientRimLight(isActive: true, cornerRadius: 29)
        )
        .overlay(
            shape.strokeBorder(Color.white.opacity(0.14), lineWidth: 0.5)
        )
        .environment(\.colorScheme, .dark)
    }

    private var waveformIndicator: some View {
        let level = reduceMotion ? 0.45 : max(0.18, controller.audioLevel)
        return HStack(spacing: 3) {
            ForEach(0..<barScales.count, id: \.self) { idx in
                let height = max(5, 22 * barScales[idx] * level)
                Capsule()
                    .fill(Color(red: 0.36, green: 0.88, blue: 1.0))
                    .frame(width: 3, height: height)
                    .animation(reduceMotion ? nil : .easeOut(duration: 0.08), value: controller.audioLevel)
            }
        }
        .frame(width: 28, height: 24)
    }
}
