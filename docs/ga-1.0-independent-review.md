# GA 1.0（v1.0.0）独立验收报告

> **历史文档（2026-11-27 归档）**：v1.1 已移除 `body_web`，原生壳为唯一 UI；文中的版本号与测试数反映当时口径。

| 项目 | 内容 |
|---|---|
| 验收对象 | 分支 `feat/ga-1.0`，提交 `f79d657` |
| 验收日期 | 2026-11-25 |
| 验收方式 | 代码审查 + 全量测试 + 真机进程/日志 + 断线重连复现 |
| 总体结论 | **条件性通过（Rough MVP）**：可作为 v1.0.0 候选继续迭代，但**不建议按现有验收报告直接签 GA** |

---

## 1. 基线结果

| 项目 | 结果 | 说明 |
|---|---|---|
| `pytest -q` | **111 passed** | 但其中 1 个用例耗时 **60.02s**（见 P2-1） |
| `brain.memory.cli eval` | **18/18 通过** | macOS 应用类用例使用 mock runner，不是真实 App |
| `node --check body_web/app.js` | 通过 | Web 调试台任务面板已接入 |
| `swift build -c release --arch arm64` | 通过 | 增量构建 0.26s |
| `dist/Qimingxing.app` | 存在，约 **178M** | 报告写 166M，口径略差 |
| 默认启动 | ✅ shell + brain + senses，**无 Godot** | `pgrep` 已确认 |
| 版本口径 | ✅ `version.txt = 1.0.0`，`/health` 与 Info.plist 一致 | 已统一 |
| WebSocket Origin | ✅ 非法 Origin 被 1008 拒绝 | 单测通过 |
| JSONL 6.0 | ✅ `SNAPSHOT_SCHEMA_VERSION = "6.0"`，迁移用例通过 | 生产代码 sqlite3 唯一入口 |

---

## 2. P0：GA 签收阻塞项

### P0-1 断线重连恢复审批：实际失败（可复现）

GA DoD C-14 要求：*WS 断线重连后通过 `pending_approvals` 恢复未决审批*。

**复现结果**：

```
approval_id_before_disconnect: apr_34ce4b773d
pending_after_reconnect: 0 []
RESULT: FAIL
```

服务端日志：

```
qimingxing.hub: Cancelling active turn task turn_id=t1
qimingxing.hub: Turn task cancelled cleanly turn_id=t1
qimingxing.hub: Disconnected client=client_1 role=body
```

**根因**：

- `Hub.unregister()` 在连接断开时立即调用 `_cancel_active_turn()`（`brain/hub.py:280`）；
- `_cancel_active_turn()` 会取消该回合所有 pending approval future（`brain/hub.py:696`）；
- `_request_tool_approval()` 的 `finally` 随后清理 `_pending_approval_requests`（`brain/hub.py:1560+`）；
- 重连时 `ui.state` 里自然没有审批。

**现有测试为什么没发现**：`test_ui_state_ui_command_and_approval_reconnect_sync` 是**新开第二个客户端**，不是同一个壳断开再连（`tests/test_protocol_v1_ga.py`）。

**修复方向**：

1. `unregister` 对处于审批等待的回合设置 grace（建议 120s），不要立即取消；
2. 增加稳定 `client_instance_id`，重连后仅向同一实例恢复审批；
3. grace 超时再取消回合并广播 `tool.approval_resolved{answered_by:"timeout"}`；
4. 增加“同一 WS 断开→重连”的真实回归测试。

### P0-2 macOS 原生应用联动：没有真实验收证据

GA DoD B-8/9/10 要求真实写入备忘录/日历/提醒事项、真实读取浏览器标签页、真实创建邮件草稿。

当前所有验证都是 mock：

- `tests/test_macos_apps.py` 使用 `_mock_runner`，不调用真实 `osascript`；
- `brain/eval_runner.py` 的 `macos_osascript_safety` / `macos_browser_privacy` / `macos_mail_no_send` 也使用 mock runner 或静态扫描；
- 现有运行日志中没有一次真实的 Notes / Calendar / Reminders / Mail / Safari / Chrome 调用。

**结论**：代码看起来合理，但 **B 组 4 项不能判定通过**，只能标“未验证”。

**必须补的真机验收**：

1. 用**打包后的 `Qimingxing.app`**（不是终端 Python）触发一次 Notes 搜索 + 创建；
2. 观察 TCC 自动化弹窗归属是 `Qimingxing.app` 还是 `osascript/终端`；若是后者，M2 方案需要调整（改用 Swift 壳执行 JXA 或签名 helper）；
3. 同样验证 Calendar / Reminders / Mail draft / Safari / Chrome；
4. 记录每一步的权限提示与 `-1743` 降级文案。

---

## 3. P1：签收前应修

### P1-1 签名后运行会写 `__pycache__`，导致 codesign 校验失败

真机证据：

```
$ codesign --verify --verbose=1 dist/Qimingxing.app
file modified: .../python/lib/python3.12/re/__pycache__/__init__.cpython-312.pyc
...
```

