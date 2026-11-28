# 启明星改造方案 v2.1（review 修正版）

> 导出：2026-11-27（v2）／修正：2026-11-27（v2.1）  
> 状态：**方案定稿，未开发**。基线 `main@22ca135`（tag `pre-restructure-2026-11-27`）：276 个 Python 测试、0 个 Swift XCTest、无 CI。  
> 本轮范围：**Phase 0 / 1a / 1b / 1c**（v1.1）。Phase 2 单独排期；Phase 3–8 仅作路线图。  
> 比对口径：`docs/restructure-metrics.md`；机械指标由 `scripts/metrics_report.py` 复现。  
> 标记：`[现状]` 已验证（含 file:line）；`[计划]` 未实现；`[决策]` 已定；`[待定]` 明确未决。

## 修订说明（v2.1）

v2 的 28 处事实偏差已按代码逐条核查修正。会改变工作量的 5 条：

1. **不存在历史回放**：shell 不读 SQLite，`BrainClient` 只有内存 400 条（`BrainClient.swift:742-748`）；brain 无历史 API。唯一跨启动持久面是 shell 自己的 `workspace.json`（`WorkspaceWindowController.swift:77-135`），而 `WorkspaceItem` 没有 cards 字段（`:29-43`）。§1.1 的"历史回放"改为"持久化 + 侧栏回放"。
2. **card-only 回合完全不落库**：`hub.py:2888` 只在 `assistant_reply` 非空时写 assistant 行，纯卡片回答在 SQLite 里没有记录，也不进下一轮上下文。
3. **`artifact_store.delete` 是路径穿越删除**：`Path.glob(f"{id}.*")` 接受 `..`（`artifact_store.py:263`），而 `artifact_id` 可从 WS 直达（`hub.py:648-653`）。基线探针已复现，见 `docs/restructure-metrics.md`。
4. **窗口不是 10 个而是 12+**，QuickEntry 有 5 个焦点特例而不是 1 个（§5.1）。
5. **`--dump-ui` 含真实面板路径**（`UIPreviewDumper.swift:576,631`），不能直接在 CI 跑。

其余按组：卡片管线 7 条、安全 6 条、构建/文档 7 条，逐条见各节 `[修正]`。

---

## 0. 最终边界

### 0.1 v2 四项拍板（不变）

| 决策 | 最终落地 | 对上一版的影响 |
|---|---|---|
| `body_web` | **整体删除**，不保留 dev-only 调试台 | 删除静态路由、构建项、启动脚本、测试和当前文档引用 |
| `body_godot` | **迁到独立仓库**，主仓不再包含 GDScript | 删除启动器、`--with-godot`、设置开关；保留协议/配置兼容 |
| QuickEntry | **suspend 后恢复**，不销毁、不重建 | 新增 `WindowCoordinator`，统一设置/工作区/媒体预览的挂起与恢复 |
| 本地 HTTP | **不做 token**，高度信任本地进程 | 删除 token 方案；只保留零摩擦的浏览器来源门禁 |

**明确不做**：本地 HTTP token、登录/握手、Web 调试台、主仓 Godot 启动、App-Aware Sidecar 窗口、Rust 重写。

### 0.2 v2.1 补充决策

| 决策 | 结论 |
|---|---|
| v1.1 范围 | Phase 0 / 1a / 1b / 1c；Phase 2 单独排期 |
| 计划载体 | epic + 子 issue；本文档为背景与决策记录 |
| Swift 测试 | Phase 0 建 `QimingxingCore` + `QimingxingCoreTests` |
| 卡片动作 | 动作=新回合；运行期 `card_id → 允许动作` registry；未知动作忽略，绝不拼进 prompt |
| 卡片持久化 | assistant `content` 写卡片正文 + `cards_json` 列；`WorkspaceItem.cards` |
| 表格去重 | 客户端内容哈希，卡片优先 |
| 裸标记 | 1a 一并修（渲染层 + 流式未闭合） |
| shell 权限 | 默认拒绝：工作区外审批，凭据目录硬拒绝 |
| 绑定地址 | 删除 `QIMINGXING_HOST` 覆盖，只绑回环 |
| UI 截图 CI | 拆 `--dump-ui`（纯离屏）与 `--verify-panels`（真实面板） |

---

## 1. 删除 `body_web` 的前置条件：先补原生卡片渲染

这是本次方案里最重要的顺序调整。

`[现状]` `body_web` 是目前**唯一消费 `output.card` 的已发布客户端**（`body_godot/scripts/main.gd:185-240` 也没有对应分支）：

- `protocol/models.py:371-376` 定义 `OutputCardPayload`（`CardAction` 在 `:366-369`）；
- `brain/hub.py:3112` 是 `CardEvent` 分支，真正发送在 `:3123-3133`；
- `body_web/app.js:852` 是事件分发，渲染器 `renderCard` 在 `:698-753`；
- `shell_macos/Sources/Qimingxing/BrainClient.swift:334-627` 的主 `switch` 没有 `output.card`；
- `StreamingEngine.swift:605-614`（`StreamingBlockParser.parse`，定义于 `:488`）遇到 `<<<CARD` 会**丢掉整段卡片正文**，不只是丢弃标记；
- `QuickEntryPanel.swift:1204` 只是注释；真正的合成表格 fallback 在 `:1269-1285`，数据来自答案文本解析，不是卡片 payload。

