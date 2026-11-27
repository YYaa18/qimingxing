# v1.1 改造比对数据

> 基线：`22ca135`（tag `pre-restructure-2026-11-27`），version 1.0.4  
> 用途：每个改造 PR 必须同时给出「基线 → 实测」两列，不接受"感觉变好了"。

## 0. 复现方法

```bash
# 机械指标（代码行数、测试数、引用数、包体）
.venv/bin/python scripts/metrics_report.py
.venv/bin/python scripts/metrics_report.py --json > /tmp/metrics.json

# Python 测试
.venv/bin/python -m pytest -q

# 基线对照（在独立 worktree 里跑同一组测试）
git worktree add /tmp/qmx-baseline 22ca135
```

## 1. 机械指标

| 指标 | 基线 `22ca135` | 目标 | 实测 |
|---|---:|---:|---:|
| Python 测试 | 276 | ≥276 | **296**（1a +6，1c +11，Phase 2 +3） |
| Swift 测试 | 0 | 1a ≥20；Phase 4 ≥80 | **90**（Phase 1a 27 + Phase 3 33 + Phase 4 30） |
| `body_web` 产品代码引用 | 5 文件 / 11 次 | 0 | **0**（Phase 1b） |
| `body_web` 代码行 | 1,933 | 0 | **0**（Phase 1b） |
| `body_web` 打包产物 | 随 `Resources/app/body_web` 打入 | 不打包 | **不在产物内**（Phase 1b） |
| `body_godot` 产品代码引用 | 7 文件 / 20 次 | 0（Phase 2） | **2 文件 / 5 次**（仅协议兼容面：client type + schema） |
| `body_godot` 代码行 | 6,521 | 0 | **0**（Phase 2） |
| 应用包体 | 417.5 MB | ≤ 基线 | **418 MB**（body_web 仅 64K，体积由 Python runtime 主导） |
| `brain/hub.py` | 3,135 行 | 不增长 | **3,207**（1a 净增 72；Phase 5 基线改为 3,207） |
| `QuickEntryPanel.swift` | 2,548 行 | 不增长 | **2,614**（1a 净增 66） |
| `UIPreviewDumper.swift` | 1,342 行 | 移出生产 target | **1,371**（新增卡片 fixture） |

基线代码量：

| area | files | lines |
|---|---:|---:|
| brain | 66 | 30,430 |
| senses | 13 | 1,762 |
| protocol | 3 | 816 |
| tests | 49 | 12,958 |
| scripts | 3 | 806 |
| shell_macos | 29 | 21,290 |
| body_web | 3 | 1,933 |
| body_godot | 13 | 6,521 |

基线最大文件（前 10）：

| file | lines |
|---|---:|
| `brain/hub.py` | 3,135 |
| `shell_macos/Sources/Qimingxing/QuickEntryPanel.swift` | 2,548 |
| `brain/memory/sqlite_store.py` | 2,503 |
| `brain/config.py` | 2,054 |
| `shell_macos/Sources/Qimingxing/SettingsWindowController.swift` | 1,940 |
| `body_godot/scripts/settings_window.gd` | 1,808 |
| `shell_macos/Sources/Qimingxing/DropPocketController.swift` | 1,517 |
| `brain/task_runner.py` | 1,410 |
| `brain/llm_router.py` | 1,354 |
| `shell_macos/Sources/Qimingxing/UIPreviewDumper.swift` | 1,342 |

## 2. 安全项