`find ... -name "__pycache__" -type d | wc -l` → **148**。

**原因**：`ProcessSupervisor.baseEnvironment()` 只设置了 `PYTHONNOUSERSITE`，没有 `PYTHONDONTWRITEBYTECODE`；Python 运行时向 app bundle 写入 `.pyc`，破坏了签名。

**修复**：`env["PYTHONDONTWRITEBYTECODE"] = "1"`（Brain/Senses 子进程都要），或在打包时预编译并签名；发布前重新 `codesign --verify`。这是后续 notarization 的前置条件。

### P1-2 Senses 与 Swift 壳重复注册全局快捷键

证据：

- Senses 日志：`actions={'chat_input': 'option+space', 'cancel': 'option+esc', 'toggle_dock': 'option+h', 'settings': 'option+k'}`；
- `senses/main.py:110-115`、`:305-312`、`:327-333` 仍注册四个动作热键；
- `shell_macos/.../HotkeyManager.swift:21-23` 同时注册 Option+Space / Option+K / Option+Esc。

后果：默认无 Godot 时多数事件被壳忽略，问题不明显；一旦开启头像，Godot 也会响应 `system.hotkey_triggered`，出现双开/双设置。规格要求 macOS 上 Senses 只保留 PTT。

**修复**：macOS 下 Senses 的 `action_hotkeys` 置空（或只保留 PTT），动作热键由壳本地注册；设置页同步所有权。

### P1-3 后台任务共享“本会话记住”的审批范围

`brain/task_runner.py:90` 只有一个 `self._session_approved_tools`，`:336` 传给所有后台任务。一个后台任务勾选“本会话记住并允许”后，后续所有后台任务都会绕过同类 CONFIRM 审批。

**修复**：按 `task_id` 隔离审批集合；跨任务复用必须重新确认。前台会话的 `_session_approved_tools` 也不要与后台共用。

### P1-4 可选头像模式与壳冲突（默认关闭，但开启即坏）

| 问题 | 证据 |
|---|---|
| Godot 仍保留 TrayManager / Settings / QuickEntry | `body_godot/scenes/Main.tscn:8,52`；`main.gd:3-4,11,35-103` |
| Godot 不处理 `ui.state` / `ui.command` | 全仓 grep 无命中 |
| 截屏隐藏仍是 `visible=false` | `avatar_controller.gd:897-906` |
| `--with-godot` 会同时出现 Swift 菜单栏 + Godot 托盘 | 构建脚本 `APP_ITEMS+=(body_godot)` 仅在 `--with-godot` |

**修复**：按补充规格移除 Godot 托盘/设置/输入框；Godot 只保留头像/气泡/音频；实现 `ui.state`/`ui.command` 或明确“头像模式为实验性、设置壳不控制头像”；截屏隐藏改为移出屏幕 + failsafe。

### P1-5 TTS 在 Swift 壳中的真实播放未验证

`BrainClient` 已接 `audio.output_start/chunk/end`，`AudioStreamPlayer` 用 AVAudioEngine 播放 24k PCM；但没有任何真机音频验收记录。

**必须验证**：默认无 Godot、关闭/开启头像两种状态下，TTS 是否只从一方出声、barge-in 是否立即停止、蓝牙设备是否正常。GA DoD C-12 包含“语音播报”。

### P1-6 `system.shortcuts_sync` 未被壳处理

`BrainClient` 只在 `system.welcome` 里读取 `shortcuts`（`BrainClient.swift:289`），没有 `case "system.shortcuts_sync"`。用户在设置里改快捷键后，Brain 会广播 `system.shortcuts_sync`，壳的热键不会更新，直到重连。

**修复**：新增 case，调用 `onShortcutsSynced` / `hotkeyManager.updateFromStrings`。

---

## 4. P2：可后置修复

| ID | 问题 | 证据/说明 |
|---|---|---|
| P2-1 | 测试套件被一个 60s 审批超时拖慢 | `test_bg_task_confirm_approval_and_foreground_chat_over_ws` 未审批前台工具，等默认 60s；建议设 `approval_timeout_sec=1` 或同时审批 |
| P2-2 | `_get_primary_body_conn` 仍 Godot 优先 | `hub.py:861`；默认无 Godot 时无影响，开启头像时语音回合主 UI 会变成 Godot（壳收镜像） |
| P2-3 | 菜单栏改音效不持久化 | `MenuBarController` 走 `ui.set_state`，Brain 只改内存；设置页保存才会写 YAML |
| P2-4 | `open_path` 未解析 symlink，且缺 `.jar/.lnk/.url` 等扩展 | `hub.py:118`；`.md` 软链到 `.app` 可绕过 |
| P2-5 | 后台任务队列无上限、报告产物无 TTL 清理 | `task_runner.submit` 无排队上限；`data/artifacts/<task_id>.md` 不会自动清理 |
| P2-6 | 后台任务使用主会话 `source_session_id` | 部分工具可能读写主会话；建议 `__bg_task__:<task_id>` |
| P2-7 | macOS 工具在非 macOS 也注册 | `build_default_tool_registry` 无条件注册，Windows 上会返回 127；v1.1 前加平台门控 |
| P2-8 | `BrainClient` 重连退避实际不增长 | `connect()` 每次把 `reconnectAttempt` 重置为 0 |
| P2-9 | 审批窗倒计时到 0 不自动关闭 | `ApprovalModalView` 只减 `remaining`，依赖 Brain 超时 |
| P2-10 | 验收报告口径偏差 | “全仓 AST”实际只扫 `brain/protocol/senses`（测试仍 import sqlite3）；166M vs 178M；B 组用 mock 却记为通过 |

