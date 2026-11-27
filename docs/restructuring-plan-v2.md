# 启明星改造方案 v2（按四项拍板修订）

> 导出日期：2026-11-27  
> 状态：**方案，未开发**。基线仍是 `main@22ca135`，276 个 Python 测试通过，Swift 端 0 个 XCTest，无 CI。  
> 标记：`[现状]` 已验证；`[计划]` 未实现；`[决策]` 已定。

## 0. 四项决策后的最终边界

| 决策 | 最终落地 | 对上一版的影响 |
|---|---|---|
| `body_web` | **整体删除**，不保留 dev-only 调试台 | 删除静态路由、构建项、启动脚本、测试和当前文档引用 |
| `body_godot` | **迁到独立仓库**，主仓不再包含 GDScript | 删除启动器、`--with-godot`、设置开关；保留协议/配置兼容 |
| QuickEntry | **suspend 后恢复**，不销毁、不重建 | 新增 `WindowCoordinator`，统一设置/工作区/媒体预览的挂起与恢复 |
| 本地 HTTP | **不做 token**，高度信任本地进程 | 删除 token 方案；只保留零摩擦的浏览器来源门禁 |

**明确不做**：本地 HTTP token、登录/握手、Web 调试台、主仓 Godot 启动、App-Aware Sidecar 窗口、Rust 重写。

---

## 1. 删除 `body_web` 的前置条件：先补原生卡片渲染

这是本次方案里最重要的顺序调整。

`[现状]` `body_web` 不是纯调试台，它是目前**唯一消费 `output.card` 的客户端**：

- `protocol/models.py:370` 定义 `OutputCardPayload`；
- `brain/hub.py:3112` 发送 `output.card`；
- `body_web/app.js:852` 是唯一渲染入口；
- `shell_macos/Sources/Qimingxing/BrainClient.swift:334-672` 的 `switch` 没有 `output.card`；
- `StreamingEngine.swift:605` 遇到 `<<<CARD` 直接跳过；
- `QuickEntryPanel.swift:1204` 只在 `turn.media` 分支兜底卡片里的表格。

所以**现在直接删 `body_web`，所有卡片正文、标题和 `more_hint` 动作都会丢失**，只有卡内表格会被现有 `md-table` 媒体抢救下来。

### 1.1 原生卡片渲染 `[计划]`

1. `BrainClient` 增加 `case "output.card"`，解码 `OutputCardPayload`，挂到当前 `ChatEntry` 的 `cards` 数组。
2. 新增 `QMXOutputCardView`：标题、类型徽标、Markdown 正文、动作按钮。
3. `dismiss` 在本地折叠/展开；其余动作通过 `input.action` 回传 `card_id + action`，复用现有 `sendEnvelope`。
4. Live Line 在 `answerView` 与 media strip 之间渲染卡片；card-only 回答也要显示卡片，不能只显示媒体。
5. 卡片内表格与现有 `md-table` 媒体去重，避免同一张表出现两次。
6. 历史回放：至少确保不显示裸 `<<<CARD>>>` 标记；完整卡片重建可作为紧随其后的 P1。

### 1.2 验收

- 纯卡片回答可见；
- 卡片内 Markdown/表格可见；
- `dismiss` 折叠；
- `more_hint` 能发起下一轮；
- 深/浅色截图正常；
- 正文不出现 `<<<CARD>>>` 或 `<<<END_CARD>>>`。

**只有这组验收通过，才进入 `body_web` 删除。**

---

## 2. 安全边界：不做 token，但不再信任浏览器来源

按你的决定，**不引入 per-launch token，也不给本地进程加鉴权**。但 `body_web` 删除后，合法客户端只剩原生壳、Senses、外部 `body_godot`，它们都不是浏览器。因此可以用零 token、零交互的方式挡掉网页攻击面。

### 2.1 零摩擦门禁 `[计划]`

