# 启明星 v1.1 产品设计：Native Interaction Model（Final）

> 日期：2026-11-28  
> 状态：**v1.1 Final（v0.3 定稿增补，冻结候选）**；工程 Phase 0–5 与 Godot 迁出均已落地  
> 基线：`main @ 6faa281`（296 Python / 90 Swift；包体 418 MB）  
> 关联：`docs/restructuring-plan-v2.md`（工程顺序）、`docs/restructure-metrics.md`（指标）、`docs/interaction-prompt-spec.md`（现状交互，未覆盖 PR #7–#9 的四个新交互）  
> 标记：`[现状]` 已在代码/报告中验证；`[目标]` 本文档要求；`[决策]` 已拍板；`[不做]` 明确不做；`[待定]` 未决

---

## 0. 一页结论

**产品一句话**：启明星是长在 macOS 里的 AI 能力层——从系统入口拿到上下文，用一个 turn 产出可直接使用的结果；不是聊天页。

**五条不可违反的规则**：

1. Quick Entry 只呈现当前 turn，不出现历史列表或消息流。
2. 每个结果至少有一个 native 出口（插入 / 替换 / 保存 / 打开 / 派任务 / 复制）；纯文本是兜底，不是默认终点。
3. 多入口、一个舞台；新入口不得新增常驻表面。
4. 只读动作直接执行；可逆写入直接执行 + 预览/撤销；不可逆或外部动作审批。
5. 数字人 / 语音是后置的可替换壳与模态，不得反向绑架当前架构。

**v1.1 主题**：**选中即用**——选中文字 → Services 或快捷键 → QE → 替换 / 插入。

### 0.1 v0.2 修订（针对 review 的 4 个缺口 + 3 个暗坑）

| # | 修订 | 落点 |
|---|---|---|
| R1 | 选中回写补齐 macOS 边界：Safari 只读正文 vs 可编辑输入框、异步 Services、焦点校验、撤销快照 | §2.6、§4.4、§5.1、§7 |
| R2 | QE「单轮视图」与「连续微调」分开：视图只显示最新 turn，上下文在短周期内连续 | §2.3、§5.1 |
| R3 | 顶部主动作条与卡片动作分层；改写 turn 增加纯净输出契约与 `primary_payload` | §2.5、§4.2、§5.1 |
| R4 | 指标改硬：常驻窗口/入口 ≤5、承载结果的表面 ≤3、删除 `PinnedNotePanel` 单列 | §2.11、§5.3 |
| R5 | Drop Pocket 全文统一为「纯输入暂存队列，禁止写入结果」 | §2.11、§4.3、§5.1 |
| R6 | 本地/远端动作路由写死白名单；只有 `more_hint` 走 WS | §2.5、§7 |
| R7 | 补 `NSServices` 缓存、审批文案、`TurnSourceContext` 前置 | §4.1、§4.4、§7 |
| R8 | 焦点校验允许 QE 自身 / 目标 pid，只在切到第三个 App 时降级；AX 按目标 pid 查询、文本归一化 | §2.6 |
| R9 | 撤销必须先重选区（`startLocation` + `insertedText`）；不支持设置选区时回退剪贴板 | §2.6 |
| R10 | 追问继承 `TurnSourceContext` 与 `turn_mode`，避免改写续轮退回聊天模式 | §2.3 |
| R11 | 审批 scope 收口排进 P0；`turn_mode` / `source_text` 作为前向兼容可选字段（`Optional[...] = None`）并重导 schema 零 diff | §2.5、§4.1、§4.2、§5.2 |
| R12 | 指标基线校正：承载最终结果的现状是 **4 个**（含 `PinnedNotePanel`），不是 12+ | §2.11、§5.3 |
| R13 | 跨模型行为一致性（方案 A）：Router L0 三档、`task_dispatch` 反例、Gemini 检索过渡态与来源归一化、`PAR-01`–`PAR-08` 验收矩阵 | §2.13、§4.1、§5.2.1、§5.3 |

### 0.2 当前工程状态 `[现状]`

| 项 | 状态 | 证据 |
|---|---|---|
| Phase 1a 原生卡片 | **已完成** | `QMXOutputCardView.swift`、`BrainClient.swift:514`、`cards_json`、296 Python / 90 Swift |
| Phase 1b 删除 body_web | **已完成** | `body_web/` 不存在；`/` 返回 JSON；产品代码 0 引用；包体 418 MB |
| Phase 1c 安全门禁 | **已完成（代码 + 测试）** | loopback 绑定、Host/Origin/Content-Type 门禁、artifact 穿越修复、shell 默认拒绝、MCP env allowlist；`tests/test_security_gate.py` |
| Phase 2 Godot 迁出 | **已完成（主仓移除）** | `6c8128f`；`body_godot/` 不存在 |
| Phase 3 窗口仲裁 | **已完成** | `Core/Window/`（WindowRole / WindowCoordinator / FocusArbitrator）；QE/Settings/Media/Workspace/Approval 已接线 |
| Phase 4–5 测试与拆分 | **已完成** | 90 XCTest；`main.py` 1,161→502、`hub/core.py` 3,207→1,617、`sqlite_store.py` 2,517→264、`config.py` 2,069→72 |
| 产品增量：动作路由 / 主动作条 / 纯净改写 / 撤销 / 表面审计 / 任务角标 / 原生闭环 | **未完成** | 见 §3、§4 |

**成功标准**：纯卡片回答可见且可回放；选中文字在三类 App 里走完「改写 → 替换」；QE 没有历史列表；承载结果的表面不增加。

---

## 1. 产品论点

### 1.1 身份

| 项 | 结论 | 标记 |
|---|---|---|
| 当前身份 | 长在 macOS 里的 AI 能力层；不是聊天页，不是数字人优先 | `[决策]` |
| 数字人 | 未来可替换的展示壳；Godot 壳已迁出主仓，不参与当前产品身份 | `[决策]` |
| 语音 | 与文字同等重要，但当前后置；架构上按同一 turn 的输入/输出模态预留 | `[决策]` |
| 推理位置 | 云端为主；本地 LLM 只有小到几百 MB 且明显划算才考虑，否则不做；`NATIVE` 指交互原生，不是运行时原生 | `[决策]` |
| 离线 | 不作为产品承诺；网络失败只做明确报错 + 重试 | `[决策]` |