| 项 | 基线证据 | 目标 | 实测 |
|---|---|---|---|
| artifact 路径穿越删除 | 探针：`ArtifactStore.delete("../victim")` 返回 `True`，`victim.txt` 被删除 | 拒绝并保留文件 | **探针返回 `False`，文件保留**；id 限 `^[A-Za-z0-9_-]{1,64}$` + resolve 后根目录包含检查 |
| HTTP 跨站请求 | 无 middleware；24 个路由各自 inline 判 loopback；无 Host/Origin/Content-Type 校验（`brain/main.py:316`） | 外部 Origin / 非法 Host / `text/plain` 写请求 403/415 | **统一 middleware**：非法 Host 403、外部 Origin / `Origin: null` 403、`text/plain` 写请求 415、无 CORS；OAuth 顶层跳转与无 body 的原生 POST 不受影响 |
| 绑定地址 | `QIMINGXING_HOST` 未校验直接传给 uvicorn（`brain/main.py:1085`） | 只绑回环 | **覆盖已删除**；非回环 `server.host` 强制回退 `127.0.0.1` 并告警 |
| shell 读凭据 | `cat ~/.aws/credentials` 判为 `safe`（`shell_tool.py:18` 无 `.aws`；`:118-122` 前缀判定） | 工作区外审批；凭据目录硬拒绝 | **`.aws/.kube/.docker/.config/gh/.npmrc/.netrc/.gnupg/.password-store` 等硬拒绝**；`cat /etc/hosts` → confirm；`cwd=/tmp` → confirm；安全前缀只在工作区内自动放行 |
| 输出脱敏 | 只覆盖已配置密钥 / `Bearer` / `sk-`（`config.py:711-720`），`AKIA…` 不遮 | 覆盖常见凭据格式 | **AKIA/ASIA、`ghp_`/`github_pat_`、`xox*`、`AIza`、PEM 私钥均已遮蔽** |
| MCP 子进程环境 | 配了 `env:` 时 `{**os.environ, **srv.env}`（`mcp_client.py:418`）；未配时 SDK 6 变量 allowlist | 父进程环境不透传 | **已实现**：固定 allowlist + per-server `env:`；密钥不再从父进程继承 |
| MCP OAuth 目录 | `Path(__file__)` 推导，打包后写进签名 bundle | 用户数据目录 + 一次性迁移 | **已实现**：`Application Support/Qimingxing/mcp_oauth`（Windows `%APPDATA%`），旧 token 首次使用时迁移并保持 0600 |
| 审批记住 scope | 进程级全局 `set`（`hub.py:350`），重启即失效；shell 已按命令前缀（`shell_tool.py:125-155`） | 按连接/会话隔离 | **按 `client_name`/`client_id` 隔离**；`clear_history` 只清当前客户端 |

路径穿越探针（基线可复现）：

```python
import tempfile
from pathlib import Path
from brain.artifact_store import ArtifactStore

root = Path(tempfile.mkdtemp())
base = root / "artifacts"; base.mkdir()
victim = root / "victim.txt"; victim.write_text("secret")
store = ArtifactStore(base_dir=base)
assert store.delete("../victim") is True
assert not victim.exists()          # 基线：True（文件被删）
```

## 3. 卡片能力

| 能力 | 基线 `22ca135` | 目标 | 实测 |
|---|---|---|---|
| 纯卡片回答在原生壳可见 | 否。`BrainClient` 无 `output.card` 分支，流式解析整段丢弃卡片正文 | 可见 | **是**（`quick_entry_card_only.png` 深/浅色） |
| 卡片标题 / 正文 / 动作 | 0 处原生渲染；仅 `body_web` 渲染 | 全部原生渲染 | **`QMXOutputCardView`**：标题、徽标、Markdown、动作 |
| 卡片动作回传 | `input.action` 的 `card_id` 从不校验；未知 action 拼进 prompt 开新回合 | registry 校验 + 拒绝 | **是**：`card_id → action` registry；未知/伪造返回 `system.error`；`dismiss` 服务端 no-op |
| 同一张表去重 | 卡片表格同时进 `turn.media`（`hub.py:2766-2774`），客户端过滤不认卡片 | 内容哈希去重，卡片优先 | **是**：`QMXTableDedupe`（header + 前 3 行） |
| card-only 回合落库 | 否。`assistant_reply` 为空时不写 assistant 行（`hub.py:2888`） | content 写卡片正文 + `cards_json` | **是**：`content` 回填 + `cards_json` 列（含归档/快照/旧库迁移） |
| 跨重启卡片 | 否。`workspace.json` 的 `WorkspaceItem` 无 cards 字段 | 侧栏保留卡片 | **是**：`WorkspaceItem.cards` 往返测试 |
| 裸 `<<<CARD>>>` 标记 | `MarkdownMessageView` 不剥离，泄漏片段按普通文本渲染 | 渲染层剥离 | **是**：`QMXCardMarkers.strip` + 流式非法标记不吞正文 |