统一 HTTP middleware，不再逐路由判断 `request.client.host`：

1. **只绑定回环地址**：`127.0.0.1` / `::1`，不接受外部网卡。
2. **Host 白名单**：只允许 `127.0.0.1:<port>`、`localhost:<port>`、`[::1]:<port>`；`testserver` 仅测试模式；拒绝 `evil.com`，挡住 DNS rebinding。
3. **Origin 白名单**：空 Origin（原生客户端）或精确回环 Origin + 精确端口；拒绝 `Origin: null` 和所有外部网页。HTTP 与 WebSocket 同一套规则。
4. **写接口 Content-Type**：只接受 `application/json`；仅 `/api/artifacts` 上传允许 `multipart/form-data`；拒绝 `text/plain`、`application/x-www-form-urlencoded` 这类可绕过 preflight 的简单请求。
5. **不添加 CORS 响应头**，浏览器即使发出请求也读不到结果。

这套门禁对 Swift `URLSession`、Senses、Godot WebSocket 都是零感知；不增加 token，也不增加握手。

### 2.2 仍然要修的问题：它们不是“本地信任”能覆盖的

| 问题 | 修法 | 阶段 |
|---|---|---|
| `shell_tool` 只拦 `.ssh`/`id_rsa`/`.env`，`cat ~/.aws/credentials` 被判定 safe | 扩展凭据路径 denylist；输出改为流式截断，不再 `communicate()` 全量缓冲 | Phase 1 |
| 审批“记住”默认只按工具名，且全局共享 | 写操作按 `tool + canonical args/resource` 生成 scope；审批缓存按连接/会话隔离 | Phase 1 |
| `artifact_store` 未知 ID 用 `glob(f"{id}.*")` 并删除 | 严格 `^[A-Za-z0-9_-]{1,64}$`，解析后必须落在 artifact 根目录内 | Phase 1 |
| stdio MCP 子进程继承完整环境变量 | 改为最小环境变量 allowlist，不透传 API key | Phase 2/5 |
| MCP OAuth 目录基于 `__file__`，打包后写入签名 bundle | 改到 Application Support 数据目录 | Phase 2/5 |
| SQLite 多语句写入无 try/rollback | 显式事务上下文，异常回滚 | Phase 5 |
| `DesktopActions` 临时实例导致 QL delegate 悬空、通知 delegate 变 nil | 全 App 单实例注入；强持有 QL data source/delegate | Phase 3 |
| `CVDisplayLink` 回调 box 可能被提前释放 | retain/release 配对；stop 后延迟释放；deinit 清理 | Phase 4 |
| `ProcessSupervisor` 启动超时后 Brain 仍在跑、崩溃计数不重置 | 超时 kill；Senses/Brain 计数独立重置；停止不阻塞主线程 | Phase 4 |
| `BrainClient` 每次重连新建 `URLSession`，旧请求失败可能拆掉新连接 | 单 Session + 连接 generation；重连清空 per-message `isStreaming` | Phase 4 |
| 模型列表重复 ID 导致 `Dictionary(uniqueKeysWithValues:)` 崩溃 | 先去重再建字典 | Phase 3/6 |

### 2.3 明确接受的残余风险

写入 `SECURITY.md`：

- 同用户下的任意本地进程都可以调用 Brain API、安装/启动 MCP 命令、读取配置；
- 这是“高信任本地”的代价，本轮不防御；
- 如果以后要防同用户恶意进程，再引入 token 或 Unix socket peer credentials。

---

## 3. `body_web` 删除清单

### 3.1 代码与构建

- 删除 `body_web/` 整个目录。
- `brain/main.py:1018-1028` 删除 `/`、`/app.js`、`/styles.css` 三个静态路由；`/` 改为最小 JSON 服务描述，不保留任何 HTML/JS/CSS：

```json
{"service":"qimingxing","version":"1.0.4","status":"ok","ui":"native"}
```

