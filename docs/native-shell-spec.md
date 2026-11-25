# 启明星原生双平台壳规格书

| 项目 | 内容 |
|---|---|
| 文档状态 | Draft — 待 Review |
| 文档版本 | 0.1 |
| 日期 | 2026-11-25 |
| 适用范围 | macOS 壳、Windows 壳、Brain 协议扩展、Godot 头像瘦身、Senses 跨平台补齐 |
| 不在范围 | Avatar rig 重写、Brain 语言重写、云端托管、移动端 |
| 相关文档 | `README.md`、`protocol.schema.json`、`config.example.yaml` |

---

## 1. 决策摘要

### 1.1 已确认决策

| ID | 决策 | 理由 |
|---|---|---|
| D1 | **Brain 继续使用 Python**，不重写 Rust | 负载为 I/O-bound；本地开销占回合延迟 <2%；重写成本与回归风险远高于收益 |
| D2 | **Avatar 继续使用 Godot，一套共享** | 透明置顶窗口、rig、漫游、贴边、拖拽为跨平台共性；重写收益低 |
| D3 | **托盘、设置、快捷输入、通知、文件交互由原生壳承载** | 这部分才是"原生体验"的主要感知面 |
| D4 | **Brain 是唯一状态源** | 两套壳必须共享配置、开关、会话、审批状态，否则必然漂移 |
| D5 | **先完成协议契约（Phase 0），再并行开发两壳** | 避免两套壳各自猜契约，后期无法合并 |
| D6 | **平台专属交互由壳执行**（reveal、打开、复制文件、Quick Look、通知） | 比在 Python 里堆平台分支更原生，也补齐 Windows 现有缺口 |

### 1.2 语言选型说明（ADR 摘要）

**决策**：保留 Python Brain。

**依据**：

- 回合延迟主要由 LLM、TTS/ASR、截屏往返决定，本地 Python 开销约 5–40ms（估算，需实测）。
- SQLite（FTS5/WAL）、Pillow、mss 均为 C 实现，Python 只是薄封装。
- asyncio + I/O-bound，GIL 不构成瓶颈；单用户桌面场景并发极低。
- MCP Python SDK、Pydantic/FastAPI、pytest 生态成熟；Rust 侧需自行补齐大量能力。
- 即使换成 Rust，整包仍包含 Godot、Python Senses、Swift/C# 壳，无法实现"单二进制"。

**重新评估的触发条件**（满足任一）：

1. 实测本地开销 p95 > 100ms，且排除网络/磁盘因素；
2. 转为多用户托管，目标 100+ 并发会话；
3. 本地推理（whisper/embedding/VAD/Opus）成为核心能力；
4. 单二进制分发成为硬性产品要求。

**若触发**：优先用 PyO3 抽取热点模块，而不是整体重写。

### 1.3 非目标

- 不追求 macOS 与 Windows 像素级一致；分别遵循 HIG 与 Fluent。
- 不替换 `body_web`；它保留为调试控制台。
- 不在本期实现自动更新、云端同步、移动端。

---

## 2. 总体架构

### 2.1 进程与连接

```
┌─────────────────────────────────────────────────────────────┐
│ brain (Python, 共享)                                         │
│ FastAPI + WS + SQLite + 调度 + MCP                           │
│ 127.0.0.1:18790                                              │
└───────▲──────────────────▲──────────────────▲───────────────┘
        │ WS               │ WS               │ WS
┌───────┴────────┐  ┌──────┴─────────┐  ┌─────┴──────────────┐
│ shell_macos    │  │ shell_windows  │  │ avatar_godot       │
│ Swift/SwiftUI  │  │ C#/WinUI 3     │  │ Godot 4             │
│ 菜单栏/设置/输入 │  │ 托盘/设置/输入  │  │ 头像/气泡/胶囊      │
└───────┬────────┘  └──────┬─────────┘  └────────────────────┘
        │ 启动/守护           │ 启动/守护
        └──────────┬──────────┘
                   │
          ┌────────▼────────┐
          │ senses (Python) │  macOS / Windows 双实现
          │ 窗口/截屏/音频/热键 │
          └─────────────────┘
```

- 每个平台**只运行一个原生壳**；壳负责启动并守护 Brain、Senses、Avatar。
- 三个客户端各自建立独立 WS 连接，Brain 按 `client_type` 与 capabilities 路由。
- 所有 HTTP 仅绑定回环地址，沿用现有 loopback 校验。

### 2.2 启动顺序

1. 壳启动，注册托盘/菜单栏图标（此时显示"正在启动"）。
2. 壳读取配置路径与端口，启动 `python -m brain.main`。
3. 轮询 `GET /health`，超时 15s；失败弹原生错误并保留日志入口。
4. 启动 `python -m senses.main --ws-url ... --http-base ...`。
5. 启动 Avatar（Godot 导出产物），注入 `QIMINGXING_WS_URL` / `QIMINGXING_HTTP_BASE`。
6. 壳建立自己的 WS 连接，发送 `system.hello`。
7. 收到 `system.welcome` + `system.privacy_state` 后，托盘图标切换为"已连接"。

### 2.3 环境变量

| 变量 | 用途 | 默认 |
|---|---|---|
| `QIMINGXING_CONFIG` | config.yaml 路径 | 仓库/应用数据目录 |
| `QIMINGXING_HOST` | Brain 绑定地址 | `127.0.0.1` |
| `QIMINGXING_PORT` | Brain 端口 | `18790` |
| `QIMINGXING_WS_URL` | 客户端连接地址 | `ws://127.0.0.1:18790/ws` |
| `QIMINGXING_HTTP_BASE` | 客户端 HTTP 基址 | `http://127.0.0.1:18790` |
| `QIMINGXING_APP_VERSION` | 版本注入 | 由打包脚本写入 |

### 2.4 版本口径

当前存在四处不一致：README（MVP 3.0）、`pyproject.toml`（0.6.0）、`brain/hub.py:SERVER_VERSION`（0.6.0-mvp6）、构建脚本（0.7.0）。

**规格要求**：引入单一 `version.txt`（如 `0.8.0`），构建时注入：

- `pyproject.toml` 版本；
- `SERVER_VERSION`；
- macOS `CFBundleShortVersionString`；
- Windows 安装器与程序集版本；
- `system.hello.app_version`。

---

## 3. 组件职责

