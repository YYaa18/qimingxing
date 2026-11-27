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
| Python 测试 | 276 | ≥276 | **282**（Phase 1a：+6） |
| Swift 测试 | 0 | 1a ≥20；Phase 4 ≥80 | **27**（Phase 1a） |
| `body_web` 产品代码引用 | 5 文件 / 11 次 | 0 | **0**（Phase 1b） |
| `body_web` 代码行 | 1,933 | 0 | **0**（Phase 1b） |
| `body_web` 打包产物 | 随 `Resources/app/body_web` 打入 | 不打包 | **不在产物内**（Phase 1b） |
| `body_godot` 产品代码引用 | 7 文件 / 20 次 | 0（Phase 2） | 7 文件 / 20 次 |
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
| artifact 路径穿越删除 | 探针：`ArtifactStore.delete("../victim")` 返回 `True`，`victim.txt` 被删除 | 拒绝并保留文件 | 待填 |
| HTTP 跨站请求 | 无 middleware；24 个路由各自 inline 判 loopback；无 Host/Origin/Content-Type 校验（`brain/main.py:316`） | 外部 Origin / 非法 Host / `text/plain` 写请求 403/415 | 待填 |
| 绑定地址 | `QIMINGXING_HOST` 未校验直接传给 uvicorn（`brain/main.py:1085`） | 只绑回环 | 待填 |
| shell 读凭据 | `cat ~/.aws/credentials` 判为 `safe`（`shell_tool.py:18` 无 `.aws`；`:118-122` 前缀判定） | 工作区外审批；凭据目录硬拒绝 | 待填 |
| 输出脱敏 | 只覆盖已配置密钥 / `Bearer` / `sk-`（`config.py:711-720`），`AKIA…` 不遮 | 覆盖常见凭据格式 | 待填 |
| MCP 子进程环境 | 配了 `env:` 时 `{**os.environ, **srv.env}`（`mcp_client.py:418`）；未配时 SDK 6 变量 allowlist | 父进程环境不透传 | 待填 |
| 审批记住 scope | 进程级全局 `set`（`hub.py:350`），重启即失效；shell 已按命令前缀（`shell_tool.py:125-155`） | 按连接/会话隔离 | 待填 |

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

## 4. 性能

| 指标 | 基线 `22ca135` | 目标 | 实测 |
|---|---:|---:|---:|
| 冷启动到可输入 | 未采集（改造 PR 不得回退 >10%） | ≤ 基线 | 待填 |
| 空闲内存 | 未采集 | ≤ 基线 | 待填 |
| Swift debug 构建（`swift test`） | 18.5 s | 不回退 >10% | 18.5 s（Phase 0） |
| Swift release 构建 | 27.6 s | 不回退 >10% | 27.6 s（Phase 0） |

> 构建时间为本机 arm64 实测，仅用于同一台机器上的前后对比。

> 性能基线需要真实启动 App 采样，在 Phase 1b 前补采一次；本轮不以性能为主目标，只做防回退。