- 清理 `FileResponse`、`_REPO_ROOT` 等不再使用的导入/常量。
- `brain/hub.py:538` 的默认 `client_type` 从 `body_web` 改为 `body_generic`；需要 `ui.state` 的客户端显式声明 `body_macos` / `body_windows` 或对应 capability。
- `scripts/build_macos_app.sh:274` 的 `APP_ITEMS` 移除 `body_web`。
- `qimingxing.sh` 移除自动 `open http://.../`、`--no-open`，启动完成只提示原生 App 或健康检查地址。

### 3.2 测试与文档

- `tests/test_privacy_and_senses.py:133-136`：静态三文件断言改为 `/` JSON 200、`/app.js` 404、`/styles.css` 404。
- `tests/test_protocol_v1_ga.py:60,258`：`client_name: body_web` 改为 `native_test`，并显式加 `client_type: body_macos`（否则 `ui_subscribed` 行为会变）。
- `README.md:21` 删除 `body_web` 条目；快速开始不再“浏览器打开”。
- `docs/native-shell-spec.md` 中“保留 body_web 调试台”的表述改为“原生壳为唯一 UI，历史 Web 控制台已移除”。
- 历史验收报告移到 `docs/archive/` 或加“历史文档”横幅，避免 AI 检索时当现状。

### 3.3 验收

- `body_web` 在产品代码、构建脚本、当前文档中零引用；
- 访问 `/app.js` 返回 404，`/` 返回 JSON；
- 打包产物不再包含 `Resources/app/body_web`；
- 276+ Python 测试通过；
- 原生卡片渲染验收通过。

---

## 4. `body_godot` 迁出主仓

### 4.1 先复制图标，不要先 `git mv`

当前 AppIcon 和 UI 预览都依赖 `body_godot/assets/sprites/happy.png`：

- `scripts/build_macos_app.sh:134`
- `UIPreviewDumper.swift:66,425`

先执行复制并提交：

```bash
mkdir -p assets
cp body_godot/assets/sprites/happy.png assets/app_icon.png
```

更新构建脚本和预览 fixture 指向 `assets/app_icon.png`。  
**不要先 `git mv`**，否则 `git subtree split` 出来的独立仓库会丢失头像精灵。

### 4.2 独立仓库 split

建议新仓库名 `qimingxing-avatar`（也可沿用 `body_godot`）：

```bash
git subtree split -P body_godot -b split/body-godot
git push <new-remote> split/body-godot:main
```

推送前确认 `body_godot` 工作区干净、历史完整。推送后在新仓库验证：

- `project.godot` 能独立打开；
- 头像、场景、脚本、资源完整；
- 能连接 `ws://127.0.0.1:18790/ws`；
- 若依赖 protocol schema，在新仓库内复制生成物并自行维护。

验证通过后，主仓执行 `git rm -r body_godot` 并单独提交。

### 4.3 主仓清理

- `scripts/build_macos_app.sh`：移除 `--with-godot`、`WITH_GODOT`、`GODOT_APP_SRC`、`APP_ITEMS+=(body_godot)`、可选 Godot 运行时复制。
- `ProcessSupervisor.swift`：移除 `godotRunning`、`godotProcess`、`isGodotPluginAvailable`、`launchOptionalGodot`、`syncAvatarEnabled`。
- `SettingsWindowController.swift:758`：移除“可选桌面头像插件”开关。
- `QimingxingApp.swift:234-236`：移除 `onAvatarEnabledChanged -> supervisor`。
- **同时移除 Settings 保存中的 `avatar_enabled` 写入**（`SettingsWindowController.swift:1587,1637`），避免主壳打开设置时把外部头像配置覆盖成默认 false。
- `qimingxing.sh`：移除 `GODOT_PID_FILE`、`find_godot_bin`、`start_godot`、状态/停止中的 Godot 分支、`--with-godot`。
- `README` 改为链接独立仓库，说明外部头像自行启动。