| 能力 | Brain | Senses | Avatar (Godot) | 壳 macOS | 壳 Windows |
|---|---|---|---|---|---|
| 会话/记忆/上下文 | ✅ | — | — | — | — |
| LLM/TTS/ASR 路由 | ✅ | — | — | — | — |
| 工具/审批/调度/MCP | ✅ | — | — | — | — |
| 窗口侦测/隐私守卫 | 接收 | ✅ | — | — | — |
| 截屏 | 请求/路由 | ✅ | — | — | — |
| 全局热键 | 接收动作 | ✅ | — | — | — |
| 音频采集/播放 | 路由 | 采集 | 播放/TTS | — | — |
| 头像/气泡/表情 | 状态 | — | ✅ | — | — |
| 托盘/菜单栏 | — | — | ❌ 移除 | ✅ | ✅ |
| 设置窗口 | 配置 API | — | ❌ 移除 | ✅ | ✅ |
| 快捷输入 | 回合 API | — | ❌ 移除 | ✅ | ✅ |
| 钉图 | 产物 | — | ❌ 移除 | ✅ | ✅ |
| 原生通知 | 事件 | — | 可选 | ✅ | ✅ |
| reveal/打开/复制文件 | 路由 | — | — | ✅ | ✅ |
| 进程守护 | — | — | — | ✅ | ✅ |

**原则**：Brain 不直接调用任何平台 UI API；壳不直接读写 `config.yaml`（只走 HTTP）。

---

## 4. 协议规格

### 4.1 兼容策略

- 信封版本保持 `v: "1.0"`，本次全部为**增量变更**。
- 服务端 `parse_payload` 对未知事件返回 `None` 并忽略；Pydantic 配置 `extra="ignore"`。
- 客户端（Swift/C#）必须忽略未知字段与未知事件。
- `client_type` 新增枚举值属于兼容变更。
- 破坏性变更才升级到 `1.1`，并保证服务端同时接受 `1.0` / `1.1`。

### 4.2 现有事件（基线）

| 方向 | 事件 | 说明 |
|---|---|---|
| C→B | `system.hello` | 握手、角色、能力 |
| B→C | `system.welcome` | 版本、profile 列表、快捷键 |
| B↔C | `system.ping` / `system.pong` | 心跳 |
| C→B | `system.config_set` | 切换 profile |
| C→B | `system.privacy_set` | 切换隐私模式 |
| B→C | `system.privacy_state` | 隐私状态广播 |
| B→C | `system.shortcuts_sync` | 快捷键同步 |
| C→B | `system.hotkey_triggered` | 全局热键动作 |
| B→C | `system.capture_prepare` / `system.capture_done` | 截屏前后隐藏 |
| C→B | `input.text` / `input.action` / `input.cancel` | 输入与取消 |
| C↔B | `audio.input_*` / `audio.output_*` | 语音流 |
| C→B | `context.*` | 窗口、OCR、游戏状态 |
| B↔S | `senses.capture_request` / `context.screen_ref` | 截屏往返 |
| B→C | `output.turn_start` / `text_delta` / `turn_end` | 回合流 |
| B→C | `output.tool_status` / `output.proactive_notice` / `output.card` / `output.artifact_capsule` | 状态与产物 |
| B↔C | `tool.approval_request` / `tool.approval_response` | 审批与澄清 |
| B→C | `avatar.state` | 表情/活动 |
| B→C | `system.error` | 错误 |

### 4.3 新增与变更

#### 4.3.1 `system.hello` 扩展

```json
{
  "client_name": "qimingxing-macos",
  "role": "body",
  "client_type": "body_macos",
  "platform": "macos",
  "app_version": "0.8.0",
  "capabilities": ["mirror_turns", "native_approvals", "native_desktop"]
}
```

- `client_type` 新增：`body_macos`、`body_windows`。
- `platform` 新增：`macos` | `windows`（可选，缺省由 client_type 推断）。
- `app_version` 新增：用于诊断与灰度。
- `ClientCapabilities` 新增字段：

| 字段 | 类型 | 默认 | 含义 |
|---|---|---|---|
| `mirror_turns` | bool | false | 接收其他客户端发起的回合事件（只读展示） |
| `native_approvals` | bool | false | 由本客户端渲染审批/澄清 |
| `native_desktop` | bool | false | 由本客户端执行 reveal/open/copy 等桌面动作 |

> 现有 `audio_output`、`auto_hide`、`pin_image`、`lip_sync` 保持不变。`_get_primary_body_conn` 的"优先 `body_godot` / audio_output"逻辑不变。

#### 4.3.2 `ui.command` / `ui.command_result`

由任意客户端发往 Brain，Brain 校验后转发给目标客户端；用于壳遥控头像、头像请求壳、Brain 请求壳执行原生动作。

```json
{
  "v": "1.0",
  "source": "body",
  "type": "ui.command",
  "payload": {
    "command_id": "cmd_ab12cd34",
    "target": "avatar",
    "action": "open_quick_entry",
    "args": {}
  }
}
```

```json
{
  "v": "1.0",
  "source": "body",
  "type": "ui.command_result",
  "payload": {
    "command_id": "cmd_ab12cd34",
    "ok": true,
    "error": "",
    "data": {}
  }
}
```

**Actions 清单**：

| target | action | args | 说明 |
|---|---|---|---|
| `avatar` | `open_quick_entry` / `close_quick_entry` | — | 若输入框已迁壳，则告警并忽略 |
| `avatar` | `cancel` | — | 急停当前播报/回合 |
| `avatar` | `toggle_dock` / `reset_position` | — | 贴边/复位 |
| `avatar` | `set_scale` | `{"scale": 1.0}` | 0.5–1.3 |
| `avatar` | `set_roam` / `set_earcons` / `set_ambient` | `{"enabled": true}` | 与 `ui.state` 同步 |
| `avatar` | `show_pin_image` | `{"artifact_id": "..."}` | 钉图 |
| `avatar` | `quit` | — | 退出头像进程 |
| `shell` | `open_quick_entry` | `{"prefill": ""}` | 壳打开输入面板 |
| `shell` | `open_settings` | `{"tab": "routing"}` | 壳打开设置 |
| `shell` | `show_notification` | `{"title","body","actions"}` | 原生通知 |
| `shell` | `reveal_path` / `open_path` / `copy_file` / `quick_look` | `{"path": "..."}` | 原生桌面动作 |
| `senses` | `set_privacy_mode` | `{"mode": "..."}` | 可选，沿用现有隐私事件 |
| `all` | `sync_state` | — | 请求重发 `ui.state` |

**路由规则**：

- `target=avatar` → 仅发给 `role=body` 且 `client_type=body_godot` 的连接。
- `target=shell` → 发给声明 `native_desktop` 的壳；若有多个，发给**发起者所在平台**的壳（Brain 记录 `platform`），否则发给第一个。
- `target=senses` → 发给 `role=senses`。
- 目标离线：返回 `ui.command_result{ok:false,error:"target_offline"}`，壳可降级为本地实现（仅限壳自己就能完成的动作）。

