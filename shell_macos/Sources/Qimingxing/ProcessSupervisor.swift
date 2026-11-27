import AppKit
import Foundation

@MainActor
final class ProcessSupervisor: ObservableObject {
    @Published private(set) var brainRunning = false
    @Published private(set) var sensesRunning = false
    @Published private(set) var lastFatalError = ""

    let host: String
    let port: Int
    let appDir: URL
    let pythonBin: URL
    let dataDir: URL
    let logsDir: URL
    let configFile: URL
    let appVersion: String

    private var brainProcess: Process?
    private var sensesProcess: Process?
    private var isShuttingDown = false
    private var brainCrashCount = 0
    private var sensesCrashCount = 0

    var httpBase: String { "http://\(host):\(port)" }
    var wsURL: String { "ws://\(host):\(port)/ws" }

    init() {
        self.host = ProcessInfo.processInfo.environment["QIMINGXING_HOST"] ?? "127.0.0.1"
        self.port = Int(ProcessInfo.processInfo.environment["QIMINGXING_PORT"] ?? "18790") ?? 18790

        let fm = FileManager.default
        let appSupport = fm.urls(for: .applicationSupportDirectory, in: .userDomainMask).first!
        self.dataDir = appSupport.appendingPathComponent("Qimingxing", isDirectory: true)
        self.logsDir = dataDir.appendingPathComponent("logs", isDirectory: true)
        try? fm.createDirectory(at: dataDir.appendingPathComponent("data", isDirectory: true), withIntermediateDirectories: true)
        try? fm.createDirectory(at: logsDir, withIntermediateDirectories: true)

        if let resDir = Bundle.main.resourceURL,
           fm.fileExists(atPath: resDir.appendingPathComponent("app/brain/main.py").path) {
            self.appDir = resDir.appendingPathComponent("app", isDirectory: true)
            let bundledPy = resDir.appendingPathComponent("python/bin/python3.12")
            self.pythonBin = fm.isExecutableFile(atPath: bundledPy.path)
                ? bundledPy
                : resDir.appendingPathComponent("python/bin/python3")
            let cfgTarget = dataDir.appendingPathComponent("config.yaml")
            if !fm.fileExists(atPath: cfgTarget.path) {
                let example = appDir.appendingPathComponent("config.example.yaml")
                if fm.fileExists(atPath: example.path) {
                    try? fm.copyItem(at: example, to: cfgTarget)
                    try? fm.setAttributes([.posixPermissions: 0o600], ofItemAtPath: cfgTarget.path)
                }
            }
            self.configFile = cfgTarget
        } else {
            let repoRoot = Self.discoverDevRepoRoot()
            self.appDir = repoRoot
            let venvPy = repoRoot.appendingPathComponent(".venv/bin/python")
            self.pythonBin = fm.isExecutableFile(atPath: venvPy.path)
                ? venvPy
                : URL(fileURLWithPath: "/usr/bin/python3")
            let repoCfg = repoRoot.appendingPathComponent("config.yaml")
            self.configFile = fm.fileExists(atPath: repoCfg.path)
                ? repoCfg
                : dataDir.appendingPathComponent("config.yaml")
        }

        let versionURL = appDir.appendingPathComponent("version.txt")
        if let ver = try? String(contentsOf: versionURL, encoding: .utf8)
            .trimmingCharacters(in: .whitespacesAndNewlines),
           !ver.isEmpty {
            self.appVersion = ver
        } else {
            self.appVersion = (Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String) ?? "1.0.4"
        }
    }

    private static func discoverDevRepoRoot() -> URL {
        var candidates: [URL] = []
        if let envRoot = ProcessInfo.processInfo.environment["QIMINGXING_ROOT"] {
            candidates.append(URL(fileURLWithPath: envRoot))
        }
        candidates.append(URL(fileURLWithPath: FileManager.default.currentDirectoryPath))
        var cursor = Bundle.main.bundleURL
        for _ in 0..<8 {
            candidates.append(cursor)
            cursor = cursor.deletingLastPathComponent()
        }
        for c in candidates {
            if FileManager.default.fileExists(atPath: c.appendingPathComponent("brain/main.py").path) {
                return c
            }
        }
        return URL(fileURLWithPath: FileManager.default.currentDirectoryPath)
    }

    func startAll(onReady: @escaping @MainActor () -> Void) async {
        isShuttingDown = false
        let alreadyHealthy = await checkBrainHealth()
        if !alreadyHealthy {
            launchBrain()
            // Senses retries its WS connection on its own; starting it now
            // overlaps its cold import with Brain's Python startup instead of
            // adding them serially.
            launchSenses()
            let healthy = await waitForBrainHealthy(timeoutSeconds: 15.0)
            if !healthy {
                lastFatalError = "Brain 后端未能在 15 秒内启动就绪。"
                // Senses was started optimistically in parallel; stop it so it
                // does not retry forever behind a failed Brain.
                stopProcess(sensesProcess)
                sensesProcess = nil
                sensesRunning = false
                showCriticalFailureAlert(
                    title: "启明星启动失败",
                    message: "后端 Brain 未能就绪，请检查日志目录：\n\(logsDir.path)"
                )
                return
            }
        } else {
            brainRunning = true
            launchSenses()
        }

        onReady()
    }

    func terminateAll() {
        isShuttingDown = true

        stopProcess(sensesProcess)
        sensesProcess = nil
        sensesRunning = false

        stopProcess(brainProcess)
        brainProcess = nil
        brainRunning = false
    }