---

## 5. GA DoD 对照

| 组 | 项目 | 结论 |
|---|---|---|
| A | 后台长任务：非阻塞、取消、审批、恢复、记账、交付 | ✅ 代码与测试基本通过（共享审批范围待修） |
| B | macOS 原生应用读写、浏览器隐私、邮件零外发、`-1743` | ⚠️ **未验证**（全部 mock，需真机） |
| C | 无 Godot 独立运行、Quick Entry、设置、断线审批、Origin、快捷键 | ⚠️ 部分通过：**C-14 断线审批失败**；C-15 快捷键所有权重复 |
| D | SQLite 唯一入口、JSONL 6.0、Web 零破坏 | ✅ 通过（报告措辞需修正） |
| E | 全量测试、金标评测、真机端到端 | ⚠️ 111/18 通过，但 B 组未真实执行；签名完整性失败 |

---

## 6. 建议修复顺序（约 2–4 天）

1. **P0-1**：审批等待期断线 grace + 稳定实例 ID + 真实重连回归测试。
2. **P0-2**：用打包后的 `.app` 跑一遍真实 Notes/Calendar/Reminders/Mail/浏览器；确认 TCC 归属。
3. **P1-1**：`PYTHONDONTWRITEBYTECODE=1`，重新签名并验证。
4. **P1-2**：Senses 只保留 PTT，壳独占动作热键。
5. **P1-3**：后台任务审批集合按 `task_id` 隔离。
6. **P1-6**：壳处理 `system.shortcuts_sync`。
7. **P1-4/P1-5**：头像模式与 TTS 真机验收；不通过则标“实验性”，不阻塞 GA。
8. **P2-1**：修 60s 测试，恢复快速回归。

---

## 7. 结论

- **工程完成度高**：协议、任务引擎、Web 任务面板、Swift 壳骨架、设置/Quick Entry/审批/菜单栏都已落地，基线全绿，默认无 Godot 可独立运行。
- **但“全绿”不等于“验收通过”**：C-14 是真实功能失败；B 组没有真实验证；签名在运行后失效。
- 建议状态：**v1.0.0-rc（Rough MVP）**，先修 P0/P1，再签 GA。

---

## 8. 修复进展（2026-11-25）

| 项 | 状态 | 说明 |
|---|---|---|
| Quick Entry 无法输入 | ✅ 已修 | 自定义 `NSTextView` 无 frame/autoresizing，文档视图零尺寸；补齐 frame/width sync、`isEditable`、焦点重试与失败兜底 |
| P0-1 断线审批恢复 | ✅ 已修 | `client_instance_id` + 断线 grace（120s）+ 原 `ClientConnection` 重绑新 WebSocket + outbox 重放；新增同连接断开重连回归测试 |
| P1-1 签名失效 | ✅ 已修 | `PYTHONDONTWRITEBYTECODE=1`；构建脚本清理 `__pycache__`/`.pyc` 后再签名 |
| P1-2 重复快捷键 | ✅ 已修 | macOS 上 Senses 只保留 PTT 与 `toggle_dock`，Quick Entry/设置/急停归 Swift 壳 |
| P1-3 后台审批共享 | ✅ 已修 | 每个后台任务独立 `session_approved_tools` |
| P1-6 shortcuts_sync | ✅ 已修 | 壳新增 `system.shortcuts_sync` 处理，并修正重连退避重置 |
| P2-1 60s 测试 | ✅ 已修 | 测试将前台审批超时设为 1s；全量套件 112 passed / **6.4s** |

**验证**：`pytest -q` 112 passed / 6.4s；`cli.py eval` 18/18；`swift build -c release` 通过；`protocol.schema.json` 已重新导出。

**尚未完成**：

- `dist/Qimingxing.app` 仍运行旧代码；需退出当前 app 后重新执行 `scripts/build_macos_app.sh`，再做一次 `codesign --verify`。
- P0-2：真实 macOS 应用/TCC 验收仍需在打包后的 `.app` 上人工执行。
- P1-4/P1-5：可选 Godot 瘦身与 Swift 壳 TTS 真机验收。
- P2：`_get_primary_body_conn` Godot 优先、菜单栏开关持久化、`open_path` symlink、任务队列/产物清理、非 macOS 工具注册等。