所以**现在直接删 `body_web`，所有卡片正文、标题和 `more_hint` 动作都会丢失**。卡内表格之所以还能留一部分，是因为 brain 把卡片正文并进 `table_source` 又抽了一次（`hub.py:2766-2774`），以 `turn.media` 的 md-table 形式存在；但如果同一回答的正文里有另一张内联表，客户端的过滤会连这张卡片表格一起丢掉（`MarkdownTable.swift:597-603`，非卡片感知）。

### 1.1 原生卡片渲染 `[计划]`

1. `BrainClient` 增加 `case "output.card"`，解码 `OutputCardPayload`，挂到当前 `ChatEntry.cards`。
2. 新增 `QMXOutputCardView`：标题、类型徽标、Markdown 正文、动作按钮。
3. `dismiss` 本地折叠/展开；其余动作通过 `input.action` 回传 `card_id + action`，复用现有 `sendEnvelope`。
4. 服务端校验：Hub 维护运行期 `card_id → 允许动作集合`，turn 结束失效；未知 `card_id` 或动作**直接忽略并发 notice**，不进入 `hub.py:771` 的通用 prompt 路径；`dismiss` 在服务端是 no-op。按钮点击后置 disabled 直到下一轮开始，防连点。
5. Live Line 在 `answerView` 与 media strip 之间渲染卡片；card-only 回答也要显示卡片，不能只显示媒体。
6. 表格去重：客户端对卡片正文里的表格与 `turn.media` 的 md-table 做内容哈希比较，命中则丢弃 media，卡片优先；不做协议改动。
7. 持久化：
   - brain 在 `assistant_reply` 为空但存在卡片时，把卡片 Markdown 写进 assistant `content`，保证 card-only 回合有 assistant 行（修 `hub.py:2888`）；
   - 新增 `cards_json` 列（沿用 `sqlite_store.py:325-329` 的 `ALTER TABLE ... ADD COLUMN` 模式）；
   - `WorkspaceItem` 增加 `cards`，`WorkspaceStore` 随 `$messages` 一起持久化，重启后侧栏仍能渲染卡片。
8. 标记处理：`MarkdownMessageView` / `parseBlocks` 剥离 `<<<CARD` 片段（`DesignSystem.swift:711-739` 当前不剥）；Swift 流式解析遇未闭合标记时不再吞掉剩余全文（与 brain 的 EOF 自动闭合对齐，`stream_parser.py:365-386`）。

`[现状]` 补充两条容易踩的协议事实：

- **卡片只发给来源连接**：`hub.py:3123` 用 `conn.send_envelope`，不像文本/媒体那样走 `_send_with_mirrors`（`:1626-1640`）。Godot 迁出后只剩一个原生连接，可接受；但要写进协议说明，避免以后加第二客户端时踩坑。
- **`card_id` 是运行期的**：每个 `CardEvent` 新生成（`stream_parser.py:48-49`），不入库、不回传校验（`hub.py:735-779` 从不读 `payload.card_id`）。

### 1.2 验收

- 纯卡片回答可见，且该回合在 SQLite 有 assistant 行；
- 卡片内 Markdown/表格可见，同一张表不重复出现；
- `dismiss` 折叠且服务端无动作；
- `more_hint` 能发起下一轮；
- 伪造 `card_id` 或未知 action 不产生新回合，只收到 notice；
- 重启 App 后工作区侧栏仍显示卡片；
- 深/浅色截图正常；
- 正文不出现 `<<<CARD>>>` 或 `<<<END_CARD>>>`。

**只有这组验收通过，才进入 `body_web` 删除。**

---

## 2. 安全边界：不做 token，但不再信任浏览器来源

按拍板，**不引入 per-launch token，也不给本地进程加鉴权**。但 `body_web` 删除后，合法客户端只剩原生壳、Senses、外部 `body_godot`，它们都不是浏览器。因此可以用零 token、零交互的方式挡掉网页攻击面。

### 2.1 现状 `[修正]`

- `[现状]` **没有任何 middleware**：`brain/main.py:316` 是裸 `FastAPI(...)`，没有 `add_middleware` / CORS。24 个 HTTP 路由分别在 `main.py:349,437,471,512,519,541,554,597,641,674,735,744,788,828,863,912,937,973,1011` 做 inline `request.client.host` 判断。
- `[现状]` 唯一例外是 `GET /api/mcp/oauth/callback`（`main.py:564`），它没有 loopback 检查——因为它是浏览器顶层跳转入口，必须可达。它靠 `state` 自保：`complete_callback` 只在 `state` 命中 `_pending_by_state` 时 resolve（`mcp_oauth.py:231`）。
- `[现状]` 没有任何路由校验 Content-Type；`request.json()` 直接 `json.loads(body)`，`text/plain` 带 JSON 体也会被接受。
- `[现状]` WebSocket 已有 Origin 检查（`main.py:69-85`），放行空 Origin 和回环 Origin（端口集合含 `server_port` 与 `18790`）。
- `[现状]` 绑定默认 `127.0.0.1`（`config.py:175`），但 `main.py:1085` 接受未校验的 `QIMINGXING_HOST` 并传给 uvicorn。