### 4.4 保留的兼容面

- 保留 protocol 中的 `body_godot` client type；
- 保留 `brain/config.py` 的 `godot` 配置和 `ui.avatar_enabled`；
- 保留 `tests/test_audio_pipeline.py` 等以 `body_godot` 字符串验证协议兼容的测试；
- 主仓不再启动、打包、管理 Godot 进程。

### 4.5 验收

- 主仓无 `*.gd`、`project.godot`、`body_godot/`；
- 构建脚本无 Godot 参数；
- 独立仓库可独立运行并连接 Brain；
- 外部头像连接/断开不影响原生壳焦点；
- 现有 Godot 协议兼容测试仍通过。

---

## 5. 窗口/焦点仲裁与 suspend/restore

### 5.1 现状问题

`[现状]` 当前至少有 10 个独立窗口控制器，且焦点操作散落：

- `NSApp.activate(ignoringOtherApps:)` 出现在 `DesktopActions`、`SettingsWindowController`、`MediaViewer`、`QuickEntryPanel`、`ApprovalSheetController`、`WorkspaceWindowController`、`SpatialLassoController`、`ProcessSupervisor` 等 8+ 处；
- `orderFrontRegardless()` 出现在 `DesktopActions`、`WalkieTalkie`、`TaskToast`、`DropPocket`、`PermissionGuide`；
- `QuickEntryPanel` 只有 `hiddenForMediaViewer` 一个特例，设置和工作区没有统一挂起；
- 关闭回调里还有 `DispatchQueue.asyncAfter` 直接恢复，容易和下一次打开竞争。

### 5.2 角色与优先级

| 组 | 角色 | 规则 |
|---|---|---|
| passive | `taskToast`、`sidecarSuggestion` | 不抢 key、不 `NSApp.activate`、不挂起其他窗口 |
| transient | `dropPocket`、`spatialLasso`、`walkieTalkie` | 非激活浮层；不改变 key owner |
| content | `quickEntry`、`workspaceWing`、`workspaceWindow`、`mediaAttached`、`mediaDetached`、`settings` | 同一时刻只有一个 key owner；大内容面打开时挂起 QuickEntry |
| modal | `approval` | 高于 content；不挂起 QuickEntry，只接管 key，关闭后归还 |
| security | `permissionGuide` | 最高优先级；不抢焦点，关闭后归还 |

优先级：`passive 0 < transient 10 < content 20 < modal 30 < security 40`。

### 5.3 核心 API

```swift
enum WindowRole { ... }
enum WindowPriority: Int { ... }
enum ActivationPolicy { case none, required, restorePrevious }

struct WindowRequest {
    let role: WindowRole
    let reason: String
    let activation: ActivationPolicy
    let exclusiveGroup: String?
}

struct WindowLease {
    let role: WindowRole
    let generation: UInt64
}

@MainActor
final class WindowCoordinator {
    func present(_ request: WindowRequest) -> WindowLease
    func dismiss(_ lease: WindowLease)
    func suspend(_ role: WindowRole, reason: String)
    func resume(_ role: WindowRole)
    var keyOwner: WindowRole? { get }
}
```

`WindowLease` 带 generation，用来忽略异步/延迟关闭回调，避免旧 `mediaViewerClosed` 把新打开的窗口关掉或抢走焦点。

配套：

- `FocusArbitrator`：唯一允许调用 `NSApp.activate` 的组件；记录激活前的 `NSRunningApplication`，负责归还焦点。
- `HotkeyRouter`：统一 UI 全局热键；PTT 仍留在 Senses，不迁入。
- `PanelFactory`：统一 `NSPanel` 的 `styleMask`、`level`、`collectionBehavior`、`hidesOnDeactivate`、`becomesKeyOnlyIfNeeded`。

### 5.4 suspend/restore 矩阵