#### 4.3.3 `ui.state` / `ui.set_state`

Brain 广播的 UI 状态快照，以及客户端提交的增量补丁。

```json
{
  "v": "1.0",
  "source": "brain",
  "type": "ui.state",
  "payload": {
    "avatar_scale": 1.0,
    "roam": true,
    "earcons": true,
    "ambient": false,
    "docked": false,
    "active_profile": "deepseek_flash",
    "privacy_mode": "meta_only",
    "shortcuts": {
      "ptt_hotkey": "right_option",
      "chat_input_hotkey": "option+space",
      "cancel_hotkey": "option+esc",
      "toggle_dock_hotkey": "option+h",
      "settings_hotkey": "option+k"
    }
  }
}
```

```json
{
  "v": "1.0",
  "source": "body",
  "type": "ui.set_state",
  "payload": {
    "origin": "shell_macos",
    "patch": { "roam": false }
  }
}
```

**规则**：

- Brain 校验字段白名单；未知字段忽略。
- 允许持久化的键：`avatar_scale`、`roam`、`earcons`、`ambient`。
- `docked`、`active_profile`、`privacy_mode` 由各自现有机制维护，只读下发。
- `shortcuts` 仍由 `system.shortcuts_sync` 负责，`ui.state` 中仅作镜像。
- 变更后 Brain 广播全量 `ui.state`（含给 Avatar 与所有壳）。
- 防抖：同一客户端 200ms 内的重复 patch 合并。

#### 4.3.4 `tool.approval_resolved`

审批/澄清被任一 UI 处理后的广播，避免另一侧 UI 悬挂。

```json
{
  "v": "1.0",
  "source": "brain",
  "type": "tool.approval_resolved",
  "payload": {
    "approval_id": "apr_ab12cd34",
    "turn_id": "turn_ab12cd34",
    "approved": true,
    "answered_by": "shell_macos",
    "answer_text": ""
  }
}
```

- 触发时机：Brain 收到有效 `tool.approval_response` 后，向所有 body 与发起连接广播。
- 客户端收到后必须自行关闭对应审批卡；重复响应不再处理。
- 超时（`timeout_sec`）同样广播 `approved=false, answered_by="timeout"`。

#### 4.3.5 `output.turn_start` 扩展

新增可选字段：`origin_client_id`、`origin_client_type`。用于诊断与镜像去重。

#### 4.3.6 二进制音频（P1 优化，非 Phase 0 必需）

- 输入侧已支持二进制帧（`handle_raw_bytes`）。
- 输出侧当前为 `data_b64` JSON。规格建议后续切换为二进制帧：
  - `audio.output_start` 仍用 JSON 声明 `sample_rate/channels/format`；
  - 之后的二进制帧默认归属"该连接最近一次 output_start 的流"；
  - `audio.output_end` 用 JSON 关闭。
- 兼容策略：保留 `data_b64` 字段，客户端声明 `audio_binary` capability 时 Brain 才发二进制。
- 收益：去掉 base64 33% 膨胀与编解码开销；不改变时序语义。

### 4.4 关键时序

#### 4.4.1 壳发起文本回合（含头像镜像）

```
Shell → Brain: input.text {text, attachments}
Brain → Shell: output.turn_start {origin=shell}
Brain → Avatar: output.turn_start {mirror}
Brain → Shell: output.text_delta ...
Brain → Avatar: output.text_delta ...      # 气泡同步
Brain → Avatar: avatar.state {thinking}
Brain → Avatar: audio.output_*             # TTS 仍归 Avatar
Brain → Shell: output.turn_end
Brain → Avatar: output.turn_end
```

- Avatar 声明 `mirror_turns`，因此收到只读流。
- 若 Avatar 未声明 `mirror_turns`，则只有壳收到流，头像不动。

#### 4.4.2 壳发起语音回合

```
Senses → Brain: audio.input_start/chunk/end
Brain → Shell: output.turn_start
Brain → Avatar: mirror + TTS
```

- 语音输入仍由 Senses 采集；壳只负责触发（若壳提供录音按钮，则发 `ui.command` 给 Senses 或直接复用 PTT）。
- 建议：录音入口保持"全局热键 + 壳按钮"两路，最终都由 Senses 执行采集。

#### 4.4.3 审批/澄清

```
Initiator (Shell) → Brain: tool.approval_response
Brain → all bodies: tool.approval_resolved
Brain → Initiator: 继续回合
```

- `native_approvals` 的壳渲染原生 sheet/alert；Avatar 只切到 `warning` 表情。
- `body_web` 未声明 `native_approvals` 时，沿用现有胶囊 UI。

#### 4.4.4 托盘命令

```
Shell → Brain: ui.command {target:"avatar", action:"toggle_dock"}
Brain → Avatar: ui.command
Avatar → Brain: ui.command_result
Brain → Shell: ui.command_result
```

#### 4.4.5 截屏自动隐藏

```
Brain → Avatar: system.capture_prepare
Brain → Senses: senses.capture_request
Senses → Brain: context.screen_ref
Brain → Avatar: system.capture_done
```

- 保持现有流程；Avatar 的隐藏实现改为"原生排除"或移出屏幕，不再使用 `visible=false`（见 9.3）。

### 4.5 HTTP API 清单

| 方法 | 路径 | 用途 | 调用方 |
|---|---|---|---|
| GET | `/health` | 健康检查 | 壳、运维 |
| GET | `/` `/app.js` `/styles.css` | Web 调试台 | 浏览器 |
| WS | `/ws` | 主协议 | 壳、Avatar、Senses、Web |
| POST | `/api/artifacts` | 附件上传 | 壳、Web |
| GET/POST | `/api/config/settings` | 设置读写 | 壳 |
| POST | `/api/config/test_provider` | 连通性探测 | 壳 |
| GET/POST/DELETE | `/api/mcp/servers` | MCP CRUD | 壳 |
| POST | `/api/mcp/servers/{name}/authorize` | OAuth 授权 | 壳 |
| POST | `/api/mcp/reload` | 热重载 | 壳 |
| GET | `/api/mcp/oauth/callback` | OAuth 回调 | 浏览器 |
| POST | `/api/context/attach_paths` | 路径挂载 | 壳、Avatar |
| POST | `/api/desktop/open_action` | 桌面动作（过渡） | Avatar、Web |
| GET | `/api/memory/debug` | 记忆诊断 | 运维 |

**变更**：`/api/desktop/open_action` 保留兼容，但壳优先走 `ui.command` 的 `native_desktop` 路径；Windows 上线前需补齐该端点的平台分支（见 10.5）。