### 2.1.1 零摩擦门禁 `[计划]`

统一 HTTP middleware，不再逐路由判断 `request.client.host`：

1. **只绑定回环地址**：`127.0.0.1` / `::1`；**删除 `QIMINGXING_HOST` 覆盖**（要远程访问另开显式开关，不在本轮）。
2. **Host 白名单**：只允许 `127.0.0.1:<port>`、`localhost:<port>`、`[::1]:<port>`；`testserver` 仅测试模式；拒绝 `evil.com`，挡住 DNS rebinding。
3. **Origin 白名单**：空 Origin（原生客户端/顶层导航）或精确回环 Origin + 精确端口；拒绝 `Origin: null` 和所有外部网页。HTTP 与 WebSocket 同一套规则。
4. **写接口 Content-Type**：只接受 `application/json`；仅 `/api/artifacts` 上传允许 `multipart/form-data`；拒绝 `text/plain`、`application/x-www-form-urlencoded` 这类可绕过 preflight 的简单请求。
5. **不添加 CORS 响应头**，浏览器即使发出请求也读不到结果。
6. **OAuth 回调不豁免**：它是空 Origin 的顶层 GET，规则 3 天然放行；伪造请求因 `state` 不命中而无法完成授权（`mcp_oauth.py:231`）。

这套门禁对 Swift `URLSession`、Senses、Godot WebSocket 都是零感知；不增加 token，也不增加握手。兼容性验收要分别确认三者都发送了正确的 `Content-Type`（Godot 侧历史上是手写 HTTP 头，见 §4.2）。

### 2.2 仍然要修的问题：它们不是"本地信任"能覆盖的

| 问题 | 修法 | 阶段 |
|---|---|---|
| `artifact_store.delete` 路径穿越：`glob(f"{id}.*")` 接受 `..`，`artifact_id` 从 WS `context.screen_ref` 直达 `hub.py:648-653` | 严格 `^[A-Za-z0-9_-]{1,64}$`；解析后必须落在 artifact 根目录内；失败即返回 False 并记日志 | Phase 1c |
| `shell_tool` 只拦 `.ssh`/`id_rsa`/`id_ed25519`/`.env`/`config.yaml`（`shell_tool.py:18`），`cat ~/.aws/credentials` 判 safe（`:118-122`）；`cwd` 不受工作区约束 | 改为**默认拒绝**：工作区/显式授权 root 内自动放行，区外一律审批；凭据目录硬拒绝（永不自动放行，只允许单次审批） | Phase 1c |
| 输出 `communicate()` 全量缓冲（`shell_tool.py:211`），截断发生在全部读完之后（`:241-243`） | 改流式读取 + 达到上限即截断 | Phase 1c |
| 输出脱敏不覆盖常见凭据格式（`config.py:711-720` 只处理已配置密钥、`Bearer`、`sk-`），AWS `AKIA…` 会原样进 LLM | 扩展脱敏规则集，并与 denylist 一起测试 | Phase 1c |
| 审批"记住"是进程级全局 `set`（`hub.py:350`），重启即失效；shell 已按命令前缀 scope（`shell_tool.py:125-155`），base 默认只是工具名（`base.py:172-174`） | 按连接/会话隔离；写操作按工具类定义 scope（文件=canonical 路径，shell=命令前缀+工作目录） | Phase 1c |
| stdio MCP 子进程：配了 `env:` 时 `{**os.environ, **srv.env}`（`mcp_client.py:418`），父进程环境全透传 | 改为最小 allowlist（`PATH/HOME/LANG/TMPDIR` + 系统必需），密钥走 per-server 显式 env 配置 | Phase 2 |
| MCP OAuth 目录基于 `__file__`（`mcp_oauth.py:23`），打包后写入签名 bundle；`build_macos_app.sh:327` 的"Python never writes into Resources"因此不成立 | 改到 Application Support 数据目录；一次性迁移旧 token，不强制重授权 | Phase 2 |
| SQLite 多语句写入无 try/rollback | 显式事务上下文，异常回滚 | Phase 5 |
| `DesktopActions` 临时实例导致 QL delegate 悬空、通知 delegate 变 nil | 全 App 单实例注入；强持有 QL data source/delegate | Phase 3 |
| `CVDisplayLink` 回调 box 可能被提前释放 | retain/release 配对；stop 后延迟释放；deinit 清理 | Phase 4 |
| `ProcessSupervisor` 启动超时后 Brain 仍在跑、崩溃计数不重置 | 超时 kill；Senses/Brain 计数独立重置；停止不阻塞主线程 | Phase 4 |
| `BrainClient` 每次重连新建 `URLSession`，旧请求失败可能拆掉新连接 | 单 Session + 连接 generation；重连清空 per-message `isStreaming` | Phase 4 |
| 模型列表重复 ID 导致 `Dictionary(uniqueKeysWithValues:)` 崩溃 | 先去重再建字典 | Phase 3/6 |

### 2.3 明确接受的残余风险

写入 `SECURITY.md`（当前不存在，需新建）：