`[现状]` README 仍自称「通用 AI 桌面数字人助手」与 v1.0.4（`README.md:1-3`），虽然测试数已更新到 296（`:23`）。v1.1 发布前应改为「原生 macOS AI 助手」，版本号随发布更新。

### 1.2 用户与场景

- 单人、本机、macOS 13+ 的重度用户；中文输入优先；隐私敏感；能配置 API Key 与 MCP。
- 典型场景：选中文字改写 / 解释 / 总结；拖拽文件总结；圈选屏幕问答；Ghost Write 就地写；快问快答；后台任务；Workspace 回顾。
- `[不做]` 零配置通用消费者、Windows 首发、移动端、云端托管。

### 1.3 不是什么

- 不是 ChatGPT 桌面版或另一个聊天客户端。
- 不是数字人优先、语音优先。
- 不是任务中心或工作流编排器。
- 不是本地模型 / 离线产品。

---

## 2. 交互模型

### 2.1 多入口 → 同一 turn

| 入口 | `[现状]` | `[目标]` | v1.1 |
|---|---|---|---|
| 选中文字 + Services / 快捷键 | 快捷键可开 QE；无 Services；无自动捕获选中文字 | 改写 / 解释 / 总结三动作 | **是** |
| 拖拽 / Drop Pocket | `DropPocketController` 存在，带暂存架 | 纯输入队列，结果进 QE / Workspace | 审计 |
| 圈选 / Spatial Lasso | `SpatialLassoController` 存在，带悬浮便签 | 输入浮层；便签进 Workspace | 审计 |
| Ghost Write | Accessibility API 就地把结果写回目标 App | 保留；预览 / 撤销 / 焦点校验 | 保持 |
| 菜单栏 Popover | 启动器 + 状态 + 最多 2 条任务 | 只做启动器 / 状态；不展示完整结果 | 审计 |
| Shortcuts / App Intents | 无 | 后续版本全量 | `[不做]` v1.1 |

规则：所有入口都把上下文包成同一个 turn；入口本身不拥有结果。

### 2.2 结果舞台（Quick Entry）

- `[决策]` QE 只呈现当前 turn；历史自动进 Workspace；没有「上一轮」按钮，只有一行「上一轮已存入工作区」。
- `[决策]` 版面：全文继续流式；结果完成后，主动作固定在顶部 / 结论旁，不用滚到底；卡片与媒体折叠在下方。
- `[现状]` 仍是文本优先：`answerView(turn)` 之后才 `cardsView(turn)`（`QuickEntryPanel.swift:1193-1196`），动作只存在于卡片内部；没有独立主动作条。
- `[缺口]` 主动作条、结果级动作路由、卡片/媒体折叠。

### 2.3 单轮视图 vs 连续微调

`[决策]` **视图单轮化，上下文短周期连续化**：

- QE 永远只渲染最新 turn，不出现 transcript。
- 在同一个 QE 生命周期内（或 3–5 分钟内、且选中源未变），模型保留最近几轮上下文，支持「语气再正式一点」「缩短一半」这类连续微调。
- 顶部只给一个极轻的「承接上一轮 · 重置」提示；点「重置」、点「新对话」、切换选中源或超时，才开启新上下文。
- `[现状]` Brain 的 session 在 `clear_history` 前持续存在（`hub.py:760-771`）；「新对话」会清空壳侧消息并归档 session（`QuickEntryPanel.swift:1782`、`BrainClient.swift:264-274`）。后端已支持连续上下文，缺的是 UI 规则与提示。

`[验收]` 改写后追一句「再正式一点」不需要重新选中；QE 仍只显示最新 turn；点「重置」后不再承接。

**上下文与模式继承** `[决策]`：在同一个 `TurnSourceContext` 有效期内（未点「重置」/「新对话」、未超 3–5 分钟窗口、未切换新的选中源），用户在 QE 中发起的追问 turn **自动继承原 turn 的 `TurnSourceContext` 与 `turn_mode`（`rewrite` / `explain` / `summarize`）**：

- 原 turn 为 `rewrite` 时，追问（如「语气再正式一点」「缩短一半」）继续走纯净输出契约，顶部 `replace` 保持可用；
- 原 turn 为 `explain` / `summarize` 时，追问保持常规回答模式，不被强制切为 `rewrite`；
- 只有普通提问（`turn_mode = default`）不继承写回上下文。

### 2.4 turn 结果模型

`[决策]` turn 是结果的最小单位：

```text
turn result = 文本 + 内容卡 + 媒体 + 产物 + 任务
```

- 卡片只是内容块，不是统一容器；task / artifact / media 不叫 card。
- `[现状]` `LiveTurn` / `ChatEntry` 已聚合文本、cards、media、sources；`WorkspaceItem` 已有 `cards`（`WorkspaceWindowController.swift:42`）。
- `[现状]` 任务与产物仍有独立生命周期；不要为了「统一」把它们塞进卡片协议。

### 2.5 内容卡、动作分层与纯净输出

`[决策]` 卡片类型收敛为内容类：`markdown` / `code` / `table`（显示提示）；`spoiler_steps` 作为唯一特殊类型保留。task / artifact / media 不纳入 card kind。

**动作分两层**：

| 层 | 作用对象 | 动作 | 执行方 |
|---|---|---|---|
| 顶部结果条 | 整轮 `primary_payload` | `replace` / `insert` / `copy` / `save` / `open` | 客户端 |
| 卡片内动作 | 该卡片自身 | `copy`（卡片正文）/ `dismiss` / `more_hint` | 客户端（`more_hint` 除外） |

- 卡片默认不出现 `replace` / `insert`，除非它被显式标记为整轮 `primary_payload`。
- 两层都有 `copy` 时，标签必须区分（「复制整段」vs「复制代码」），不能出现两个同名按钮。