    private func baseEnvironment() -> [String: String] {
        var env = ProcessInfo.processInfo.environment
        env["QIMINGXING_CONFIG"] = env["QIMINGXING_CONFIG"] ?? configFile.path
        env["PYTHONPATH"] = appDir.path
        env["PYTHONNOUSERSITE"] = "1"
        // The app bundle is code-signed; never let Python write __pycache__
        // into Resources or the signature becomes invalid after first launch.
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["QIMINGXING_HOST"] = host
        env["QIMINGXING_PORT"] = String(port)
        env["QIMINGXING_HTTP_BASE"] = httpBase
        env["QIMINGXING_WS_URL"] = wsURL
        env["QIMINGXING_APP_VERSION"] = appVersion
        let curPath = env["PATH"] ?? "/usr/bin:/bin"
        env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:\(curPath)"
        return env
    }

    private func launchBrain() {
        guard !isShuttingDown else { return }
        if !FileManager.default.isExecutableFile(atPath: pythonBin.path) {
            lastFatalError = "未找到 Python 运行时: \(pythonBin.path)"
            return
        }
        let proc = Process()
        proc.executableURL = pythonBin
        proc.arguments = ["-m", "brain.main"]
        proc.currentDirectoryURL = appDir == Bundle.main.resourceURL?.appendingPathComponent("app") ? dataDir : appDir
        proc.environment = baseEnvironment()
        redirectOutput(proc: proc, logName: "brain.log")
        proc.terminationHandler = { [weak self] terminated in
            Task { @MainActor [weak self] in
                guard let self, !self.isShuttingDown else { return }
                self.brainRunning = false
                if self.brainProcess === terminated {
                    self.brainProcess = nil
                    self.brainCrashCount += 1
                    if self.brainCrashCount <= 3 {
                        let delay = pow(2.0, Double(self.brainCrashCount - 1))
                        try? await Task.sleep(nanoseconds: UInt64(delay * 1_000_000_000))
                        self.launchBrain()
                    } else {
                        self.showCriticalFailureAlert(
                            title: "Brain 进程连续异常退出",
                            message: "Brain 服务已连续 3 次异常退出，请查看日志排查：\n\(self.logsDir.path)"
                        )
                    }
                }
            }
        }
        do {
            try proc.run()
            brainProcess = proc
            brainRunning = true
        } catch {
            lastFatalError = "启动 Brain 失败: \(error.localizedDescription)"
        }
    }

    private func launchSenses() {
        guard !isShuttingDown else { return }
        guard FileManager.default.isExecutableFile(atPath: pythonBin.path) else { return }
        let proc = Process()
        proc.executableURL = pythonBin
        proc.arguments = ["-m", "senses.main", "--ws-url", wsURL, "--http-base", httpBase]
        proc.currentDirectoryURL = appDir == Bundle.main.resourceURL?.appendingPathComponent("app") ? dataDir : appDir
        proc.environment = baseEnvironment()
        redirectOutput(proc: proc, logName: "senses.log")
        proc.terminationHandler = { [weak self] terminated in
            Task { @MainActor [weak self] in
                guard let self, !self.isShuttingDown else { return }
                self.sensesRunning = false
                if self.sensesProcess === terminated {
                    self.sensesProcess = nil
                    self.sensesCrashCount += 1
                    if self.sensesCrashCount <= 3 {
                        let delay = pow(2.0, Double(self.sensesCrashCount - 1))
                        try? await Task.sleep(nanoseconds: UInt64(delay * 1_000_000_000))
                        self.launchSenses()
                    }
                }
            }
        }
        do {
            try proc.run()
            sensesProcess = proc
            sensesRunning = true
        } catch {
            NSLog("Failed to launch senses: \(error)")
        }
    }

    private func redirectOutput(proc: Process, logName: String) {
        let logURL = logsDir.appendingPathComponent(logName)
        FileManager.default.createFile(atPath: logURL.path, contents: nil)
        if let fh = try? FileHandle(forWritingTo: logURL) {
            fh.seekToEndOfFile()
            proc.standardOutput = fh
            proc.standardError = fh
        }
    }

    private func stopProcess(_ proc: Process?) {
        guard let proc, proc.isRunning else { return }
        proc.terminate()
        let deadline = Date().addingTimeInterval(1.5)
        while proc.isRunning && Date() < deadline {
            Thread.sleep(forTimeInterval: 0.05)
        }
        if proc.isRunning {
            kill(proc.processIdentifier, SIGKILL)
        }
    }

    func checkBrainHealth() async -> Bool {
        guard let url = URL(string: "\(httpBase)/health") else { return false }
        var req = URLRequest(url: url)
        req.timeoutInterval = 1.0
        do {
            let (_, resp) = try await URLSession.shared.data(for: req)
            if let http = resp as? HTTPURLResponse, http.statusCode == 200 {
                brainCrashCount = 0
                return true
            }
        } catch {}
        return false
    }

    private func waitForBrainHealthy(timeoutSeconds: TimeInterval) async -> Bool {
        let started = Date()
        let deadline = started.addingTimeInterval(timeoutSeconds)
        while Date() < deadline {
            if await checkBrainHealth() {
                brainRunning = true
                return true
            }
            if let p = brainProcess, !p.isRunning {
                return false
            }
            // Poll fast during the expected cold-start window, then back off.
            let interval: UInt64 = Date().timeIntervalSince(started) < 2.0
                ? 50_000_000
                : 250_000_000
            try? await Task.sleep(nanoseconds: interval)
        }
        return false
    }

    private func showCriticalFailureAlert(title: String, message: String) {
        let alert = NSAlert()
        alert.alertStyle = .critical
        alert.messageText = title
        alert.informativeText = message
        alert.addButton(withTitle: "打开日志目录")
        alert.addButton(withTitle: "关闭")
        NSApp.activate(ignoringOtherApps: true)
        let resp = alert.runModal()
        if resp == .alertFirstButtonReturn {
            NSWorkspace.shared.open(logsDir)
        }
    }
}