### 4.6 错误与降级

| 场景 | 行为 |
|---|---|
| WS 断开 | 客户端指数退避重连（1s → 5s 上限），期间壳显示"离线"，禁用提交 |
| Brain 未就绪 | 壳显示启动失败 + "打开日志"按钮 |
| 目标客户端离线 | `ui.command_result{ok:false}`；壳可本地降级（通知、reveal） |
| 未知事件/字段 | 忽略，不报错 |
| 审批超时 | 广播 `approval_resolved{approved:false, answered_by:"timeout"}` |
| 头像崩溃 | 壳重启 Avatar；连续 3 次失败后停止并通知 |

---

## 5. 状态模型

### 5.1 所有权

| 状态 | 所有者 | 持久化 | 同步方式 |
|---|---|---|---|
| profiles / providers / keys | Brain | `config.yaml` (0600) | `system.welcome`、`ui.state` |
| privacy 模式 | Brain | `config.yaml` 默认值 + 内存 | `system.privacy_state` |
| shortcuts | Brain | `config.yaml` | `system.shortcuts_sync` |
| UI 开关（roam/earcons/ambient/scale） | Brain | `config.yaml` `ui:` | `ui.state` / `ui.set_state` |
| 会话、消息、记忆 | Brain | SQLite | 内部 |
| 当前回合、取消事件 | Brain | 内存 | turn 事件 |
| 窗口位置/尺寸 | 各壳/头像 | 平台本地（NSWindow autosave / 本地设置） | 不同步 |
| 托盘图标状态 | 各壳 | 本地 | 依据 WS 连接 + `ui.state` |
| 日志 | 各进程 | 本地文件 | 不汇总（Phase 3 可选） |

### 5.2 配置持久化

建议在 `config.yaml` 新增：

```yaml
ui:
  avatar_scale: 1.0
  roam: true
  earcons: true
  ambient: false
```

兼容处理：

- 优先读 `ui.*`；
- 若缺省，回退到现有 `godot.earcons_enabled` 等字段；
- 首次写入时迁移并保留旧字段（一个版本周期后删除）。

### 5.3 禁止事项

- 壳不得直接写 `config.yaml`，避免与 Brain 的原子写冲突。
- 壳不得缓存 API Key 明文；只保存 Brain 返回的 masked 值。
- Avatar 不得自行持久化共享开关；本地只保留窗口几何。

---

## 6. 回合、审批与语音

### 6.1 回合归属

- 每个回合有唯一 `origin_client_id`（发起者）。
- 发起者始终收到完整流。
- 声明 `mirror_turns` 的 Avatar 收到只读镜像流，用于气泡与表情。
- 未声明 `mirror_turns` 的 Web/其他壳不接收他人回合，避免重复 UI。

### 6.2 TTS 路由

- 保持现状：`_get_primary_body_conn` 优先 `body_godot` 或 `audio_output`。
- 壳**不得**声明 `audio_output`，否则会抢走 TTS 并导致双声。
- 若未来壳要接管 TTS，必须新增互斥协商（例如 `audio_owner`），本期不做。

### 6.3 审批与澄清

- `tool.approval_request` 发给发起者 + 声明 `native_approvals` 的壳。
- 发起者为壳时，壳渲染原生 UI；Avatar 只显示 `warning` 表情。
- 发起者为 Web 时，沿用现有胶囊 UI。
- 任一 UI 响应后广播 `tool.approval_resolved`。
- `ask_user` 澄清复用同一机制，`details.kind == "clarification"`。

### 6.4 取消与急停

- `input.cancel` 仍全局生效：Brain 取消当前回合、TTS，并广播 `output.turn_end{finish_reason:"cancelled"}`。
- 托盘"急停"、壳按钮、Avatar 胶囊、全局热键都走同一路径。
- 壳收到 `cancelled` 后立即停止本地动画/播放。

### 6.5 截屏

- 仅 `full_on_demand` 模式允许截屏；`off` / `meta_only` 给出原生提示。
- 截屏请求仍由 Brain → Senses 往返，壳不参与。
- 壳可在设置中切换隐私模式（`system.privacy_set`）。

---

## 7. macOS 壳规格（shell_macos）

### 7.1 技术栈

| 项 | 选择 |
|---|---|
| 语言 | Swift 6 |
| UI | SwiftUI + AppKit（关键窗口用 AppKit 控制） |
| 最低系统 | macOS 13.0 |
| 构建 | Xcode 工程 + `xcodebuild` |
| 应用形态 | `LSUIElement = true`（无 Dock 图标），菜单栏常驻 |
| 签名 | 开发期 ad-hoc；发布期 Developer ID + notarization |

### 7.2 生命周期

- `NSApplication` activation policy `.accessory`。
- 单实例：`NSRunningApplication` / 文件锁；二次启动唤起快捷输入。
- 登录启动：`SMAppService.mainApp.register()`（可配置）。
- 退出：`applicationShouldTerminate` 中优雅停止 Avatar → Senses → Brain，超时强杀。
- 崩溃恢复：监听子进程退出，指数退避重启；连续失败弹原生告警并保留日志。

### 7.3 菜单栏

- `NSStatusItem`，图标为 template image（现有 `tray_icon_template.png`）。
- 左键：`NSPopover` + SwiftUI 卡片（对应现有托盘面板）。
- 右键：原生 `NSMenu`（设置、重置位置、打开 Web、退出）。
- 菜单项使用 SF Symbols；勾选项使用 `NSMenuItem.state`。
- 状态：已连接/离线/启动中使用不同图标或角标。

### 7.4 设置窗口

- 独立 `NSWindow` + `NSHostingController`，`NSVisualEffectView` 侧边栏。
- 五个分区：外观、模型与路由、语音与隐私、快捷键、MCP。
- 原生控件：`Form`、`SecureField`、`Picker`、`Toggle`、`Table`。
- 快捷键：`⌘,` 打开；`⌘W` 关闭；窗口 frame 自动恢复。
- 数据源：`GET /api/config/settings`；保存 `POST`；探测 `POST /api/config/test_provider`。
- MCP：CRUD + OAuth 授权，轮询状态；沿用现有 HTTP 语义。
- 密钥：只显示 masked；留空表示保留原值；永不写入本地存储。

### 7.5 快捷输入

- `NSPanel`：
  - `styleMask = [.nonactivatingPanel, .borderless]`
  - `level = .floating`
  - `collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]`
  - `hidesOnDeactivate = false`
  - `becomesKeyOnlyIfNeeded = true`
  - `canBecomeKey` 重写为 true