- 同用户下的任意本地进程都可以调用 Brain API、安装/启动 MCP 命令、读取配置；
- 这是"高信任本地"的代价，本轮不防御；
- 如果以后要防同用户恶意进程，再引入 token 或 Unix socket peer credentials。

---

## 3. `body_web` 删除清单

### 3.1 代码与构建

- 删除 `body_web/` 整个目录（3 文件 / 1,933 行）。
- `brain/main.py:1018-1028` 删除 `/`、`/app.js`、`/styles.css` 三个静态路由；`/` 改为最小 JSON 服务描述，不保留任何 HTML/JS/CSS。
- **版本号只取一处**：响应里的 `version` 用 `SERVER_VERSION`（`hub.py:166`，读 `version.txt`），不要写死 `"1.0.4"`。同时清理 `brain/utils.py:18` 与 `scripts/build_macos_app.sh:10` 的过期 fallback（后者仍是 `1.0.1`）。
- 清理 `FileResponse`、`_REPO_ROOT` 等不再使用的导入/常量（`main.py:18,65`）。
- `brain/hub.py:537-539` 的默认 `client_type` 从 `body_web` 改为 `body_generic`；需要 `ui.state` 的客户端显式声明 `body_macos` / `body_windows` 或对应 capability。
- **重新生成 `protocol.schema.json`**：`body_web` 出现在 `protocol/models.py:91` 与生成物 `protocol.schema.json:1394`，v2 漏了这一步。
- `scripts/build_macos_app.sh:274` 的 `APP_ITEMS` 移除 `body_web`。
- `qimingxing.sh:267-268` 移除自动 `open http://.../`；`--no-open`（`:250-251`）一并删除。注意当前条件是 `no_open == 0 && with_godot == 0`，Godot 参数移除后简化为纯启动。

### 3.2 测试与文档

- `tests/test_privacy_and_senses.py:133-136`：静态三文件断言改为 `/` JSON 200、`/app.js` 404、`/styles.css` 404（现在只断言 200，不校验 body）。
- `tests/test_protocol_v1_ga.py:60,258`：`client_name: body_web` 改为 `native_test`，并显式加 `client_type: body_macos`。
  `[修正]` 注意：`:258-265` 现在能通过是因为 `hub.py:572-573` 对任何 `role=body` 的连接在有 pending approval 时都会发初始 `ui.state`，不是靠 `ui_subscribed`。改测试后要**另加一条断言广播路径**（`hub.py:956`）的用例，否则会"绿着变行为"。
- `README.md:21` 删除 `body_web` 条目；`README.md:51` 删除"自动在浏览器打开"；`README.md:23` 的 255 测试数改为当前真实值。
- `docs/native-shell-spec.md` 有 6 处 `body_web` 表述（`:51,387,836,854,950,991`），且文件第 5 行自标 `Draft — 待 Review`。先决定它是权威文档还是也进 archive，再改。
- 历史验收报告移到 `docs/archive/`（当前不存在，需新建）或加"历史文档"横幅：`v1.0.1-acceptance-report.md`（205 tests）、`ga-1.0-acceptance-report.md`（191）、`ga-1.0-independent-review.md`（111）、`ga1-avatar-noncritical.md`。四个数字互相矛盾，统一加横幅说明"数字为当时口径"。

### 3.3 验收

- `body_web` 在产品代码、构建脚本、当前文档中零引用（基线：产品 5 文件/11 次、测试 2 文件/3 次、文档 7 文件/41 次）；
- 访问 `/app.js` 返回 404，`/` 返回 JSON；
- 打包产物不再包含 `Resources/app/body_web`；
- `protocol.schema.json` 已重新生成且不含 `body_web`；
- 276+ Python 测试通过；
- 原生卡片渲染验收通过。

---

## 4. `body_godot` 迁出主仓

### 4.1 先复制图标，不要先 `git mv`

`UIPreviewDumper.swift:66,425` 硬编码 `body_godot/assets/sprites/happy.png`；AppIcon 是**软依赖**——`build_macos_app.sh:180-193` 找不到就画椭圆 fallback。复制仍然该做，但理由不是"不复制就丢头像精灵"。

```bash
mkdir -p assets
cp body_godot/assets/sprites/happy.png assets/app_icon.png
```

更新 `UIPreviewDumper` fixture 指向 `assets/app_icon.png`。**不要先 `git mv`**，否则 `git subtree split` 出来的独立仓库会丢失头像精灵。

### 4.2 独立仓库 split 与契约

`[现状]` `body_godot/` **不依赖** `protocol.schema.json` 或 Python `protocol` 包；`ws_client.gd:62-65,87-100` 手写 envelope，`project.godot` 自带全部资源。所以 v2 的"复制生成物到新仓库"前提不存在，真正的风险是**手写协议无声漂移**。

建议新仓库名 `qimingxing-avatar`：

```bash
git subtree split -P body_godot -b split/body-godot
git push <new-remote> split/body-godot:main
```

推送前确认 `body_godot` 工作区干净、历史完整。推送后在新仓库验证：

- `project.godot` 能独立打开；
- 头像、场景、脚本、资源完整；
- 能连接 `ws://127.0.0.1:18790/ws`；
- 声明兼容的协议版本（`system.hello` 上报），主仓按版本决定是否拒连。