### 3.1 Phase 1a 证据

```bash
.venv/bin/python -m pytest -q                          # 282 passed
cd shell_macos && swift test                           # 27 passed
swift build -c release                                 # Build complete (22.9s)

# 从仓库根目录执行（fixture 依赖 cwd）
shell_macos/.build/debug/Qimingxing --dump-ui /tmp/qmx-ui-dark
shell_macos/.build/debug/Qimingxing --dump-ui /tmp/qmx-ui-light --light
```

产物：`quick_entry_card_only.png`（纯卡片回答，深/浅色）、`quick_entry_chat.png`（卡片 + 表格 + 媒体混合）。

已知取舍：

- 卡片动作 registry 按容量（200）淘汰，不按回合失效——卡片在回合结束后仍需可点。
- 表格签名只比较表头与前 3 行，以兼容 brain 侧 15×100 截断。
- 1a 是功能净增：`hub.py` +72 行、`QuickEntryPanel.swift` +66 行；Phase 5 的"只降不升"基线改为 3,207 / 2,614。

### 3.2 Phase 1b 证据

```bash
.venv/bin/python -m pytest -q                 # 282 passed
.venv/bin/python -m protocol.export_schema    # body_web refs: 0
find dist/Qimingxing.app -name "body_web*"    # 空
codesign --verify --deep --strict dist/Qimingxing.app   # OK
./scripts/build_macos_app.sh                  # 418M
shell_macos/.build/debug/Qimingxing --dump-ui /tmp/qmx-ui-dark2
```

`GET /` 返回 `{"service":"qimingxing","version":"1.0.4","status":"ok","ui":"native"}`；`/app.js`、`/styles.css` 返回 404。

### 3.3 Phase 1c 证据（同一组测试，基线 vs 本分支）

```bash
# 基线 22ca135（独立 worktree）
git worktree add /tmp/qmx-baseline 22ca135
cp tests/test_security_gate.py /tmp/qmx-baseline/tests/
cd /tmp/qmx-baseline && <repo>/.venv/bin/python -m pytest tests/test_security_gate.py -q
# → 7 failed, 4 passed

# 本分支
.venv/bin/python -m pytest tests/test_security_gate.py -q
# → 11 passed
```

基线失败项：非法 Host、跨站 Origin、`text/plain` 写请求、路径穿越删除、凭据脱敏、shell 凭据硬拒绝、工作区边界。
其余 4 项（原生无 Origin、multipart 上传、OAuth 顶层跳转、有效 id 删除）在两侧都通过，用来证明门禁没有误伤。

### 3.4 Phase 2（迁出准备）证据

```bash
cp body_godot/assets/sprites/happy.png assets/app_icon.png
.venv/bin/python -m pytest -q            # 296 passed
cd shell_macos && swift test             # 27 passed

git subtree split -P body_godot -b split/body-godot
git ls-tree --name-only split/body-godot
# assets/ default_bus_layout.tres project.godot scenes/ scripts/ theme/
git ls-tree split/body-godot assets/sprites/happy.png   # 头像精灵在
```

主仓已摘除：`--with-godot`、`Godot.app` 打包、`ProcessSupervisor` 启动器、设置页头像开关、`avatar_enabled` 写入、`qimingxing.sh` Godot 分支、AppIcon/预览对 `body_godot` 的路径依赖（改用 `assets/app_icon.png`）；最后 `git rm -r body_godot`（6,521 行 GDScript）。

**独立仓库运行时验证**（`YYaa18/qimingxing-avatar`，private）：

```bash
# 临时 Brain（隔离数据目录，端口 18791）
cd /tmp/qmx-run && PYTHONPATH=<repo> QIMINGXING_PORT=18791 <repo>/.venv/bin/python -m brain.main
# 从新仓库 clone 出来的项目，headless 启动
QIMINGXING_WS_URL=ws://127.0.0.1:18791/ws \
  ~/Downloads/Godot.app/Contents/MacOS/Godot --headless --path /tmp/qmx-avatar --quit-after 400
```