| 场景 | QuickEntry 行为 | 关闭后 |
|---|---|---|
| 打开 Workspace Wing | 不挂起，wing 是同一面板的一部分 | 收 wing，不重建面板 |
| 打开完整 Workspace 窗口 | **挂起**：`orderOut`，保留 frame/draft/焦点状态 | 若挂起前可见且 App 仍在前台，恢复并聚焦输入框 |
| 打开 Settings | **挂起** | 同上；若用户已切到别的 App，不抢焦点 |
| 打开 detached Media Viewer | **挂起** | 同上；复用现有 250ms 延迟但走 lease generation |
| 打开 Approval | 不挂起；QuickEntry 保留但让出 key | 归还 key 给之前 owner |
| 打开 Permission Guide | 不挂起；security 优先 | 归还 key |
| Task Toast / Drop Pocket / Lasso / Walkie | 不挂起 | 不改变 key |
| QuickEntry 热键 | 切换 QuickEntry；若被挂起则恢复并置于当前内容之上 | 关闭后 key 回到原内容；审批/权限存在时不抢 |

**焦点归还规则**：

1. 激活任何窗口前，`FocusArbitrator` 记录当前前台外部 App。
2. 只有 `ActivationPolicy.required` 才调用 `NSApp.activate`；passive/transient 只 `orderFrontRegardless`。
3. 关闭时优先把 key 还给 lease 栈顶；若 QuickEntry 是挂起前可见的，恢复它。
4. 若用户已切换到其他 App，或当前前台不是启明星，**不激活、不抢焦点**。
5. 所有直接 `NSApp.activate` / `makeKeyAndOrderFront` 从控制器移除，只保留在 coordinator/factory。

### 5.5 迁移顺序

1. 引入 `WindowCoordinator` + `FocusArbitrator` + `PanelFactory` 骨架，先注册 QuickEntry、Settings、Workspace、Media，不改变行为。
2. 加 suspend/restore，先覆盖 Settings 和 detached Media。
3. 覆盖完整 Workspace；Wing 保持不挂起。
4. 迁移 Approval、Permission、Toast、DropPocket、Lasso、Walkie。
5. 删除各控制器里的直接激活和 `hiddenForMediaViewer` 特例。

### 5.6 Sidecar

本轮不做。等 `WindowCoordinator` 至少稳定一个发布周期后再做：

- Sidecar 只作为服务/意图，不新增窗口；
- 提示走 `TaskToast`；
- 若确实需要面板，使用 `.sidecarSuggestion` role，经 coordinator 统一调度；
- 没有 coordinator 不开发 Sidecar。

---

## 6. 测试与 CI

### 6.1 Swift 测试目标

当前 `shell_macos/Package.swift` 只有单个 executable target，没有测试 target。建议拆成：

```text
Sources/QimingxingCore/     # 可测试状态机/策略/模型
Sources/QimingxingUI/       # SwiftUI/AppKit 视图与服务
Sources/Qimingxing/         # App 入口与依赖装配
Tools/UIPreviewDumper/      # 开发期截图工具，不再进生产 target
Tests/QimingxingCoreTests/
```

首期 80–120 个 XCTest，覆盖：

- `WindowCoordinator`：present/dismiss 优先级、lease generation、suspend/restore、key 归还；
- `FocusArbitrator`：外部 App 记录、切走不抢焦点、嵌套 modal；
- `HotkeyRouter`：命令映射、审批期间行为、无重复注册；
- `StreamingBlockParser` / `StreamingRevealer`：代码围栏、表格、卡片标记、半截 token、CJK、结束后 flush；
- `GhostWriteEngine`：idle/capturing/writing/done/failed、取消、超时、目标 App 切换；
- `MarkdownTable`：转义管道、代码 span、15×100 上限、TSV；
- `ReconnectPolicy`：指数退避、上限、welcome 后重置；
- `ProcessSupervisor`：启动超时、崩溃计数、停止幂等（注入 fake process/clock）；
- `ApprovalQueue`：去重、顺序、resolved；
- `MediaViewerModel`：表格大窗口条件、关闭通知。