**纯净输出契约** `[决策]`：

- 由「改写 / 插入」入口触发的 turn，必须携带 `mode = rewrite`（或等价字段）与 `source_text`；Brain 使用独立的 rewrite 提示词，只输出可替换文本本体，不带开场白、结束语、解释或 Markdown 围栏（除非内容本身需要）。
- 该 turn 的 `primary_payload` = assistant 正文；顶部 `replace` / `insert` 只能使用 `primary_payload`，绝不把聊天式回答整体写回目标 App。
- 验收时用固定 fixture 检查：替换内容不包含「好的」「以下是」「希望这能帮到你」这类前后缀。

**协议前向兼容增量** `[决策]`：

- 客户端上行用户消息 Payload（`protocol/models.py`）新增可选字段：`turn_mode: Optional[Literal["default", "rewrite", "explain", "summarize"]] = None` 与 `source_text: Optional[str] = None`（默认空，不影响既有客户端）。
- 修改完成后必须执行 `python -m protocol.export_schema`，确保磁盘 `protocol.schema.json` 与 `build_protocol_schema()` 零 diff。

**动作路由白名单** `[决策]`：

```text
本地：dismiss / copy / insert / replace / save / open
远端：more_hint
未知：忽略 + notice，绝不拼进 prompt
```

`[现状]` 客户端把卡片动作原样回传服务端（`QMXOutputCardView.swift:97-103`、`QuickEntryPanel.swift:1255-1265`、`BrainClient.sendCardAction:729`）；服务端按 `card_id → 动作` registry 校验（`hub.py:775-806`）。本地动作尚未落地。

### 2.6 原生闭环的 macOS 边界

`[决策]` 选中 → 改写 → 替换不是一条同步字符串回传，而是一条异步回合：

```swift
struct TurnSourceContext {
    let appName: String
    let bundleId: String
    let pid: pid_t
    let windowTitle: String?
    let selectedText: String
    let isEditable: Bool        // AX settable / text role / contenteditable
    let capturedAt: Date
    let selectionRange: NSRange?
}

struct UndoReplacementSnapshot {
    let targetPid: pid_t
    let bundleId: String
    let originalText: String
    let insertedText: String
    let startLocationUTF16: Int?   // 写回前选区起始 UTF-16 偏移
    let writtenAt: Date            // 用于 10 秒撤销窗口判定
}
```

规则：

1. **入口只捕获，不回写**：`NSServices` handler 必须立即返回；它只捕获选区与 `TurnSourceContext`，拉起 QE，绝不能阻塞等待大模型。
2. **写回前校验焦点（防自阻、防误写第三方 App）**：
   - 允许触发写回时的前台 App 为 **`context.pid` 本身**或 **`Qimingxing`（因点击按钮 / 追问微调导致的焦点转移）**；仅当用户主动切去**第三个无关 App** 时，拒绝盲写并降级为「复制结果 + 提示」。
   - 校验目标控件时，直接通过 `AXUIElementCreateApplication(context.pid)` 查询目标应用的当前焦点元素；比对选区文本时先做首尾空白与换行符（`\r\n` / `\n`）归一化；若目标控件（如部分网页编辑器）不支持读取 `kAXSelectedTextAttribute`，允许凭 `pid + focusedElement` 有效性与 `isEditable == true` 放行，并在写回前激活目标 App。
3. **Safari 分两种口径**：
   - 网页正文（只读）：`isEditable = false`，只提供「解释 / 总结 / 复制 / 存工作区」，隐藏 `replace` / `insert`。
   - 输入框 / `contenteditable`：`isEditable = true`，允许「改写 → 替换」。
4. **撤销不依赖目标 App 原生 Undo 栈（重选区后还原）**：
   - AX 替换完成后，目标 App 光标通常会折叠至新文本末尾（选区长度变为 `0`），**禁止直接向 `kAXSelectedTextAttribute` 二次写入 `originalText`（会导致原文被追加在末尾）**。
   - 写回前记录 `UndoReplacementSnapshot`；10 秒窗口内用户点击「撤销替换」时：
     1. 优先通过 `kAXSelectedTextRangeAttribute` 重新框选 `NSRange(location: startLocationUTF16, length: insertedText.utf16.count)`；
     2. 读回该范围文本并与 `insertedText`（归一化后）核对一致，确认无外部改动后再写回 `originalText`；
     3. 若目标 App 不支持设置 `AXSelectedTextRange` 或核对不一致，**中止自动改写**，将 `originalText` 写入剪贴板并提示「当前应用不支持自动撤销，原文已复制到剪贴板」。
5. **验收 App**：备忘录、Safari（只读正文 + 输入框两种）、TextEdit；不承诺全 App。

`[现状]` `GhostWriteEngine` 已有 AX 改写 + 剪贴板回落（`GhostWriteEngine.swift:93-262`），但没有 `TurnSourceContext`、焦点校验或撤销快照。`[缺口]` 上述 5 条全部。

### 2.7 动作与安全三档

| 档 | 动作 | 默认行为 | 审批 |
|---|---|---|---|
| 只读 | copy / open / reveal / quick look | 直接执行 | 无 |
| 可逆写入 | insert / replace / save draft | 直接执行 + 预览 / 撤销 | 无（首次可提示） |
| 不可逆 / 外部 | 发送邮件、删除、提交表单、执行 shell、跨 App 写 | 先审批 | 按 `tool + canonical args` 授权，不只按工具名 |

`[现状]` 审批已按 client 隔离（`hub.py:350,738-743`）；shell 的 remember 绑定到非变更命令前缀（`shell_tool.py:181`），其他工具仍默认按工具名（`tools/base.py:173`）。`[目标]` 写操作统一到 `tool + canonical args`；UI 文案改成「允许本次相同操作 / 参数」，不能写「始终允许此工具」。

### 2.8 历史与 Workspace