Brain 日志：`Handshake hello: client=client_1 role=body type=body_godot name=body_godot`，随后干净断开。头像/场景/脚本/主题与 80 个提交历史均在远程仓库中。

### 3.5 Phase 3（骨架）证据

```bash
cd shell_macos && swift test   # 56 passed（新增 29）
```

新增 `Core/Window/{WindowRole,WindowCoordinator,FocusArbitrator,PanelFactory}.swift`：

- 纯状态机：优先级、key owner、suspend/resume、`WindowLease` generation。
- 决策 A 已进规则并接线：QuickEntry 覆盖 Settings 时挂起 Settings，QuickEntry 关闭后恢复。
- `FocusArbitrator` 是当前唯一允许 `NSApp.activate` 的组件；记录外部前台 App，用户已切走时不抢焦点。
- `PanelConfiguration` 按 role 给出 level/styleMask/collectionBehavior，可在无窗口环境下断言。
- 已接线：QuickEntry、Settings、Media（detached）、Workspace（只接管 key，保留并排）、Approval、Permission（永不拿 key）。
- 卡片内表格可点击打开媒体查看器（`MarkdownMessageView.onOpenTable`）。
- 不注册（passive/transient，不接管 key）：Toast、DropPocket、Lasso、Walkie、菜单栏、QL 面板。
- 例外清单（仍可直接 `NSApp.activate`）：`NSOpenPanel` 系统模态、fatal `NSAlert`、SpatialLasso overlay/popover、QL 预览、composer 焦点通知。
- `resume` 保留原 sequence；`onKeyOwnerChanged` 会把模型里的 keyOwner 强制落到真实窗口（含 `ApprovalWindow.canBecomeKey = true`）。

### 3.6 Phase 4 证据

```bash
cd shell_macos && swift test                          # 90 passed
swift build --build-path /tmp/qmx-noflags             # 去掉 unsafe flags 后干净构建
swift test --build-path /tmp/qmx-noflags2             # 90 passed，无并发诊断错误
```

- 新增 30 个测试：`ApprovalQueueModel`、`MediaViewerModel`、`MarkdownTable` 解析/15×100 上限/TSV、`StreamingBlockParser`（代码围栏、CJK、未闭合强调、表格检测、空输入）。
- `Package.swift` 已删除 `-disable-dynamic-actor-isolation` / `-disable-actor-data-race-checks`。
- 未做：`UIPreviewDumper` 移出生产 target（需要 library + public API，顺延到 Phase 6）；`GhostWriteEngine`/`ProcessSupervisor` 测试（缺少注入点）。

### 3.7 Phase 5 证据（路由层 + rich_content）

```bash
.venv/bin/python -m pytest -q   # 296 passed
wc -l brain/main.py brain/rich_content.py brain/rich/*.py brain/app/routes/*.py
```

| 文件 | 前 | 后 |
|---|---:|---:|
| `brain/main.py` | 1,161 | **502**（app 装配 + `/ws`） |
| `brain/rich_content.py` | 713 | **31**（兼容 facade） |
| `brain/app/routes/` | 0 | 10 模块（1,113 行，含 `_deps.py`） |
| `brain/rich/` | 0 | `sources 425` / `tables 213` / `media 86` |

`RouteDeps` 显式传递共享对象；`brain.rich_content` 的公共 import 全部保持可用（hub、task_runner、browser tools、tests 未改）。

## 4. 性能

| 指标 | 基线 `22ca135` | 目标 | 实测 |
|---|---:|---:|---:|
| 冷启动到可输入 | 未采集（改造 PR 不得回退 >10%） | ≤ 基线 | 待填 |
| 空闲内存 | 未采集 | ≤ 基线 | 待填 |
| Swift debug 构建（`swift test`） | 18.5 s | 不回退 >10% | 18.5 s（Phase 0） |
| Swift release 构建 | 27.6 s | 不回退 >10% | 27.6 s（Phase 0） |

> 构建时间为本机 arm64 实测，仅用于同一台机器上的前后对比。

> 性能基线需要真实启动 App 采样，在 Phase 1b 前补采一次；本轮不以性能为主目标，只做防回退。