验证通过后，主仓执行 `git rm -r body_godot` 并单独提交。

`[待定]` 契约归属（Phase 2 开工前定）：

- (a) 主仓导出带版本号的 `PROTOCOL.md` / JSON schema，新仓库作为下游依赖，主仓 CI 跑兼容测试；
- (b) 冻结 Godot 客户端，只保证现有协议可用，不承诺演进。

推荐 (a)+(b) 组合。现有 `tests/test_audio_pipeline.py:218-220,412-414` 已用 `body_godot` 字符串验证协议兼容，可作为兼容测试雏形。

### 4.3 主仓清理

- `scripts/build_macos_app.sh`：移除 `--with-godot`、`WITH_GODOT`、`GODOT_APP_SRC`、`APP_ITEMS+=(body_godot)`、可选 Godot 运行时复制（`:19,24,38,41,87-110,275-276,314-316`）。
- `ProcessSupervisor.swift:31,41,276`：移除 `godotRunning`、`godotProcess`、`isGodotPluginAvailable`、`launchOptionalGodot`、`syncAvatarEnabled`。
- `SettingsWindowController.swift:758-766`：移除"可选桌面头像插件"块（现在是 `isGodotPluginAvailable` 为 false 时显示静态文字，不是禁用开关）。
- `QimingxingApp.swift:234-236`：移除 `onAvatarEnabledChanged -> supervisor`。
- **同时移除 Settings 保存中的 `avatar_enabled` 写入**（`SettingsWindowController.swift:1587,1637`），避免主壳打开设置时把外部头像配置覆盖成默认 false——这一条是必须的，因为开关隐藏时该字段仍在写。
- `qimingxing.sh`：移除 `GODOT_PID_FILE`、`find_godot_bin`、`start_godot`、状态/停止中的 Godot 分支、`--with-godot`（`:12,19,194-238,260-261,274,283,336,340`）。
- `README` 改为链接独立仓库，说明外部头像自行启动。

### 4.4 保留的兼容面

- 保留 protocol 中的 `body_godot` client type（`models.py:91`、`protocol.schema.json:1393`）；
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

### 5.1 现状 `[修正]`

`[现状]` 窗口/焦点问题比 v2 描述更大：

- **12+ 个窗口/面板拥有者**，不止 10 个：`MenuBarController`（NSStatusItem + NSPopover）、`DesktopActions`（QLPreviewPanel + 钉图 NSPanel）、`NSAlert`（`ProcessSupervisor.swift:356`）、`NSOpenPanel`（`QuickEntryPanel.swift:1771`），`SpatialLassoController` 一个类拥有 3 个 panel 类型 + 一个可多实例的"钉住便签"字典（`:139,150,161,749`）。
- `NSApp.activate` **14 处、8 个文件**：QuickEntry `:368,1780,1784,2246`；Approval `:214,242`；Media `:144,176`；Lasso `:424,588`；Settings `:186`；Workspace `:421`；DesktopActions `:288`；ProcessSupervisor `:362`。PermissionGuide / TaskToast / DropPocket / WalkieTalkie 不调用它，只用 `orderFrontRegardless`。
- `QuickEntry` 有 **5 个焦点特例**，不是 1 个：`installFocusRecovery()`（任何窗口关闭后 120 ms 重新抢 key，`:103-143`）、`pickAttachmentFile()` 围绕 NSOpenPanel 的 orderOut/restore（`:1776-1784`）、`hideAndRestoreFocus()`（`:429-437`）、`show()` 的 150 ms 延迟激活（`:360-370`）、`MediaViewerLauncher.openHandler` 的三路分支（`QimingxingApp.swift:107-119`）。
- `hiddenForMediaViewer`（`:77`）只在 `restoreAfterMediaViewer`（`:172-179`）里清，`show()` 不清；250 ms 延迟在 `QimingxingApp.swift:323-330`，不在面板里。
- **Approval 不是 sheet**：独立 floating `NSWindow`（`ApprovalSheetController.swift:211-246`），并且会 `NSApp.activate(ignoringOtherApps: true)`。
- `HotkeyManager` 已经用 Carbon 统一注册 5 个热键并从 Brain 同步（`HotkeyManager.swift:298`，默认 `option+space` / `option+k` / `option+esc` / `option+d` / `option+shift+space`）；PTT 在 Senses（`senses/audio/macos_ptt.py`）。缺的是审批/权限期间的仲裁，不是注册。

### 5.2 角色与优先级

| 组 | 角色 | 规则 |
|---|---|---|
| passive | `taskToast`、`sidecarSuggestion` | 不抢 key、不 `NSApp.activate`、不挂起其他窗口 |
| transient | `dropPocket`、`spatialLasso`、`walkieTalkie`、`menuBarPopover`、`quickLookPanel` | 非激活浮层；不改变 key owner |
| content | `quickEntry`、`workspaceWing`、`workspaceWindow`、`mediaAttached`、`mediaDetached`、`settings`、`pinnedNote` | 同一时刻只有一个 key owner；Settings / Detached Media 打开时挂起 QuickEntry（Workspace 只接管 key，保留并排） |
| modal | `approval` | 高于 content；不挂起 QuickEntry，只接管 key，关闭后归还 |
| security | `permissionGuide` | 最高优先级；不抢焦点，关闭后归还 |