`UIPreviewDumper.swift` 当前 1,342 行，移到 `Tools/UIPreviewDumper/`，保留 `--dump-ui` 深/浅色截图，但不再作为唯一 UI 验收手段。

### 6.2 Python 测试

- 增加 `pytest-timeout`、`pytest-cov`；
- 标记 `macos`、`tcc`、`chrome`、`slow`；
- 真实 TCC/Chrome 测试默认 skip，CI 用 fake；
- 覆盖率先 report-only，基线稳定后设 70% gate；
- 现有 276 个测试不得减少。

### 6.3 CI

新增 `.github/workflows/ci.yml`：

- `python-tests`：macOS runner，`pytest -q --timeout=60 --cov=brain,senses --cov-report=term-missing`；
- `swift-build`：`swift build -c release`；
- `swift-tests`：`swift test`；
- `ui-snapshots`：运行 `UIPreviewDumper --dump-ui`，上传深/浅色截图 artifact；
- `package-smoke`：`scripts/build_macos_app.sh` + `codesign --verify --deep --strict`；
- `x86_64` 构建作为 nightly/手动 job，不阻塞每个 PR。

---

## 7. 巨石代码拆分

### 7.1 Python 目标结构

```text
brain/
  app/routes/{health,settings,mcp,tasks,artifacts,desktop,feedback}.py
  hub/{connection,turns,approvals,media,ui_state,routing}.py
  worker/{runner,approval_bridge,artifact_bundle,watchdog_integration}.py
  llm/{router,streaming,providers/}
  tools/{registry,executor,builtins/...}
  memory/{backend,sqlite/...}
  rich/{sources,tables,media}.py
  config/{models,loader,writer,secrets}.py
  browser/{runtime,security,routes,downloads}.py
```

顺序：

1. `main.py` 路由拆分（低风险，先做）；
2. `rich_content` 拆 `rich/`；
3. `hub` 拆连接/回合/审批/媒体/ui_state；
4. `worker`、`llm`、`tools`、`memory`；
5. `config`、`browser` 最后。

### 7.2 Swift 目标结构

```text
Sources/Qimingxing/
  App/
  Core/Window/{WindowCoordinator,FocusArbitrator,HotkeyRouter,PanelFactory,WindowRole}.swift
  Core/Transport/{BrainClient,WebSocketTransport,ReconnectPolicy,MessageDecoding}.swift
  Core/Process/ProcessSupervisor.swift
  Core/Services/{DesktopActions,NotificationService,PermissionService,MediaCache}.swift
  Features/{QuickEntry,Workspace,Media,Settings,Approval,Interactions}/...
  UI/DesignSystem/...
Tools/UIPreviewDumper/
```

重点拆分：

- `QuickEntryPanel.swift` 2,548 行：面板、布局、Live Line、Composer、媒体侧翼分离；
- `StreamingEngine.swift` 1,288 行：解析器、揭示器、滚动驱动分离；
- `UIPreviewDumper.swift` 1,342 行：移出生产 target；
- `WorkspaceWindowController`、`MediaViewer`、`SettingsWindowController` 按 Feature 拆。

### 7.3 拆分纪律

- 每个 PR 只搬一个域；`git mv` + re-export，**不混行为修改**；
- 公共 import 保持兼容，例如 `brain.hub.Hub` 继续可用；
- 每个 PR 结束 Python 276+、Swift 全绿、UI 截图无回归；
- 文件大小基线：`hub.py 3,135`、`QuickEntryPanel.swift 2,548`、`UIPreviewDumper.swift 1,342`；存量只降不升，新增文件 >800 行需拆分；
- `Package.swift` 的 `-disable-dynamic-actor-isolation` / `-disable-actor-data-race-checks` 在并发问题修复后移除，不作为长期配置。

---

## 8. 实施顺序与回滚

