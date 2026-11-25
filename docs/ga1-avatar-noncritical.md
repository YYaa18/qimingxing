# GA 1.0 补充规格：头像插件非关键化与主成果面板

| 项目 | 内容 |
|---|---|
| 文档状态 | 已确认 — 对 GA 1.0 规格的补充 |
| 关联文档 | `启明星（Qimingxing）— GA 1.0（v1.0.0）工作内容与验收标准.md` |
| 日期 | 2026-11-25 |
| 适用范围 | macOS 原生壳、Godot 头像、Brain 路由、Senses 热键归属、GA 验收 |
| 核心结论 | **原生壳是唯一必需 UI；Godot 头像只是可选动画插件，拔掉不影响任何主功能** |

---

## 1. 术语

| 术语 | 含义 | 定位 |
|---|---|---|
| **原生壳（shell_macos）** | Swift 6 + SwiftUI/AppKit 的 `Qimingxing.app` | 主功能载体 |
| **头像插件（avatar_godot）** | Godot 透明置顶窗口，仅头像/气泡/表情 | 可选装饰 |
| **Brain** | Python 后端，状态与能力中心 | 核心服务 |
| **Senses** | Python 感知与音频客户端 | 核心服务 |
| **成果面板（Result Panel）** | 原生壳内承载流式回复、工具状态、产物的主界面 | GA 1.0 必需 |
| **工作台（Workbench）** | 未来的空间化多卡片界面（钢铁侠式） | v1.1+，非 GA 范围 |

> 本文中的“壳子/躯壳”统一指头像插件，不指原生壳。

---

## 2. 核心原则

**P1. 原生壳 100% 独立可用。**
头像插件关闭、崩溃、未安装、版本不匹配时，所有主功能不受影响。

**P2. 头像只消费只读装饰事件。**
头像接收 `avatar.state` 与 `mirror_turns` 镜像流，用于动画和气泡；不拥有任何回合、审批、任务、通知或热键。

**P3. 任何核心路径不得 `await` 头像。**
Brain 与原生壳都不得等待头像上线、响应或恢复。

**P4. 同一时刻只有一个 TTS 播放方。**
头像开启时归头像；头像关闭时归原生壳。禁止双声。

**P5. 默认关闭。**
`ui.avatar_enabled` 默认 `false`；启动流程不包含 Godot。

**P6. 故障隔离。**
头像崩溃最多自动重启一次；失败后静默降级，仅在用户显式开启头像时提示。

**P7. 性能与体积单独计账。**
核心性能指标在头像关闭状态下测量；头像资源不参与主包启动关键路径。

---

## 3. 所有权矩阵

| 能力 | 原生壳 | 头像插件 | Brain | Senses |
|---|---|---|---|---|
| 菜单栏 / 托盘 | ✅ | ❌ | — | — |
| Quick Entry / 主对话面板 | ✅ | ❌ | — | — |
| 设置中心 | ✅ | ❌ | 配置 API | — |
| 审批 / 澄清 UI | ✅ | ❌（仅表情） | 路由 | — |
| 后台任务状态 | ✅ | ❌ | 执行/持久化 | — |
| 原生通知 | ✅ | ❌ | 事件 | — |
| TTS 播放 | ✅（头像关闭时） | ✅（头像开启时） | 路由 | — |
| 语音采集 / PTT | ❌ | ❌ | 路由 | ✅ |
| 看屏 / 隐私守卫 | ❌ | ❌ | 路由 | ✅ |
| 头像动画 / 气泡 | ❌ | ✅ | 状态 | — |
| 全局 UI 快捷键 | ✅ | ❌ | — | 仅 PTT |
| 桌面动作（reveal/open/copy/QL） | ✅ | ❌ | 路由 | — |
| 钉图 | ✅ | ❌ | 产物 | — |

---

## 4. 路由规则（必须实现）

### 4.1 主 UI 连接

```
主 UI = body_macos（原生壳）
      → body_web（调试台）
      → body_godot（仅当原生壳缺失时兜底）
```

回合输出、工具状态、审批、任务状态、通知，默认发往主 UI。

### 4.2 TTS owner

```
if avatar_enabled and body_godot connected and audio_output:
    TTS → body_godot
else:
    TTS → body_macos
```

- 绝不能同时向两者发送 `audio.output_*`。
- 头像开启但音频设备异常时，原生壳不自动接管，避免双声；由用户切换。

### 4.3 镜像