- `[决策]` 新一轮开始时，上一轮自动存 Workspace；QE 只留一行提示；不提供「上一轮」按钮。
- `[决策]` Workspace 是时间线，不是结果库：按天分组 turn，类型过滤 + 搜索；每行是结果摘要，不是聊天气泡。
- `[现状]` WorkspaceStore 持久化 messages（最多 500）与 `cards`；`QuickEntryPanel` 有 `workspaceLink`；Wing 展示最近 3 轮（`WorkspaceWing.swift:156`），当前只读。
- `[目标]` Wing 标注「最近」，明确只读；Lasso 便签迁移后进入 Workspace 结果项；不新增库视图。

### 2.9 媒体

- `[决策]` 媒体预览是 **detail view**，不是 stage：附着侧翼是 peek，独立窗口是 deep。
- `[现状]` `MediaViewer` / `MediaStripView` 已实现侧翼与独立窗；表格去重已做（`MarkdownTable.swift:635-645`）。无需新增窗口。

### 2.10 任务

- `[决策]` 派发 → 菜单栏角标 → 完成 Toast / 系统通知 → Workspace 任务卡；不建任务中心，不在 QE 常驻任务条。
- `[现状]` 菜单栏 Popover 展示最多 2 条活跃任务（`MenuBarController.swift:122`）；状态栏角标目前只反映 Drop Pocket 暂存数（`:48`），不反映任务；Toast 是 `.statusBar` 非激活面板（`TaskToast.swift:288,327`）。
- `[目标]` 增加任务角标（活跃任务数）；保留 Workspace 任务卡与 Toast 兜底。

### 2.11 表面角色与审计

| 表面 | `[现状]` | `[决策]` 目标角色 | v1.1 动作 |
|---|---|---|---|
| Quick Entry | 主面板，文本优先 | Stage（当前 turn） | 主动作版面 |
| Workspace Wing | QE 附着，最近 3 轮 | Workspace peek，只读 | 标注「最近」 |
| Workspace Window | 三栏时间线 | Stage（历史） | 保留；行改结果摘要 |
| Media 侧翼 | 附着预览 | Detail peek | 保留 |
| Media 独立窗 | 独立预览 | Detail deep | 保留 |
| Approval / Clarification | 独立浮窗，主动激活 | Decision overlay | 保留 |
| Settings | 独立窗口 | Stage（配置） | 保留 |
| Task Toast | 非激活顶部横幅 | Passive | 保留；补任务角标 |
| Drop Pocket | 拖拽暂存架 | **纯输入队列** | 禁止写入结果 |
| Spatial Lasso | 圈选输入 + 浮窗 | Input overlay | 保留输入 |
| PinnedNotePanel | 悬浮便签结果 | **删除** | 迁移到 Workspace 结果项 |
| Walkie-Talkie | 独立语音浮层 | Input overlay | 保留实验 |
| Permission Guide | 权限引导 | Decision / security | 保留 |
| 菜单栏 Popover | 启动器 + 状态 + 任务 | Launcher / status | 不展示完整结果 |
| Ghost Write | 就地写回目标 App | Inline result | 保留；预览 / 撤销 |
| Avatar | 外部 Godot 插件 | External | 不参与 |

**硬指标** `[决策]`：

- **常驻窗口 / 入口 ≤5**：QE、Workspace、Settings、菜单栏、Drop 队列（仅在可见时计）。现状 `12+` 是全部窗口 / 面板拥有者，包含审批、权限、Walkie 等不承载结果的浮层。
- **承载最终结果的表面 ≤3**：QE、Workspace、目标 App 内联（Ghost Write）。现状是 **4 个**：上述 3 个 + `PinnedNotePanel`；Media viewer 视为结果的 detail view，不计入。
- **`PinnedNotePanel` 删除**，单列验收项；Lasso 便签只能进 Workspace 结果项。
- 其他表面只能是输入浮层、决策浮层、被动通知或 detail view；不得展示最终结果。

### 2.12 禁止清单

- QE 历史列表、聊天 transcript、消息气泡、卡片栈。
- 为「统一」把 task / artifact / media 塞进 card kind。
- 新增常驻任务中心、Workspace 结果库视图、第二个媒体 stage。
- 把卡片动作拼进 prompt 开新回合（未知动作必须忽略或报错）。
- 本地模型 / 离线承诺。
- 向 Drop Pocket 写入任何输出 / 结果项。

### 2.13 跨模型行为一致性（方案 A）

> 目标：单模型用户（无论仅配置 DeepSeek、仅配置 Gemini，或后续接入 Claude / OpenAI / Qwen）在启明星前台获得**完全一致的执行节奏与等待体感**——体验跟任务轻重走，不跟模型厂商走。

`[现状]` `brain/prompts.py:30` 只有一条「后台任务」说明，没有同步/异步三档准则；`GeminiNativeAdapter` 存在（`brain/providers.py:194`），`groundingChunks` 已有归一化函数（`brain/gemini_native.py:276`），但没有 1.5s 检索过渡态；`cli eval --parity` 尚不存在（现有 `brain/eval_runner.py` 可复用）。以下均为 `[目标]`。

#### 2.13.1 Router L0 统一路由准则（静态前缀，零破坏 Prefix Cache）

在 Router L0 静态系统提示词中固定写入以下三档判定规则（不随轮次变化，保证 `PCH-01`–`PCH-03` 缓存前缀字节稳定）：