| 阶段 | 内容 | 出口条件 |
|---|---|---|
| Phase 0 | 处理未跟踪文档；打 `pre-restructure-2026-11-27` tag；建立 CI 骨架 | 工作区干净，基线可回滚 |
| Phase 1a | 原生 `output.card` 渲染 + XCTest | 卡片/动作/纯卡片回答通过 |
| Phase 1b | 删除 `body_web`，`/` 改 JSON，更新构建/脚本/测试/文档 | `body_web` 零引用，测试全绿 |
| Phase 1c | HTTP Host/Origin/Content-Type 门禁 + 路径/审批/artifact 修复 | 跨站请求 403，原生客户端无感 |
| Phase 2 | `body_godot` 图标复制、subtree split、主仓删除、外部仓库验证 | 主仓无 GDScript，外部头像可连 |
| Phase 3 | `WindowCoordinator` + suspend/restore + 焦点矩阵测试 | 设置/工作区/媒体关闭后正确恢复 |
| Phase 4 | Swift 测试 target、core 抽取、`UIPreviewDumper` 移出、Streaming/GhostWrite 测试 | 80–120 Swift tests，CI 绿 |
| Phase 5 | Python 巨石拆分（按域逐个 PR） | 测试数不降，文件大小下降 |
| Phase 6 | Swift Feature 拆分、移除 unsafe concurrency flags | 构建/测试/截图全绿 |
| Phase 7 | CI 加固、打包/codesign/性能验收 | 发布包可验证 |
| Phase 8 | App-Aware Sidecar | coordinator 稳定一个版本后再评估 |

每个阶段独立 PR，不混合提交。`body_web` 删除和 `body_godot` 迁出必须是两个独立 PR，方便单独回滚。

---

## 9. 总验收标准

| 维度 | 标准 |
|---|---|
| 遗留代码 | 产品代码/构建脚本无 `body_web`、`body_godot`；历史文档归档；`ARCHITECTURE.md`/`AGENTS.md` 明确产品/外部/dev-only |
| 测试 | Python ≥276；Swift 80–120；无新增长期 skip；CI 全绿 |
| 安全 | 外部 Origin、`Origin: null`、非法 Host、`text/plain` POST 均 403/415；原生 WS/HTTP 正常 |
| 窗口 | QuickEntry→Settings/Workspace/Media 关闭后恢复 frame/draft/焦点；切走 App 不抢焦点；审批/权限无死锁 |
| 卡片 | 标题/正文/动作/纯卡片回答/卡内表格全部正常，无裸标记 |
| UI | `--dump-ui` 深/浅色截图无回归；表格、媒体、Ghost Write 手动点测 |
| 打包 | `dist/Qimingxing.app` 构建成功，`codesign --verify --deep --strict` 通过 |
| 性能 | 启动/内存记录基线，重构 PR 不引入 >10% 回退 |

---

## 10. 风险与后果

- **删除 `body_web` 会失去浏览器调试台**：替代是 `/` JSON、日志、`--dump-ui` 截图和原生 Workspace；这是你已拍板的取舍。
- **Windows 端没有原生壳**：删除 `body_web` 后，Windows 只剩 Senses，无可视 UI；若以后需要，另立客户端仓库，不回流主仓。
- **卡片渲染是删除 `body_web` 的硬前置**：不做就会丢卡片正文，不能先删后补。
- **`body_godot` 独立仓库存在版本偏移风险**：靠保留 `body_godot` client type、`godot` 配置和协议兼容测试控制。
- **WindowCoordinator 会改变焦点行为**：必须用矩阵测试锁定；每个迁移 PR 可单独回滚。
- **Swift target 拆分可能影响打包脚本**：每步都跑 `swift build` + 打包 smoke，不一次性大搬迁。
- **高信任本地的残余风险已接受**：同用户恶意进程不在本轮防御范围，写入 `SECURITY.md`。