- `body_godot` 声明 `mirror_turns` 时，接收只读的 `output.turn_start/text_delta/turn_end`、`avatar.state`、`output.tool_status`。
- 镜像事件不得反向驱动回合、审批或任务。
- 头像未连接时，Brain 直接跳过镜像，不报错、不重试。

### 4.4 热键归属

| 热键 | Owner | 说明 |
|---|---|---|
| PTT 按住说话 | Senses | 保持现状 |
| Quick Entry | 原生壳 | 全局注册；Brain 重启不影响 |
| 设置 | 原生壳 | 全局注册 |
| 急停 | 原生壳 | 本地停播 + 发 `input.cancel` |
| 贴边 / 复位 | 原生壳 → `ui.command` → 头像 | 头像关闭时忽略 |
| 缩放 / 漫步 | 设置页 → `ui.command` → 头像 | 头像关闭时隐藏 |

macOS 上 Senses **不得**再注册 `chat_input` / `cancel` / `settings` 动作热键，避免与壳重复触发。

### 4.5 截屏

- 截屏始终由 Senses 执行，与头像无关。
- `system.capture_prepare` / `capture_done` 仅发给声明 `auto_hide` 的头像。
- 头像关闭时，截屏流程不等待、不报错。
- 头像启用时，隐藏方式为移出屏幕 + failsafe，不使用 `visible=false`。

---

## 5. 生命周期与故障隔离

### 5.1 启动

1. 原生壳启动，注册菜单栏（启动中态）。
2. 启动 Brain，轮询 `/health`。
3. 启动 Senses。
4. 原生壳建立 WS，收到 `system.welcome` + `ui.state` 后切为已连接。
5. **仅当** `ui.avatar_enabled == true` 时，异步启动 Godot；启动失败不影响前四步。

### 5.2 运行

- 头像断开：Brain 仅记录日志；镜像跳过；TTS 若原归头像，按 4.2 规则回落到壳（需明确：仅在头像进程退出且开关仍开启时，壳接管并提示一次）。
- 头像崩溃：壳最多重启一次；仍失败则把 `avatar_enabled` 置为 `false` 并通知一次。
- 头像卡顿/高 CPU：壳提供"关闭头像"快捷入口，不阻塞主 UI。

### 5.3 退出

- 先停头像（若在运行）→ 停 Senses → 停 Brain。
- 任一步超时强杀；确保无孤儿进程。
- 头像不存在时，退出流程不得等待。

---

## 6. 配置与 UX 降级

```yaml
ui:
  avatar_enabled: false
  avatar_scale: 1.0
  roam: true
  earcons: true
  ambient: false
```

- 设置页「通用与外观」提供“启用桌面头像（实验性/装饰性）”开关，默认关闭。
- 关闭时，缩放、漫步、贴边、音效联动等头像专属项隐藏或置灰，并给出说明。
- 开启时，若 Godot 未安装/启动失败，展示一次可关闭的提示，不阻塞设置保存。
- 菜单栏不展示头像状态；头像状态只在设置页和托盘弹层的次要位置出现。

---

## 7. 主成果面板（GA 1.0 必需）

头像关闭后，流式回复、工具状态、产物必须有一个稳定落点。原生壳必须提供 **成果面板**：

### 7.1 最低能力

- 展示 user / assistant 多轮消息；
- 流式 delta 实时渲染；
- 工具执行状态（running/completed/error）与步骤；
- 后台任务卡片（标题、进度、当前步骤、取消、查看结果）；
- 产物卡片（标题、类型、Quick Look / Reveal / Copy）；
- 审批/澄清请求（Sheet 或内联卡片）；
- 错误与重连提示；
- 历史回看（至少当前会话）。

### 7.2 形态

GA 1.0 不要求空间化。推荐二选一：

- Quick Entry 提交后原地扩展为 transcript 面板；或
- 菜单栏弹层中的“对话”页 / 独立 `NSWindow`。

要求：可键盘操作、可复制、可滚动、可恢复窗口状态；不依赖头像气泡。

### 7.3 与头像气泡的关系

- 气泡是成果面板的装饰性重复显示，不是唯一输出。
- 头像关闭时，成果面板行为完全一致。

---

## 8. 性能与打包口径

| 指标 | 测量条件 |
|---|---|
| 壳启动到菜单栏可见 < 1s | 头像关闭 |
| Brain `/health` 冷启动 < 5s | 头像关闭 |
| 空闲总 CPU < 3% | 头像关闭 |
| 常驻内存 | 壳、Brain、Senses 分别计；头像单独列 |
| 主包体积 | 不含 Godot 运行时；若打包，标注为可选组件 |