```markdown
## 执行节奏准则（同步直出 vs 后台派单）
必须按任务复杂度决定响应方式，与底层模型无关：
1. **同步直出（严禁派发后台任务）**：
   - 普通问答、概念解释、文案润色、代码片段讨论；
   - `turn_mode` 为 `rewrite` / `explain` / `summarize` 的选中文本处理与连续微调；
   - 仅需 1 次轻量检索即可回答的事实/资讯/价格/天气查询（优先使用当前可用的联网搜索能力直接查完作答）；
   - 单步系统速查或轻量写入（查/建单条备忘录、日历、提醒、搜图、读剪贴板、读单个短文档 `doc_inspect`）。
2. **必须调用 `task_dispatch` 后台异步执行**：
   - 需要跨多个网页深读（`web_fetch`）或交叉对比 3 个以上来源的调研分析；
   - 需要调用本地 Shell（`dev_ops`）、多文件处理、表格加工、生成/导出 `.xlsx/.docx/.pdf` 等本地文件产物；
   - 涉及多步跨应用联动、浏览器自动化或预计执行超过 15 秒的复杂任务。
   - **硬约束**：即使当前模型具备服务端内置联网搜索（如 Gemini Grounding），只要任务要求生成本地文件、多步整理或调用本地工具，也必须优先派发 `task_dispatch`，不得尝试在单轮搜索回复中硬扛。
```

#### 2.13.2 收紧 `task_dispatch` 工具描述（抑制过度委派）

更新 `brain/tools/orchestration_tools.py:47` 中 `task_dispatch` 的 `description`，显式加入反例禁令：

> 派发**复杂、多步或耗时超过 15 秒**的后台任务（如多源深度调研、本地文件/表格处理、文档生成、Shell 诊断、浏览器自动化）。
> **禁止用于**简单事实查询、单次联网搜索即可回答的问题、选中文本改写/解释/总结、或单步日历/提醒/备忘录/文档读取操作——此类请求必须在当前对话直接同步完成。

#### 2.13.3 同步等待体感与引用来源拉平

针对客户端搜索（DeepSeek 等走 `web_search`）与服务端黑盒搜索（Gemini 原生 Google Search Grounding）的交互温差，在 Brain 适配层做两点统一：

| 体验维度 | 客户端搜索（DeepSeek 等） | 服务端搜索（Gemini Grounding） | 统一拉平要求 |
|---|---|---|---|
| **检索过渡提示** | 触发 `web_search` 时自动推送工具执行态 | 服务端静默检索 3–8 秒，前端光标静止易误判卡死 | `GeminiNativeAdapter` 在启用原生搜索的流式请求中，若 **1.5 秒内未收到首字**，由 Brain 主动推送轻量过渡态（`正在联网检索…`），首字到达后自动清除 |
| **来源引用展示** | 由 `web_search` 结果提取标题与 URL | 由服务端 `groundingMetadata.groundingChunks` 返回 | 在 `GeminiNativeAdapter` 收口阶段，将 `groundingChunks` 归一化为与 `web_search` 相同的 `sources` 结构，统一在 QE / Workspace 折叠展示 |

验收矩阵见 §5.2.1（`PAR-01`–`PAR-08`）；发版前必须用 `cli eval --parity` 对 DeepSeek 与 Gemini 各跑一轮。

---

## 3. 当前实现盘点（1a / 1b / 1c / Phase 2）

| 能力 | 状态 | 证据 |
|---|---|---|
| 原生 `output.card` 解码与渲染 | **已完成** | `BrainClient.swift:514`；`QMXOutputCardView.swift` |
| 卡片挂在当前 turn | **已完成** | `BrainClient.swift:514-524` |
| card-only 回合可见 | **已完成** | `QuickEntryPanel.swift:1212-1225` |
| 卡片持久化 `cards_json` | **已完成** | `brain/memory/sqlite_store.py:61,144,333-335` |
| card-only assistant 行 | **已完成** | `hub.py` `stored_assistant_content` |
| 动作 registry 校验 | **已完成** | `hub.py:775-806` |
| 裸标记剥离 | **已完成** | `QMXCardMarkers`（`MarkdownTable.swift:250`）；`StreamingEngine.swift:600-620` |
| 表格去重 | **已完成** | `MarkdownTable.swift:635-645` |
| Workspace `cards` | **已完成** | `WorkspaceWindowController.swift:42` |
| body_web 删除 / `/` JSON | **已完成** | `body_web/` 不存在；产品代码 0 引用 |
| Phase 1c 安全门禁 | **已完成（代码 + 测试）** | `tests/test_security_gate.py`；loopback 绑定（`main.py:1138-1143`）；shell 凭据 denylist（`shell_tool.py:19`）；artifact 穿越校验（`artifact_store.py:258-280`）；MCP env allowlist（`mcp_client.py:45-46`） |
| Godot 主仓移除 | **已完成** | `6c8128f`；`body_godot/` 不存在 |
| CI + Swift XCTest | **已完成** | `.github/workflows/ci.yml`；296 Python / 90 Swift |
| 卡片本地动作（copy/insert/replace/save/open） | **未完成** | 服务端只发 `dismiss` / `more_hint`；客户端全部回传 |
| 顶部主动作条 / `primary_payload` | **未完成** | 文本在前，无结果级动作条 |
| 改写纯净输出 | **未完成** | 无 rewrite 模式 / 无前后缀剥离 |
| `turn_mode` / `source_text` 协议字段 | **未完成** | `InputTextPayload` / `InputActionPayload` 目前只有 text/attachments/action/data（`protocol/models.py:202-210`） |
| 追问继承 `TurnSourceContext` / `turn_mode` | **未完成** | 当前无 rewrite 模式与继承规则 |
| `TurnSourceContext` / 焦点校验 / 撤销快照 | **未完成** | `GhostWriteEngine` 只有 AX 改写 + 剪贴板回落 |
| code/table 卡片版式 | **部分** | Markdown 可渲染，但无独立卡片类型/徽标 |
| 表面审计 | **未完成** | `PinnedNotePanel` 仍在（`SpatialLassoController.swift:161`）；Wing 未标注；Drop 仍有暂存架 |
| 任务角标 | **未完成** | 角标只反映 Drop Pocket（`MenuBarController.swift:48`） |
| 原生闭环（Services / 选中捕获） | **未完成** | 无 `NSServices` / `AppIntents`；快捷键只开 QE |
| 审批 scope 到 `tool + args` | **部分** | 已按 client 隔离；shell 用命令前缀，其他工具按工具名 |
| 文档同步 | **部分** | `restructuring-plan-v2` 仍写「未开发」；README 仍是「数字人」v1.0.4；`SECURITY.md` 不存在 |