`pinnedNote` 是唯一允许多实例的 content：按"最后获得 key 者持有"处理，不进入 suspend 矩阵。`quickLookPanel` 由系统托管，只登记不仲裁。

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
- `HotkeyArbitrator`（不是 HotkeyRouter）：复用现有 `HotkeyManager` 注册，只增加"审批/权限/内容窗口期间的命令仲裁"。
- `PanelFactory`：统一 `NSPanel` 的 `styleMask`、`level`、`collectionBehavior`、`hidesOnDeactivate`、`becomesKeyOnlyIfNeeded`。

### 5.4 suspend/restore 矩阵

| 场景 | QuickEntry 行为 | 关闭后 |
|---|---|---|
| 打开 Workspace Wing | 不挂起，wing 是同一面板的一部分 | 收 wing，不重建面板 |
| 打开完整 Workspace 窗口 | **不挂起**（决策：保留并排布局）；Workspace 接管 key，QuickEntry 留在原位 | 关闭 Workspace 后 key 还给 QuickEntry |
| 打开 Settings | **挂起** | 同上；若用户已切到别的 App，不抢焦点 |
| 打开 detached Media Viewer | **挂起** | 同上；保留现有 250 ms 延迟但走 lease generation |
| 打开 Approval | 不挂起；QuickEntry 保留但让出 key | 归还 key 给之前 owner |
| Approval 在 Settings 打开期间到达 | 不挂起 Settings；Approval 接管 key | key 还给 Settings |
| 打开 Permission Guide | 不挂起；security 优先 | 归还 key |
| Task Toast / Drop Pocket / Lasso / Walkie / 菜单栏弹层 | 不挂起 | 不改变 key |
| QuickEntry 热键 | 切换 QuickEntry；若被挂起则恢复并置于当前内容之上 | 关闭后 key 回到原内容；审批/权限存在时不抢 |
| Settings 打开时按 QuickEntry 热键 | **A（已定）**：把 QuickEntry 提到 Settings 之上并挂起 Settings | 关闭 QuickEntry 后恢复 Settings；审批/权限存在时不抢 |

**焦点归还规则**：

1. 激活任何窗口前，`FocusArbitrator` 记录当前前台外部 App。
2. 只有 `ActivationPolicy.required` 才调用 `NSApp.activate`；passive/transient 只 `orderFrontRegardless`。
3. 关闭时优先把 key 还给 lease 栈顶；若 QuickEntry 是挂起前可见的，恢复它。
4. 若用户已切换到其他 App，或当前前台不是启明星，**不激活、不抢焦点**。
5. 所有直接 `NSApp.activate` / `makeKeyAndOrderFront` 从控制器移除，只保留在 coordinator/factory。

### 5.5 迁移顺序

`[现状]` Phase 3 已落地（PR #21）：

1. ~~骨架~~：`WindowCoordinator` + `FocusArbitrator` + `PanelFactory` 已入，59→60 XCTest。
2. ~~QuickEntry 焦点特例~~：show/hide 走 lease；`installFocusRecovery` 在非 owner 时让位；`hiddenForMediaViewer` 及其 250ms+350ms 延迟链已删除。
3. ~~Settings / detached Media~~：present/dismiss + suspend/restore；决策 A 已人工验证。
4. ~~Workspace~~：只接管 key、保留并排布局（决策见 §5.4）。
5. ~~Approval / Permission~~：Approval 为可成为 key 的独立浮窗（`ApprovalWindow.canBecomeKey`）；Permission 最高优先级但永不拿 key。Toast / DropPocket / Lasso / Walkie / 菜单栏 / QL 面板按 passive/transient **不注册**（不接管 key、不挂起别人）。
6. **仍保留的直接激活（例外清单）**：`NSOpenPanel.runModal` 前后、fatal error `NSAlert`、SpatialLasso overlay/popover、QL 预览、composer 焦点通知。这些是系统模态或瞬态浮层，不经 coordinator；其余 content/modal 角色的激活只经 `FocusArbitrator`。

### 5.6 Sidecar

本轮不做。等 `WindowCoordinator` 至少稳定一个发布周期后再做：

- Sidecar 只作为服务/意图，不新增窗口；
- 提示走 `TaskToast`；
- 若确实需要面板，使用 `.sidecarSuggestion` role，经 coordinator 统一调度；
- 没有 coordinator 不开发 Sidecar。

---

## 6. 测试与 CI

### 6.1 Swift 测试目标

`[现状]` `shell_macos/Package.swift` 只有一个 executable target（`:13-15`），没有 test target，也没有 package 依赖（`.build` 里残留的 Luminare 等 checkout 不再被 import）。

Phase 0 先建最小可测骨架，Phase 4 再拆完整分层：

```text
Sources/QimingxingCore/     # 可测试状态机/策略/模型
Sources/QimingxingUI/       # SwiftUI/AppKit 视图与服务（Phase 4）
Sources/Qimingxing/         # App 入口与依赖装配
Tests/QimingxingCoreTests/
```