- 位置：屏幕上方居中，宽度 620–720pt。
- 内容：多行输入、附件（拖拽/粘贴/选择）、profile 选择、看屏开关、发送/停止。
- 提交：`input.text` / `input.action{ask_with_screen}`；附件先 `POST /api/artifacts`。
- 中文输入法、撤销、拼写检查使用系统 `NSTextView`（SwiftUI `TextEditor` 底层）。
- Esc 关闭；`⌘Enter` 发送；空输入不提交。

### 7.6 通知

- `UNUserNotificationCenter`，请求授权一次。
- 主动提醒（`output.proactive_notice`）→ 原生通知，带"打开对话"动作。
- 审批/澄清默认走壳内 sheet；仅在头像隐藏时可降级为通知。

### 7.7 原生桌面动作

| 动作 | 实现 |
|---|---|
| reveal | `NSWorkspace.shared.activateFileViewerSelecting([url])` |
| open | `NSWorkspace.shared.open(url)` |
| copy_file | `NSPasteboard.general` 写入 file URL |
| quick_look | `QLPreviewPanel` |
| 钉图 | 无边框 `NSPanel` + `NSImageView`，支持缩放/关闭 |

### 7.8 权限（TCC）

- 壳作为 responsible process，权限归属"启明星.app"，不再归属终端。
- `Info.plist` 保留 `NSMicrophoneUsageDescription`、`NSScreenCaptureUsageDescription`、`NSAppleEventsUsageDescription`。
- 屏幕录制：Senses 首次失败时调用 `CGRequestScreenCaptureAccess`，提示用户重启应用。
- 辅助功能/输入监控：全局热键仍由 Senses 的 Quartz listener 负责，壳只配置。
- 壳自身不需要辅助功能权限，除非未来接管全局热键。

### 7.9 原生体验验收点

- 菜单栏明暗自动适配；菜单键盘导航可用；VoiceOver 可读。
- 设置窗口支持全键盘操作、焦点环、深浅色、窗口恢复。
- 快捷输入不抢焦点、不打断全屏应用、中文输入法正常。
- 通知点击可回到对话。
- 所有窗口在 Stage Manager / 多桌面下行为正常。

---

## 8. Windows 壳规格（shell_windows）

### 8.1 技术栈

| 项 | 选择 |
|---|---|
| 运行时 | .NET 8/9（self-contained 发布） |
| UI | WinUI 3（Windows App SDK 1.6+） |
| 最低系统 | Windows 10 1809+；Windows 11 为推荐目标 |
| 应用形态 | 经典桌面应用（非 MSIX），托盘常驻 |
| 安装 | Inno Setup 安装器；后续可评估 MSIX |
| 签名 | 代码签名证书（发布期）；开发期可未签名 |

> 备选：若必须覆盖 Win10 早期版本且不愿接受 Mica 降级，可改用 WPF；本规格默认 WinUI 3。

### 8.2 生命周期