---

## 4. v1.1 剩余切片

### 4.1 顺序

1. **P0：验证 1a/1b/1c + Phase 2 + 审批收口**
   - 跑通现有全量回归、深 / 浅色 UI 截图与重启回放；补 `SECURITY.md`；同步 README / `restructuring-plan-v2` / `restructure-metrics`。
   - 将其他写工具审批记住粒度统一收敛到 **`tool + canonical args`**，审批 UI 文案改为「允许本次相同操作 / 参数」（禁止出现「始终允许此工具」）。
2. **P1-A：`TurnSourceContext` + 结果舞台 + 协议增量**
   - 先定义 `TurnSourceContext` 与 `UndoReplacementSnapshot`；
   - 扩展 `protocol/models.py`（`turn_mode`、`source_text`）并同步 `protocol.schema.json`；
   - 实现 QE 顶部主动作条、动作分层、客户端本地动作白名单路由与卡片 / 媒体折叠。
3. **P1-B：纯净改写 + 撤销 + 跨模型一致性底座**
   - 落地 `rewrite` 提示词契约、`primary_payload` 提取、连续微调上下文 / 模式继承与 10 秒重选区撤销；
   - 同步落地 §2.13：Router L0 三档路由准则、`task_dispatch` 描述反例、`GeminiNativeAdapter` 1.5s 检索过渡态与 `groundingChunks` 来源归一化（与 `rewrite` 提示词同批改动静态前缀，确保 Prefix Cache 一次性对齐）；
   - 补齐离线单测护栏（静态前缀稳定性、废话过滤 fixture、Gemini 过渡态与来源归一化）。
4. **P1-C：表面审计**
   - 删除 `PinnedNotePanel`（Lasso 便签迁入 Workspace）；Wing 标注「最近」且只读；Drop Pocket 锁定纯输入队列；菜单栏补活跃任务角标。
5. **P1-D：原生闭环**
   - `Info.plist` 注册 3 个 `NSServices`（改写 / 解释 / 总结）+ 快捷键选区捕获；
   - 落地防自阻焦点校验与 Safari 只读正文 vs 可编辑输入框双口径。
6. **P2：真机验收、跨模型对照实测与发布**
   - 执行 `/Applications` 安装 + `/System/Library/CoreServices/pbs -flush`，完成三 App 闭环真机验收；
   - 执行 `cli eval --parity` 真机跨模型对照验收（DeepSeek + Gemini 各跑一遍 `PAR-01`–`PAR-08`），结果记入 `docs/restructure-metrics.md`；
   - 验证 `codesign --verify --deep --strict` 与包体 ≤418 MB 基线。

### 4.2 结果舞台

- 增加顶部结果条：`replace` / `insert` / `copy` / `save` / `open`，只作用于 `primary_payload`。
- 协议：`InputTextPayload` / `InputActionPayload` 增加可选 `turn_mode`、`source_text`；重导 `protocol.schema.json`，旧客户端不传时行为不变。
- 卡片内动作只作用于卡片自身；`replace` / `insert` 默认不出现在卡片上。
- 动作可用性由 `TurnSourceContext` 决定：有可编辑选区才显示 `replace`，有写入能力才显示 `insert`，有文件才显示 `open` / `save`。
- 动作路由按 §2.5 白名单：本地动作不经过 WebSocket；`more_hint` 才发 `sendCardAction`。
- 动作条在结果完成后出现，位于顶部 / 结论旁；卡片与媒体可折叠。

### 4.3 表面审计

- Wing：标注「最近」；只读；不新增回复 / 重试。
- Lasso：删除 `PinnedNotePanel`；便签转为 Workspace 结果项（note）。
- Drop：保留暂存架作为**纯输入队列**；禁止写入任何输出 / 结果项。
- 菜单栏：只做启动器 / 状态；任务用角标，完整详情进 Workspace。
- 移除任何只在浮窗里存在的「结果」；结果只允许出现在 QE、Workspace、目标 App 内联。

### 4.4 原生闭环

- `NSServices`：在选中文字上提供「用启明星改写 / 解释 / 总结」；handler 只捕获 `TurnSourceContext`，立即拉起 QE。
- 快捷键：若当前有选中文字，自动带入 QE；没有则普通提问。
- 结果落到 QE，顶部主动作为 `replace`（`isEditable`）或 `insert`（无选区但有目标 App）；`replace` 带 10 秒撤销。
- 写回前校验前台 App / pid / AX 元素 / 选区文本；允许 QE 自身或目标 pid，只在切到第三个 App 时降级为复制 + notice。
- 追问继承：同一 `TurnSourceContext` 有效期内，续轮继承 `turn_mode` 与选区；`replace` 保持可用；点重置 / 切换源 / 超时才失效。
- 撤销：写回前保存 `originalText` / `insertedText` / `startLocation`；撤销先重选区再写回原文；不支持设置选区的 App 回退剪贴板。
- 验收：备忘录、Safari（只读正文 + 输入框两种）、TextEdit；不改全 App。

### 4.5 不在本轮

完整 Shortcuts / App Intents 目录、完整语音 UX、首次启动向导、任务中心、Workspace 结果库、数字人壳、Windows、Sidecar。

---

## 5. 验收标准

### 5.1 产品场景