- Godot 运行时是否随包：见开放问题 Q5；无论哪种，默认不启动。
- 若随包，安装器/打包脚本必须支持“不安装头像组件”的最小安装。

---

## 9. 验收标准（在 GA DoD 上追加）

| ID | 验收项 | 判定方式 |
|---|---|---|
| A1 | 默认配置不启动 Godot | 启动后 `pgrep -f Godot` 为空；日志无头像启动记录 |
| A2 | 无头像全链路 | 关闭头像，完成文本、语音、看屏、附件、审批、后台任务、通知、设置、Quick Look/Reveal/Copy |
| A3 | 头像崩溃隔离 | 手动 kill Godot；壳不崩溃、不阻塞；最多重启一次；主流程继续 |
| A4 | TTS 单一 owner | 头像开/关两种状态下，均只有一个进程出声；抓包确认无重复 `audio.output_*` |
| A5 | 核心路径零等待 | 代码审查 + 测试：Brain/壳无 `await` 头像响应；头像离线时回合正常 |
| A6 | 性能口径 | 报告注明头像关闭；头像开启数据单独附录 |
| A7 | 设置降级 | 头像关闭时，专属项隐藏/置灰且有说明 |
| A8 | 成果面板 | 无头像状态下，流式文字、工具状态、任务卡、产物卡均可见可操作 |

---

## 10. 代码级改造点

| 位置 | 现状 | 要求 |
|---|---|---|
| `brain/hub.py:657` `_get_primary_body_conn` | 优先 `body_godot` / `audio_output` | 拆分主 UI 与 TTS owner |
| `brain/hub.py:668` `_get_tts_target_conns` | 发给所有 `audio_output` | 保证单一 owner |
| `brain/hub.py:836` `_handle_audio_input_end` | 语音回合发给 primary body | 发往主 UI（壳），头像只镜像 |
| `brain/hub.py:249` `unregister` | 立即取消当前回合 | 审批等待期保留 grace，配合恢复 |
| `brain/hub.py:1237` `_request_tool_approval` | `(turn_id, future)` | 增加 origin/instance/task 元数据 |
| `brain/config.py:1031/1331` | 显式字段白名单 | 增加 `ui:` 段与兼容回退 |
| `senses/main.py:110/305` | 注册 chat/cancel/settings/dock | macOS 只保留 PTT；动作热键归壳 |
| `body_godot/scripts/main.gd` | 托盘/设置/快捷输入接线 | 移除；仅保留头像/气泡/音频 |
| `body_godot/scripts/avatar_controller.gd` | `visible=false` 隐藏 | 改为移出屏幕 + failsafe |
| `body_web/app.js` | 固定事件 if 链 | 增加 task.status/approval_resolved/成果视图 |
| `brain/main.py:583` `/ws` | 无 Origin 校验 | 增加 Origin 白名单（端口取配置） |

---

## 11. 给开发 Agent 的硬约束

1. **默认配置不启动 Godot；所有 GA DoD 必须在无 Godot 下通过。**
2. **任何核心路径不得依赖 `body_godot`**：主 UI 路由 shell-first，审批/任务/通知只走原生壳，TTS 单一 owner。
3. **头像只消费只读镜像/装饰事件**：断连、崩溃、未安装都不得阻塞壳；壳不能 `await` 头像的任何响应。

---

## 12. 与工作台（Workbench）的关系

- GA 1.0 只交付**成果面板**（列表/卡片流），不交付空间化工作台。
- 但 GA 1.0 的后台任务与工具结果应输出**结构化卡片数据**（见下），为 v1.1 工作台复用，避免返工。
- 工作台是原生壳的演进，不使用 Godot 实现；头像仍只是装饰插件。

建议 GA 1.0 即冻结最小卡片模型：

```json
{
  "card_id": "card_xxx",
  "kind": "markdown|code|image|pdf|web|task|artifact|approval|note|calendar|mail|browser_tab",
  "title": "",
  "subtitle": "",
  "body": {},
  "source": {"task_id": "", "artifact_id": "", "turn_id": ""},
  "actions": ["quick_look", "reveal", "copy", "open"],
  "created_at": 0,
  "updated_at": 0
}
```

GA 1.0 由成果面板渲染为列表；v1.1 由工作台渲染为空间卡片。协议字段一次定义，两代 UI 共用。