`[现状]` Phase 0 已验证：SwiftPM 6.4 允许 test target 直接依赖 executable target，`@testable import Qimingxing` 可用（`ModuleSmokeTests` 通过；本地 debug 构建 18.5s、release 27.6s）。Phase 1a 直接测；`QimingxingCore` 拆分留到 Phase 4，按需进行。

首期（Phase 1a）≥20 个 XCTest，覆盖：

- 卡片 payload 解码（含缺字段、未知 action、空 actions）；
- 表格内容哈希去重（含转义管道、代码 span）；
- `<<<CARD` 未闭合 / 非法开头 / 截断；
- 流式块解析不吞正文。

Phase 4 补到 80–120，覆盖：

- `WindowCoordinator`：present/dismiss 优先级、lease generation、suspend/restore、key 归还；
- `FocusArbitrator`：外部 App 记录、切走不抢焦点、嵌套 modal；
- `HotkeyArbitrator`：命令映射、审批期间行为、无重复注册；
- `StreamingBlockParser` / `StreamingRevealer`：代码围栏、表格、卡片标记、半截 token、CJK、结束后 flush；
- `GhostWriteEngine`：idle/capturing/writing/done/failed、取消、超时、目标 App 切换；
- `MarkdownTable`：转义管道、代码 span、15×100 上限、TSV；
- `ReconnectPolicy`：指数退避、上限、welcome 后重置；
- `ProcessSupervisor`：启动超时、崩溃计数、停止幂等（注入 fake process/clock）；
- `ApprovalQueue`：去重、顺序、resolved；
- `MediaViewerModel`：表格大窗口条件、关闭通知。

`[现状]` Phase 4 已扩到 **90 个 XCTest**（ApprovalQueue、MediaViewerModel、MarkdownTable 解析/上限/TSV、StreamingBlockParser 的代码围栏/CJK/未闭合强调/表格检测）。`UIPreviewDumper.swift` 当前 1,371 行，移出生产 target 需要先有 `QimingxingUI` library + public API，**延后到 Phase 6 的 Feature 拆分一起做**（本阶段不做，避免一次性 public 化全仓类型）。**尚未覆盖**：`GhostWriteEngine`、`ReconnectPolicy`（当前无独立类型）、`ProcessSupervisor`（需要 fake process/clock 注入）。

### 6.2 Python 测试

- 增加 `pytest-timeout`、`pytest-cov`；
- 标记 `macos`、`tcc`、`chrome`、`slow`；
- 真实 TCC/Chrome 测试默认 skip，CI 用 fake；
- 覆盖率先 report-only，基线稳定后设 70% gate；
- 现有 276 个测试不得减少。

### 6.3 CI

新增 `.github/workflows/ci.yml`（当前 `.github` 不存在）：

- `python-tests`：macOS runner，`pytest -q --timeout=60`；
- `swift-build`：`swift build -c release`；
- `swift-tests`：`swift test`；
- `ui-snapshots`：**只跑离屏渲染**。现有 `--dump-ui` 里的 `verifyRealPanelLifecycle` 会真的 `controller.show()` 并跑 run loop（`UIPreviewDumper.swift:576,631`），必须拆成 `--dump-ui`（`cacheDisplay` 离屏）与 `--verify-panels`（真实面板，手动/单独 job）；
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

1. `[现状]` `main.py` 路由拆分完成（PR #23）：`brain/app/routes/` 10 个模块，`main.py` 1,161 → 502 行；
2. `[现状]` `rich_content` 拆 `rich/` 完成：`sources.py` / `tables.py` / `media.py`，`rich_content.py` 降为 31 行兼容 facade；
3. `[现状]` `hub` 拆分完成（PR #24–#29）：`connection` / `ui_state` / `approvals` / `turns`（含 `_start_turn` / `_run_turn` / 截屏）/ `tool_schema` / `media`；`core.py` 3,207 → **1,617**，只剩 Hub 本体与编排方法；
4. `[现状]` `worker` 完成（#30–#32）；`llm` 完成（#33–#35）；`memory` **拆分完成**（#36–#39：schema/rows/messages/facts/skills_usage/tasks_store/snapshot，`sqlite_store.py` 2,517 → **264**）；再 `tools` 与 `config`/`browser`；
5. `[现状]` `browser` 已拆（#40）；`config` **拆分完成**（#41–#43：defaults 59 / models 700 / io 1,370，`config.py` 2,069 → **72 行 facade**）；仅剩 `tools/macos_apps.py`（968，需先加 `run_osascript` 注入缝）。

### 7.2 Swift 目标结构