1. 纯卡片回答在 QE 可见；SQLite 有 assistant 行；重启后 Workspace 仍能渲染卡片。
2. 选中文字 → Services / 快捷键 → QE → 「替换」或「插入」可用；替换可撤销；外部动作走审批。
3. Safari 只读正文：不出现 `replace` / `insert`，只提供解释 / 总结 / 复制 / 存工作区；Safari 输入框：允许改写 → 替换。
4. 改写后追一句「再正式一点」不需要重新选中；QE 仍只显示最新 turn；点「重置」后不再承接。
5. 改写结果点 `replace`，写回内容不含「好的」「以下是」「希望这能帮到你」等前后缀。
6. 结果完成后，主动作无需滚动即可见；全文仍可读；卡片 / 媒体可折叠。
7. QE 只显示当前 turn；上一轮自动存 Workspace；没有历史列表或「上一轮」按钮。
8. Wing 只读最近 3 条并标注「最近」；Lasso 便签进入 Workspace；`PinnedNotePanel` 不存在；Drop 没有结果项；菜单栏有任务角标。
9. Workspace 每行是结果摘要；卡片 / 媒体 / 任务详情完整；类型过滤与搜索可用。
10. 任务：派发 → 角标 → 完成通知 / Toast → Workspace 任务卡；没有新任务窗口。
11. **撤销安全性**：在备忘录 / TextEdit 执行「改写 → 替换」后 10 秒内点击「撤销替换」，准确还原为 `originalText` 且不出现原文重复追加；若目标控件不支持重选区，则不破坏文档并提示原文已复制。
12. **跨窗防误写与同窗微调**：在 QE 内打字追问微调后再点 `replace` 仍能正确写回原 App；若用户切去第三个无关 App 后再点 `replace`，系统拒绝盲写并降级为复制 + 提示。
13. **跨模型一致体感**：分别仅配置 DeepSeek 与仅配置 Gemini（开启原生搜索）时，简单搜索 / 提醒 / 改写均在 QE 同步直出（Gemini 超过 1.5s 显示「正在联网检索…」且附带统一来源），复杂多源调研与本地写文件任务均异步派发 `task_dispatch`。

### 5.2 技术 / 回归

- Python ≥296、Swift ≥27；新增动作 / 上下文 / 撤销 / 原生闭环测试，目标 Swift ≥40。
- `body_web` 产品代码零引用；`/app.js`、`/styles.css` 404；`/` 返回 JSON。
- 1c 探针通过：外部 Origin、非法 Host、`text/plain` 写请求 403/415；artifact 路径穿越拒绝；`cat ~/.aws/credentials` 需审批；MCP env allowlist 生效。
- 审批 scope 验收：shell 变更命令不记住；非变更前缀可记住；其他写工具在切换参数后必须重新审批（目标 `tool + args`）。
- 协议：`turn_mode` / `source_text` 为可选字段；`protocol.schema.json` 重导后与模型一致；旧客户端不传字段时行为不变。
- 撤销：AX 选区设置可用时写回原文；不可用时回退剪贴板并提示；不得出现重复追加。
- 深 / 浅色 `--dump-ui` 截图正常；正文无裸 `<<<CARD>>>`；同一张表不重复。
- `scripts/build_macos_app.sh` + `codesign --verify --deep --strict` 通过；包体不高于 418 MB 基线（同口径）。

### 5.2.1 跨模型行为一致性验收矩阵（`PAR-01`–`PAR-08`）

采用「CI 离线护栏 + 发版真机 `--parity` 实测」两层策略（CI 不消耗付费 API Token；发版前及每次接入新模型厂商时必跑真机 `--parity`）：

| ID | 测试指令 / 场景 | 期望统一行为（DeepSeek / Gemini / 未来模型均需一致） | 门禁级别 |
| :--- | :--- | :--- | :--- |
| **PAR-01** | “查一下今天苹果公司股价 / 最新一条科技新闻” | **同步直出**（单次搜索直接答复，不得调用 `task_dispatch`） | **必过（Hard）** |
| **PAR-02** | “建一条明天上午 10 点开产品会的提醒” | **同步直出**（直接调 `macos_reminders_create`，不得调用 `task_dispatch`） | 常规跟踪 |
| **PAR-03** | “读一下这份表格有几个表、总金额多少” | **同步直出**（直接调 `doc_inspect`，不得调用 `task_dispatch`） | 常规跟踪 |
| **PAR-04** | “深度调研 5 个本地 AI 笔记工具的定价并生成本地 Markdown 报告” | **异步派单**（即使开启 Gemini 原生搜索，也必须调 `task_dispatch`） | **必过（Hard）** |
| **PAR-05** | “把这三张销售表合并去重、按大区汇总导出新 Excel” | **异步派单**（调用 `task_dispatch`，不得口头编造数据） | 常规跟踪 |
| **PAR-06** | “查一下本地仓库里为什么昨天有个单元测试挂了” | **异步派单**（调用 `task_dispatch(packs=["dev_ops"])`） | 常规跟踪 |
| **PAR-07** | **选中改写（`turn_mode=rewrite`）**：选中一段话触发「改写」 | **同步直出且纯净**：不得派后台任务；输出零开场白 / 结束语 / 多余围栏 | **必过（Hard）** |
| **PAR-08** | **连续微调**：在 `PAR-07` 后追问“再正式一点、缩短一半” | **继承 `rewrite` 且同步纯净**：仍不派后台任务，无废话前后缀，顶部 `replace` 保持可用 | **必过（Hard）** |

- **离线单测护栏（常规 `pytest` 必跑）**：
  - Router 静态前缀逐字节稳定（`PCH-01`–`PCH-03` 回归通过）；
  - `task_dispatch` 描述包含明确禁止反例；
  - `GeminiNativeAdapter` 1.5s 无首字触发检索状态帧，首字后清除，且 `groundingChunks` 正确归一化为 `sources`；
  - `turn_mode=rewrite` 提示词路由与废话前后缀检测 fixture 通过。
- **真机实测门禁（新增 `cli eval --parity`，复用 `brain/eval_runner.py`）**：发版前对 DeepSeek 与 Gemini 真实配置各跑 1 轮，`PAR-01 / PAR-04 / PAR-07 / PAR-08` 必须 100% 通过，其余项记录对照结果存档。

### 5.3 指标（硬口径）