- 单实例：命名 Mutex；二次启动唤起快捷输入。
- 登录启动：`HKCU\Software\Microsoft\Windows\CurrentVersion\Run`（可配置）。
- 子进程：`Process` 启动 Brain/Senses/Avatar；Job Object 确保壳退出时清理。
- 崩溃恢复：同 macOS，指数退避 + 通知。
- 日志：`%LOCALAPPDATA%\Qimingxing\logs\`。

### 8.3 托盘

- `Shell_NotifyIcon`（P/Invoke）或 `H.NotifyIcon.WinUI`。
- 左键：XAML `Flyout`/自定义无边框面板（对应现有 popover）。
- 右键：原生 `HMENU` + `TrackPopupMenu`（最原生）或 XAML 菜单。
- 图标适配深/浅任务栏；支持高 DPI。
- 状态：已连接/离线/启动中。

### 8.4 设置窗口

- WinUI 3 `NavigationView`，与 Windows 设置同构。
- 窗口背景：Win11 Mica；Win10 降级 Acrylic/纯色。
- 自定义标题栏：`ExtendsContentIntoTitleBar = true`。
- 五个分区与 macOS 对齐（内容、字段、校验一致）。
- 原生控件：`TextBox`、`PasswordBox`、`ComboBox`、`ToggleSwitch`、`ListView`。
- 快捷键显示：`Ctrl`/`Alt` 文案，不显示 ⌘。
- 数据源与 macOS 完全一致（同一 HTTP 契约）。

### 8.5 快捷输入

- 无边框 `Window`：
  - `WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW`
  - `SetWindowPos(HWND_TOPMOST)`
  - `SystemBackdrop = DesktopAcrylicBackdrop`（Win11 可 Mica）
  - 不进入任务栏
- 位置：光标所在显示器顶部居中；多显示器按 `MonitorFromWindow` 计算。
- 中文 IME 使用系统输入法（WinUI 原生支持）。
- 提交路径与 macOS 相同。

### 8.6 通知

- `AppNotificationManager`（Windows App SDK），非打包应用需注册。
- 主动提醒带"打开对话"按钮；审批在头像隐藏时可降级为通知。
- 通知中心可回看。

### 8.7 原生桌面动作

| 动作 | 实现 |
|---|---|
| reveal | `explorer.exe /select,"<path>"` 或 `SHOpenFolderAndSelectItems` |
| open | `Process.Start(new ProcessStartInfo(path){UseShellExecute=true})` |
| copy_file | `CF_HDROP`（Win32）或 PowerShell `Set-Clipboard -Path` |
| quick_look | Windows 无直接等价；降级为"打开"或资源管理器预览窗格 |
| 钉图 | 无边框 `Window` + `Image`，支持缩放/关闭 |

### 8.8 权限与系统限制

- 普通桌面会话无需额外授权即可读取前台窗口标题、mss 截屏。
- UAC 提权窗口无法被非提权进程读取/截屏：检测到安全桌面时返回明确提示。
- 首次监听 `127.0.0.1` 可能触发防火墙提示；安装器说明并预置规则（可选）。
- 全局热键由 Senses 的低级键盘钩子处理，壳不需要管理员权限。

### 8.9 原生体验验收点

- Mica/Acrylic 在 Win11 正常，Win10 降级不崩。
- 托盘左/右键行为符合 Windows 习惯。
- 快捷输入不抢焦点、不出现在 Alt+Tab、全屏游戏上可呼出。
- 高 DPI（125%/150%/200%）与多显示器下位置正确。
- 中文 IME、粘贴、撤销正常。
- 通知点击可回到对话。

---

## 9. Avatar（Godot）规格

### 9.1 保留与移除

**保留**：

- `Main.tscn` / `main.gd`（精简）
- `WSClient`（ws_client.gd）
- `AudioPlayer`（audio_player.gd）
- `AvatarController`（rig/漫游/贴边/拖拽）
- `UIOverlay`（气泡、产物胶囊、审批/澄清胶囊、临时状态）

**移除**：

- `TrayManager` / `tray_manager.gd`
- `SettingsWindow` / `settings_window.gd` / `.tscn`
- `QuickEntryWindow` / `quick_entry_window.gd` / `.tscn`
- `PinImageWindow`（迁至原生壳；若壳未就绪可暂留一个版本）

### 9.2 交互变更

- 头像单击：发 `ui.command{target:"shell", action:"open_quick_entry"}`，不再本地打开输入框。
- 头像右键：发 `ui.command{target:"shell", action:"open_settings"}` 或打开壳菜单。
- 全局热键 `chat_input`：由 Senses → Brain 广播；壳响应打开输入面板，Avatar 不再响应。
- 审批/澄清：Avatar 只显示表情与气泡提示；交互在壳内完成。
- 拖拽文件到头像：保留现有行为（`POST /api/context/attach_paths`），或转发 `ui.command` 给壳；本期保留。

### 9.3 截屏隐藏修复

当前 `set_capture_hidden()` 直接设置主窗口 `visible=false`，Godot 4 会报错且无效。

**规格要求**（二选一，优先 A）：

- **A. 原生排除**：
  - Windows：`SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE)`；
  - macOS：`NSWindow.sharingType = .none`（需实测系统版本行为）；
  - 由壳在启动 Avatar 后取得窗口句柄调用，或通过 GDExtension 暴露。
- **B. 移出屏幕**：截屏前将窗口移到所有显示器之外，截屏后恢复；需处理多显示器/全屏。

无论哪种，都必须有 failsafe 定时恢复，避免头像永久消失。

### 9.4 窗口行为

- 透明、无边框、置顶、点击穿透多边形：保持现状。
- 多显示器/DPI：Godot 导出时启用 per-monitor DPI aware；与壳的坐标体系统一。
- 层级：Avatar 置顶；壳的 `NSPanel`/WinUI 面板层级需高于 Avatar，避免被遮挡（见 12.4 验收）。
- 关闭 Avatar：壳退出时先发 `ui.command{action:"quit"}`，再强杀兜底。

### 9.5 导出

- macOS：现有 `.app` 内嵌 Godot 运行时；后续可切换为导出模板产物。
- Windows：使用 Godot 导出模板生成 `QimingxingAvatar.exe` + `.pck`。
- 版本号与壳、Brain 保持一致。
- 资源：保留 `assets/`；移除仅设置/托盘使用的图标（可延后）。

---

## 10. Senses 规格

### 10.1 平台抽象

新增：

```python
# senses/audio/factory.py
def create_hotkey_listener(platform: str, ...) -> HotkeyListener: ...
```

- `darwin` → `MacOSPTTListener`
- `win32` → `WindowsHotkeyListener`
- 其他 → `NullHotkeyListener`（记录告警，不阻塞启动）

`SensesAgent._get_hotkey_listener()` 改为调用工厂，禁止无条件实例化 macOS 实现。

### 10.2 Windows 全局热键

| 能力 | 实现 |
|---|---|
| 动作热键（chat/cancel/settings/dock） | `RegisterHotKey` + 消息窗口线程 |
| PTT 按住说话 | `WH_KEYBOARD_LL` 低级键盘钩子，捕获 keydown/keyup |
| 修饰键单独按住 | 钩子跟踪 VK 状态（`GetAsyncKeyState` 辅助） |
| 冲突处理 | 注册失败时记录并上报 `permission_hint`，不崩溃 |

- 不引入 `pynput`/`keyboard` 依赖；用 `ctypes` + Win32。
- 钩子线程必须消息循环；退出时 `UnhookWindowsHookEx`。
- 与 macOS 行为对齐：按下开始采集，松开结束。

### 10.3 快捷键配置

- 统一规范字符串：`right_ctrl`、`ctrl+space`、`alt+esc`、`alt+h`、`alt+k`。
- 平台默认值：

| 动作 | macOS 默认 | Windows 默认（建议） |
|---|---|---|
| PTT | `right_option` | `right_ctrl` |
| 快捷输入 | `option+space` | `ctrl+alt+space` |
| 急停 | `option+esc` | `ctrl+alt+x` |
| 贴边 | `option+h` | `ctrl+alt+h` |
| 设置 | `option+k` | `ctrl+alt+,` |

> Windows 需避开系统保留组合：`Alt+Space`（窗口菜单）、`Ctrl+Space`（中文 IME 切换）、`Win+*`。

- 配置仍存于 Brain；Senses 按平台解析；设置 UI 只展示当前平台可用的组合。

### 10.4 窗口提供者

- `WindowsWindowProvider.get_active_window()` 传入 `app_name` / `process_name` 给 `detect_self_window`，与 macOS 对齐。
- UWP 应用（`ApplicationFrameHost.exe`）需回退到窗口标题/子进程识别。
- 隐私黑名单补充 Windows 常见敏感应用（Windows Security、Credential Manager、BitLocker、银行客户端等）。

### 10.5 桌面动作

- `brain/tools/desktop_io.py` 的 `execute_desktop_action` 增加平台分支：
  - Windows：`explorer /select,`、`os.startfile`、剪贴板 `CF_HDROP`；
  - 无法实现 `quick_look` 时返回明确错误码，由壳降级为"打开"。
- 长期方向：优先由壳执行（`native_desktop`），Brain 端点仅作兼容与 Web 调试。

### 10.6 截屏

- 保持 mss + Pillow；补：
  - 显示器选择（主屏/光标所在屏）；
  - 高 DPI 坐标处理；
  - 安全桌面/锁屏时返回可读错误。
- 不做后台轮询；仍按需截屏。

### 10.7 开发脚本

- 新增 `qimingxing.ps1`（start/status/logs/restart/stop），行为与 `qimingxing.sh` 对齐。
- 打包脚本各自独立：`scripts/build_macos_app.sh`、`scripts/build_windows_installer.ps1`。

---

## 11. 打包与分发

### 11.1 macOS

```
Qimingxing.app/
  Contents/
    MacOS/qimingxing            # Swift 壳（CFBundleExecutable）
    Resources/
      app/                      # brain/ senses/ protocol/ body_web/ config.example.yaml
      python/                   # CPython 3.12 + site-packages
      Godot.app/                # 或导出的 Avatar 产物
      AppIcon.icns
      zh-Hans.lproj/
    Info.plist                  # LSUIElement=true