```text
Sources/Qimingxing/
  App/
  Core/Window/{WindowCoordinator,FocusArbitrator,HotkeyArbitrator,PanelFactory,WindowRole}.swift
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
  `[现状]` Phase 1a 是功能净增（`hub.py` 3,207、`QuickEntryPanel.swift` 2,614）；Phase 5 起以 1a 后的数值为新基线。
- 行数由 `scripts/metrics_report.py` 采集；**是否在 CI 阻断**留到 Phase 4 定（先只报告）；
- `[现状]` Phase 4 已移除 `Package.swift` 的 `-disable-dynamic-actor-isolation` / `-disable-actor-data-race-checks`：新 build path 下干净构建 + 90 tests 全绿，不再作为长期配置。

---

## 8. 实施顺序与回滚

| 阶段 | 内容 | 出口条件 |
|---|---|---|
| Phase 0 | tag `pre-restructure-2026-11-27`；提交未跟踪文档；基线指标入库；`QimingxingCore` + Tests 骨架；CI 骨架 | 工作区干净，基线可回滚，`swift test` 绿，CI 三 job 绿 |
| Phase 1a | 原生 `output.card` 渲染 + 动作 registry + content/cards_json 落库 + workspace 侧栏 + 表格哈希去重 + 裸标记修复 + ≥20 XCTest | 卡片/动作/纯卡片回答/重启回放全部通过，card-only 回合有 assistant 行 |
| Phase 1b | 删除 `body_web`，`/` 改 JSON（版本取 `SERVER_VERSION`），重生成 `protocol.schema.json`，更新构建/脚本/测试/文档 | `body_web` 产品引用为 0，schema 不含 `body_web`，测试全绿 |
| Phase 1c | HTTP Host/Origin/Content-Type 门禁 + 删除 `QIMINGXING_HOST` + artifact 路径穿越 + shell 默认拒绝 + 脱敏 + 审批隔离 | 跨站 403/415，路径穿越探针失败，原生客户端无感 |
| Phase 2 | `body_godot` 图标复制、subtree split、主仓删除、外部仓库验证、契约与兼容测试 | 主仓无 GDScript，外部头像可连，兼容测试绿 |
| Phase 3 | `WindowCoordinator` + suspend/restore + 焦点矩阵测试（12+ 窗口、5 个特例） | 设置/工作区/媒体关闭后正确恢复 |
| Phase 4 | Swift 测试 target 完整拆分、core 抽取、`UIPreviewDumper` 移出、Streaming/GhostWrite 测试 | 80–120 Swift tests，CI 绿 |
| Phase 5 | Python 巨石拆分（按域逐个 PR） | 测试数不降，文件大小下降 |
| Phase 6 | Swift Feature 拆分、移除 unsafe concurrency flags | 构建/测试/截图全绿 |
| Phase 7 | CI 加固、打包/codesign/性能验收 | 发布包可验证 |
| Phase 8 | App-Aware Sidecar | coordinator 稳定一个版本后再评估 |

每个阶段独立 PR，不混合提交。`body_web` 删除和 `body_godot` 迁出必须是两个独立 PR，方便单独回滚。每个 PR 必须附 `docs/restructure-metrics.md` 的「实测」列更新。

---

## 9. 总验收标准

| 维度 | 标准 |
|---|---|
| 遗留代码 | 产品代码/构建脚本无 `body_web`、`body_godot`；历史文档归档；`ARCHITECTURE.md`/`AGENTS.md` 明确产品/外部/dev-only |
| 测试 | Python ≥276；Swift：1a ≥20、Phase 4 80–120；无新增长期 skip；CI 全绿 |
| 安全 | 外部 Origin、`Origin: null`、非法 Host、`text/plain` POST 均 403/415；`QIMINGXING_HOST` 已删；artifact 路径穿越探针失败；原生 WS/HTTP 正常 |
| 卡片 | 标题/正文/动作/纯卡片回答/卡内表格全部正常，无裸标记；重启后侧栏可见；未知 action 不产生回合 |
| 窗口 | QuickEntry→Settings/Workspace/Media 关闭后恢复 frame/draft/焦点；切走 App 不抢焦点；审批/权限无死锁 |
| UI | `--dump-ui` 深/浅色截图无回归；表格、媒体、Ghost Write 手动点测 |
| 打包 | `dist/Qimingxing.app` 构建成功，`codesign --verify --deep --strict` 通过；包体 ≤ 基线 |
| 性能 | 启动/内存记录基线，重构 PR 不引入 >10% 回退 |
| 数据 | `docs/restructure-metrics.md` 的「实测」列全部填写，基线→实测可对比 |

---

## 10. 风险与后果

- **删除 `body_web` 会失去浏览器调试台**：替代是 `/` JSON、日志、`--dump-ui` 截图和原生 Workspace；这是已拍板的取舍。
- **Windows 端没有原生壳**：删除 `body_web` 后，Windows 只剩 Senses，无可视 UI；若以后需要，另立客户端仓库，不回流主仓。
- **卡片渲染是删除 `body_web` 的硬前置**：不做就会丢卡片正文，不能先删后补。
- **无历史回放**：shell 不读 SQLite，卡片只在当前运行 + `workspace.json` 中可见；完整历史是可独立立项的功能，不藏在 1a 里。
- **`body_godot` 独立仓库存在版本偏移风险**：手写 envelope 不依赖 schema，靠兼容版本声明 + 主仓 compatibility 测试控制；契约归属 `[待定]`。
- **WindowCoordinator 会改变焦点行为**：必须用矩阵测试锁定；每个迁移 PR 可单独回滚。实际基数是 12+ 窗口、5 个 QuickEntry 特例，工期按此估算。
- **Swift target 拆分可能影响打包脚本**：每步都跑 `swift build` + 打包 smoke，不一次性大搬迁。
- **高信任本地的残余风险已接受**：同用户恶意进程不在本轮防御范围，写入 `SECURITY.md`（需新建）。