| 指标 | 基线 | v1.1 目标 |
|---|---:|---:|
| Python 测试 | 296 | ≥296 |
| Swift 测试 | 27 | ≥40（动作 / 上下文 / 撤销 / 原生闭环） |
| 常驻窗口 / 入口 | 12+（全部窗口/面板拥有者） | **≤5**（QE、Workspace、Settings、菜单栏、Drop 队列） |
| 承载最终结果的表面 | **4**（QE、Workspace、Ghost Write、`PinnedNotePanel`） | **≤3**（删除 `PinnedNotePanel`） |
| `PinnedNotePanel` | 存在 | **删除** |
| 纯卡片回答可见 | 是 | 是（回归） |
| 选中→改写→替换 | 无 | 备忘录 / Safari 输入框 / TextEdit 通过；Safari 正文走复制/存 |
| 跨模型一致性（DeepSeek / Gemini） | 未测（同步/异步割裂） | 离线护栏全绿；真机 `PAR-01 / 04 / 07 / 08` **100% 通过** |
| `body_web` 产品代码引用 | 0 | 0 |
| 包体 | 418 MB | ≤418 MB（同口径） |

---

## 6. 明确不做

- 本地 LLM / 离线模式 / 端侧优先。
- QE 历史列表、聊天 transcript、消息气泡、卡片栈。
- 任务中心窗口、QE 常驻任务条。
- Workspace 结果库视图（只做时间线 + 过滤/搜索）。
- 完整语音 UX（ASR / TTS 打磨、唤醒词、barge-in）。
- 首次启动向导（推迟 v1.2）。
- Services / Shortcuts / App Intents 全量目录（v1.1 只做 3 个动作）。
- 数字人 / Godot 壳回流主仓（已迁出）。
- Windows 壳、App-Aware Sidecar、卡片协议大改。
- 向 Drop Pocket 写入结果。

---

## 7. 风险与取舍

- **`NSServices` 系统缓存**：开发态直接跑可执行文件通常不会出现在「服务」菜单。验收前必须把 `.app` 放到 `/Applications`（或完成 LaunchServices 注册），执行 `/System/Library/CoreServices/pbs -flush` 并重开目标 App。把这条写进验收前置，避免把缓存当 Bug。
- **Safari 可编辑边界**：网页正文默认只读，`replace` / `insert` 必须隐藏；只有 `textarea` / `contenteditable` / AX 可写元素才允许。验收分两条，不要合成一条。
- **异步回写与焦点丢失**：Services 不能等模型；写回前必须校验前台 App / pid / AX 元素 / 选区。用户切窗后点替换，只能降级复制，不能盲写。
- **撤销不依赖目标 App**：AX 写入不一定进目标 App 的 `Cmd+Z` 栈；必须用 `originalText` 快照做 10 秒撤销。剪贴板粘贴路径只作为后续优化。
- **本地 / 远端动作路由**：客户端先拦截本地动作；只有 `more_hint` 走 `sendCardAction`。未知动作忽略 + notice，绝不拼进 prompt。
- **审批记住 scope**：当前是 client + 工具名 / 命令前缀；写操作要收敛到 `tool + canonical args`。UI 文案改成「允许本次相同操作 / 参数」，不能写「始终允许此工具」。
- **表面指标口径**：不再用「12+ → ≤6」这种模糊说法；按 §2.11 的硬指标验收，`PinnedNotePanel` 单列。
- **云端依赖**：没有离线兜底；网络失败必须明确报错，并保留重试与已生成内容。
- **焦点校验防自阻**：点击 QE 按钮或追问微调时前台 App 会变为 `Qimingxing`，不可机械要求 `frontmost == context.pid`，需放行自身并仅拦截用户切往第三方 App 的场景。
- **AX 重选区兼容性**：部分非原生 / 网页控件不支持通过 `AXSelectedTextRange` 重新框选；遇此情况宁可降级复制原文提示用户，也绝不盲写覆盖或造成文本重复追加。
- **追问模式继承**：改写续轮必须继承 `turn_mode` 与 `TurnSourceContext`，否则退回聊天模式、`replace` 消失；解释 / 总结续轮不得被强制改写。
- **协议增量**：`turn_mode` / `source_text` 是可选字段，重导 schema；旧客户端不传时行为不变，避免 CI 协议一致性检查报错。
- **跨模型提示词漂移**：不同厂商模型对工具描述权重不同，仅靠单次调整提示词无法永久保证新模型不跑偏；必须将 `PAR-01`–`PAR-08` 固化为 `cli eval --parity` 发版与接入新模型时的必跑门禁。
- **1a/1b/1c 已完成**：不要重新规划或重做；本文档只覆盖剩余产品增量。

---

## 8. 待定 / 后续

- 首次启动向导（v1.2）：选厂商 → Key → 权限 → 试一句。
- 完整语音 UX：Walkie 正式化、TTS 音色、barge-in。
- 数字人壳契约：只读装饰事件、不拥有回合 / 审批 / 任务 / 热键。
- Shortcuts / App Intents 全量目录。
- Workspace 结果库视图（当时间线检索不够时）。
- 卡片协议扩展（仅当 task / artifact 真的需要 card 化）。
- 产品命名与 README 文案。
- `SECURITY.md`（v2.1 要求，当前不存在）。
- v1.1 发布后的真实成功率指标（动作使用率、替换撤销率、常驻表面数量）。

---

## 9. 变更记录

| 日期 | 变更 |
|---|---|
| 2026-11-27 | v0.1 初稿：产品论点、交互模型、表面审计、1a/1b 盘点、剩余切片、验收标准 |
| 2026-11-27 | v0.2 review 修订：Safari / 异步回写 / 焦点校验 / 撤销快照；单轮视图与连续上下文；动作分层与纯净输出；硬指标口径；NSServices 缓存与动作白名单 |
| 2026-11-27 | v0.3 Final：焦点校验允许 QE 自身；撤销先重选区；续轮继承 `turn_mode`；审批 scope 切片 + 协议可选字段；指标基线校正为 4 → ≤3 |
| 2026-11-28 | v0.3 定稿增补：修正焦点校验防自阻与 10 秒撤销重选区机制；补齐连续微调 `TurnSourceContext`/`turn_mode` 继承；补排审批 `tool + args` 与协议 Schema 增量切片；纠偏结果表面基线（4→≤3）；纳入跨模型行为一致性（方案 A + `PAR-01`–`PAR-08`） |