```

- Swift 壳替代现有 C launcher，负责启动/守护三个子进程。
- 保留 `NSMicrophoneUsageDescription`、`NSScreenCaptureUsageDescription`、`NSAppleEventsUsageDescription`。
- 开发期 `codesign --deep --sign -`；发布期 Developer ID + hardened runtime + notarization。
- 配置与数据：`~/Library/Application Support/Qimingxing/`；日志：`~/Library/Logs/Qimingxing/`。

### 11.2 Windows

```
Qimingxing/
  Qimingxing.Shell.exe          # WinUI 3 壳
  brain/ senses/ protocol/ body_web/
  python/                       # embeddable CPython + site-packages(win)
  avatar/QimingxingAvatar.exe + .pck
  config.example.yaml
  logs/
```

- 安装器：Inno Setup，写入 `%LOCALAPPDATA%\Programs\Qimingxing` 或用户选择目录。
- 配置与数据：`%APPDATA%\Qimingxing\` 或 `%LOCALAPPDATA%\Qimingxing\`（需统一，建议前者）。
- 日志：`%LOCALAPPDATA%\Qimingxing\logs\`。
- 代码签名解决 SmartScreen；未签名时安装器给出说明。
- 自动更新本期不做，预留版本检查接口。

### 11.3 Python 运行时

- macOS：沿用 `.venv` 的 Python 与 site-packages；排除 `__pycache__`、测试、`*.dist-info`。
- Windows：Python embeddable + `pip install --target` 安装 win 轮子；注意 `python312._pth`、`certifi`、`sounddevice` 的 PortAudio DLL。
- `pyobjc` 仅 macOS（`pyproject.toml` 已有平台标记）；Windows 不安装。
- 两端都不打包 `config.yaml`、`data/`、日志。

### 11.4 Godot

- macOS：可继续内嵌 `Godot.app`，或切换导出模板产物。
- Windows：必须使用导出模板生成独立 exe/pck，不能依赖编辑器二进制。
- 导出前移除托盘/设置/快捷输入场景与脚本。
- 版本号统一注入。

### 11.5 构建矩阵

| 产物 | Runner | 命令 |
|---|---|---|
| macOS .app / dmg | macOS 14 | `scripts/build_macos_app.sh --dmg` |
| Windows installer | Windows Server 2022 | `scripts/build_windows_installer.ps1` |
| Python 测试 | 两端 | `pytest -q` |
| 协议 schema | 任一端 | `python -m protocol.export_schema` |

---

## 12. 非功能需求

### 12.1 性能目标

| 指标 | 目标 |
|---|---|
| 壳启动到托盘可见 | < 1s |
| Brain `/health` 就绪（冷启动） | < 5s |
| Avatar 窗口出现 | < 2s |
| 本地回合开销（不含网络）p95 | < 50ms |
| 空闲 CPU（整体） | < 3% |
| 常驻内存 | Brain < 300MB；Avatar < 300MB；壳 < 150MB |
| 重连 | 1s 起，指数退避，上限 5s |

### 12.2 稳定性

- 任一子进程崩溃：壳自动重启，指数退避；连续 3 次失败停止并通知。
- 单实例：二次启动不产生第二套进程，唤起快捷输入。
- 退出：优雅停止 + 超时强杀；确保不残留 Python/Godot。
- WS 断线期间：壳禁用提交，保留输入草稿。

### 12.3 安全

- Brain 仅绑定 `127.0.0.1`；HTTP 端点校验 loopback（沿用现有实现）。
- 密钥只在 Brain 内存/配置文件中；WS 只下发 masked 值。
- 日志脱敏沿用 `redact_secrets`。
- 配置文件权限 `0600`（macOS） / 用户目录 ACL（Windows）。
- 不引入遥测；诊断数据仅本地。

### 12.4 可观测性

- 每个进程独立日志，带时间戳与进程名。
- `/health` 返回版本、protocol、profile、privacy、memory backend、embedding 状态。
- 壳提供"打开日志目录"入口。
- Phase 3 可选：在设置中展示最近回合耗时、token 用量、费用。

---

## 13. 测试与验收

### 13.1 Python 测试

- 现有 100 个用例必须全绿（macOS + Windows CI）。
- 新增：
  - `client_type` / capabilities 解析与路由；
  - `ui.command` 路由与离线降级；
  - `ui.state` 白名单、防抖、持久化；
  - `tool.approval_resolved` 广播与重复响应；
  - 回合镜像去重；
  - Windows 热键解析（不触发真实钩子）；
  - `desktop_io` Windows 分支（用 fake runner）；
  - Windows 窗口提供者自窗口识别。

### 13.2 协议契约

- 用 `protocol.schema.json` 生成 Swift/C# DTO，生成物纳入 CI 校验（schema 变更必须重新生成）。
- 黄金信封测试：固定 JSON → 解析 → 再序列化，字段稳定。
- 未知字段/未知事件：客户端不报错。
- 旧客户端（仅 `body_web`）与新 Brain 互通。

### 13.3 平台验收清单

**macOS**

- [ ] 菜单栏左键面板、右键原生菜单
- [ ] 设置五页读写、密钥脱敏、连通性探测、MCP OAuth
- [ ] 快捷输入不抢焦点、全屏应用可呼出、中文输入法
- [ ] 审批/澄清原生 UI，另一端同步关闭
- [ ] 托盘命令驱动头像（贴边/复位/缩放/急停）
- [ ] 截屏时头像不出现（原生排除）
- [ ] 登录启动、单实例、崩溃重启
- [ ] 深浅色、VoiceOver、Stage Manager

**Windows**

- [ ] 托盘左键面板、右键原生菜单
- [ ] 设置五页、密钥脱敏、MCP OAuth
- [ ] 快捷输入不抢焦点、不出现在 Alt+Tab、中文 IME
- [ ] 审批/澄清、通知点击回对话
- [ ] 托盘命令驱动头像
- [ ] 截屏排除/隐藏
- [ ] 高 DPI 125/150/200%、多显示器
- [ ] 登录启动、单实例、崩溃重启、Job Object 清理
- [ ] Win10 22H2 / Win11 23H2+ 冒烟

### 13.4 回归清单

- [ ] Web 调试台仍可独立完成文本/语音/附件/审批
- [ ] 全局热键在两端均可用（PTT 按住/松开）
- [ ] 隐私三档与黑名单行为一致
- [ ] 记忆提炼、会话压缩、调度提醒不受影响
- [ ] MCP stdio/SSE/streamable_http 与 OAuth 不受影响

---

## 14. 实施阶段

| 阶段 | 交付物 | 退出标准 | 粗估 |
|---|---|---|---|
| **P0 契约与地基** | 协议扩展、`ui.state` SSOT、回合镜像、审批 resolved、Godot 瘦身、Windows Senses 补齐、版本统一 | Python 测试全绿；旧 `body_web` 兼容；Windows 能启动 Senses 并触发热键 | 1.5–2.5 周 |
| **P1 macOS 壳** | Swift 菜单栏 + 设置 + 快捷输入 + 守护 + 打包 | macOS 验收清单通过；可日常使用 | 2–3 周 |
| **P2 Windows 壳** | WinUI 3 同能力 + 安装器 | Windows 验收清单通过；可日常使用 | 3–4 周 |
| **P3 打磨** | 通知、钉图、登录启动、自动更新、签名/公证、诊断面板 | 双平台发布候选 | 1.5–2 周（可并行） |

**依赖**：P1/P2 均依赖 P0；P2 可与 P1 并行，但协议变更必须先冻结。

---

## 15. 风险与缓解

| 风险 | 影响 | 缓解 |
|---|---|---|
| 两套壳行为漂移 | 用户困惑、维护成本翻倍 | 共享协议 DTO、设计 token、验收清单、截图基线 |
| 原生感与品牌感冲突 | 设计反复 | 明确"平台控件原生、品牌只做强调色/图标/文案" |
| Windows 热键/IME 冲突 | PTT 不可用 | 默认避开系统组合；设置 UI 做冲突检测 |
| TCC/UAC 权限差异 | 截屏/麦克风失败 | 壳作为 responsible process；错误提示直达系统设置 |
| Python 打包体积/启动 | 安装包大、首启慢 | 懒加载、预编译、登录启动预热；不为此重写 Rust |
| Godot 透明窗口在 Windows 异常 | 头像显示错误 | P2 前做最小验证 Demo；保留移出屏幕方案 |
| 签名/公证/代码签名成本 | 分发受阻 | 提前准备证书与预算；开发期 ad-hoc |
| 协议演进破坏旧客户端 | Web 调试台失效 | additive-only；契约测试覆盖旧客户端 |

---

## 16. 待 Review 决策

| # | 问题 | 建议 | 影响 |
|---|---|---|---|
| Q1 | Windows 最低版本 | Win11 为主，Win10 1809+ 降级 | 材质/控件选择 |
| Q2 | Windows 框架 | WinUI 3（备选 WPF） | 全部 Windows UI |
| Q3 | 钉图是否迁原生 | 是 | Godot 进一步瘦身 |
| Q4 | `ui:` 配置段 vs 扩展 `godot:` | 新增 `ui:`，兼容回退 | 迁移成本 |
| Q5 | Windows 快捷键默认值 | 见 10.3 建议 | 与 IME/系统冲突 |
| Q6 | 壳是否执行桌面动作 | 是（`native_desktop`） | 去掉 Python 平台分支 |
| Q7 | 二次启动行为 | 唤起快捷输入 | UX |
| Q8 | 本地化范围 | 先 zh-Hans，预留 en | 文案结构 |
| Q9 | 是否保留 Godot 托盘作为兜底 | 不保留，减少分叉 | 维护成本 |
| Q10 | 自动更新方案 | 本期不做，P3 评估 | 发布流程 |

---

## 17. 附录

### A. 新增字段速查

| 位置 | 字段 | 类型 | 默认 |
|---|---|---|---|
| `SystemHelloPayload` | `platform` | `"macos" \| "windows"` | null |
| `SystemHelloPayload` | `app_version` | str | "" |
| `ClientCapabilities` | `mirror_turns` | bool | false |
| `ClientCapabilities` | `native_approvals` | bool | false |
| `ClientCapabilities` | `native_desktop` | bool | false |
| `OutputTurnStartPayload` | `origin_client_id` | str | "" |
| `OutputTurnStartPayload` | `origin_client_type` | str | "" |
| `client_type` | 新值 | `body_macos` \| `body_windows` | — |

### B. 主要文件改动

| 文件 | 改动 |
|---|---|
| `protocol/models.py` | 新 payload、registry、capabilities、client_type |
| `protocol.schema.json` | 重新导出 |
| `brain/hub.py` | `ui.command` 路由、`ui.state` 广播、回合镜像、`approval_resolved` |
| `brain/main.py` | 可选：`/api/ui/state` 调试端点 |
| `brain/config.py` | `ui:` 段、平台快捷键默认值 |
| `brain/tools/desktop_io.py` | Windows 分支 / 委托壳 |
| `senses/main.py` | 热键工厂 |
| `senses/audio/__init__.py` | 移除无条件 macOS 导入 |
| `senses/audio/windows_hotkeys.py` | 新增 |
| `senses/window_providers/windows.py` | 自窗口识别、UWP 回退 |
| `senses/screen_capture.py` | 显示器/DPI/错误 |
| `body_godot/scripts/main.gd` | 移除托盘/设置/快捷输入接线 |
| `body_godot/scripts/tray_manager.gd` | 删除 |
| `body_godot/scripts/settings_window.gd` | 删除 |
| `body_godot/scripts/quick_entry_window.gd` | 删除 |
| `body_godot/scripts/pin_image_window.gd` | 删除（或保留一版） |
| `body_godot/scripts/avatar_controller.gd` | 修复截屏隐藏 |
| `scripts/build_macos_app.sh` | Swift 壳接入 |
| `scripts/build_windows_installer.ps1` | 新增 |
| `docs/` | 本规格书、验收清单 |

### C. 平台能力映射

| 能力 | macOS | Windows |
|---|---|---|
| 托盘 | `NSStatusItem` | `Shell_NotifyIcon` |
| 富面板 | `NSPopover` + SwiftUI | XAML `Flyout` / Window |
| 材质 | `NSVisualEffectView` | Mica / Acrylic |
| 设置 | `NSWindow` + SwiftUI | `NavigationView` |
| 快捷输入 | `NSPanel` | 无边框 `Window` |
| 通知 | `UNUserNotificationCenter` | `AppNotificationManager` |
| reveal | `NSWorkspace` | `explorer /select` |
| 文件复制 | `NSPasteboard` | `CF_HDROP` |
| 登录启动 | `SMAppService` | `HKCU\...\Run` |
| 单实例 | `flock` / `NSRunningApplication` | 命名 Mutex |
| 子进程清理 | `Process` + 信号 | `Process` + Job Object |

### D. 术语

| 术语 | 含义 |
|---|---|
| 壳 / Shell | 平台原生 UI 进程（Swift 或 C#） |
| Avatar | Godot 桌面头像进程 |
| Brain | Python 后端服务 |
| Senses | Python 桌面感知/音频客户端 |
| 镜像回合 | 非发起方只读接收回合事件，用于头像同步 |
| native_desktop | 壳声明可执行原生桌面动作的能力位 |

---

*本规格书为 Review Draft。确认第 16 节的开放决策后，进入 P0 实施。*
