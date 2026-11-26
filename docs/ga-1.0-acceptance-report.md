# 启明星（Qimingxing）GA 1.0（v1.0.0）验收报告

- **目标版本**：`v1.0.0`（分支 `feat/ga-1.0`）
- **版本唯一口径**：`version.txt` = `1.0.0`（同步至 `pyproject.toml`、`brain/hub.py:SERVER_VERSION`、`Qimingxing.app/Contents/Info.plist` 与 `system.hello.app_version`）
- **应用产物**：`dist/Qimingxing.app`（原生 `arm64` Swift 6 壳，默认不打包/不启动 Godot）

> **最终签收状态（2026-11-26，`v1.0.0-ga`）**
>
> - 最终分支：`feat/ga-1.0`；在 GA 基线上合并 provider adapter、模型 SQLite 配置、KV 缓存观测、Tavily 图片搜索、工作区媒体与 MediaPreview。
> - 自动化：`pytest` **191 passed**；`brain.memory.cli eval` **18 / 18**；`swift build -c release --arch arm64` 通过。
> - 新增媒体链路：`SourceItem` / `MediaItem`、`output.turn_end.sources/media`、持久 `data/media/image_search`、工作区媒体侧翼与全量详情、媒体预览双标签侧翼/独立窗口拆分停靠。
> - 已验证：真实 Tavily 中文搜图；单击打开预览、双击 QuickLook、方向键切图、拆分/停靠、新回答提示条；Tavily Key 通过设置页 0600 保存并热更新。
> - 已知限制：Tavily 顶层图片可能没有来源页；静态图 EXIF 未做统一剥离；本机为 ad-hoc 签名，分发前仍需 Developer ID 签名与公证。

---

## 一、核心交付物清单

1. **后台异步长任务引擎**：
   - [brain/task_runner.py](file:///Users/yangyang/Desktop/qimingxing/brain/task_runner.py)：基于 `asyncio.Semaphore(2)` 的轻量任务池，`max_steps=12`、`timeout_sec=300`；独立隔离上下文，不污染主对话 L0/L1 `static_prefix_hash`；消耗记入 `usage_ledger`（`slot="bg_task"`）；支持随时取消、触发 `CONFIRM` 审批挂起（`waiting_approval`）、启动自动将遗留任务标 `interrupted`；完成后自动写入 `work_history`、生成 Markdown 产物并推送 `output.proactive_notice` 与 `output.artifact_capsule`。
   - [brain/tools/bg_task_tools.py](file:///Users/yangyang/Desktop/qimingxing/brain/tools/bg_task_tools.py)：注册 `bg_task_submit`、`bg_task_list`、`bg_task_cancel`、`bg_task_inspect`。
   - [brain/memory/sqlite_store.py](file:///Users/yangyang/Desktop/qimingxing/brain/memory/sqlite_store.py)：新增 `background_tasks` 表与 JSONL `6.0` 无损迁移。
2. **macOS 原生应用深度联动工具集**：
   - [brain/tools/macos_apps.py](file:///Users/yangyang/Desktop/qimingxing/brain/tools/macos_apps.py)：防注入 `run_osascript`（`stdin` 传脚本 + `argv` 传参，硬超时 `5s`，捕获 `-1743` 返回中文授权指引）；实现备忘录（`macos_notes_search` / `macos_notes_create`）、日历（`macos_calendar_list` / `macos_calendar_create`）、提醒事项（`macos_reminders_list` / `macos_reminders_create`）、浏览器标签页（`macos_browser_tabs`，强制隐私黑名单过滤）与邮件（`macos_mail_search` / `macos_mail_draft`，零外发调用）。
3. **协议 v1.0 增量扩展与安全边界**：
   - [protocol/models.py](file:///Users/yangyang/Desktop/qimingxing/protocol/models.py) 与 [protocol.schema.json](file:///Users/yangyang/Desktop/qimingxing/protocol.schema.json)：新增 `task.status`、`ui.command`、`ui.command_result`、`ui.state`（含 `pending_approvals`）、`ui.set_state`、`tool.approval_resolved`、`body_macos` / `body_windows` 能力位。
   - [brain/main.py](file:///Users/yangyang/Desktop/qimingxing/brain/main.py)：`/ws` 握手强制校验 `Origin`，拒绝跨站 WebSocket 劫持；新增 `/api/tasks` 与 `/api/tasks/{task_id}/cancel`；`open_path` 拦截危险可执行扩展名。
4. **macOS 原生 Swift 壳（`shell_macos`）与打包脚本**：
   - [shell_macos/](file:///Users/yangyang/Desktop/qimingxing/shell_macos/Package.swift)：Swift 6 + SwiftUI/AppKit 实现，包含 `ProcessSupervisor` 子进程守护、`MenuBarController` 模板菜单栏与 Popover、`QuickEntryPanel`（`.nonactivatingPanel` + `.fullScreenAuxiliary` + `NSTextView` 中文 IME 与焦点归还）、`ApprovalSheetController` 原生审批窗、`SettingsWindowController` 五栏设置中心、`HotkeyManager` 壳内快捷键、`DesktopActions`（Quick Look / Reveal / Copy / 通知）与 `AudioStreamPlayer` 流式 TTS 播放。
   - [scripts/build_macos_app.sh](file:///Users/yangyang/Desktop/qimingxing/scripts/build_macos_app.sh)：编译 `shell_macos` 并打包独立可运行的 `dist/Qimingxing.app`（`LSUIElement = true`，默认不启 Godot，可选 `--with-godot`）。
5. **Web 调试台兼容更新**：
   - [body_web/app.js](file:///Users/yangyang/Desktop/qimingxing/body_web/app.js)、[body_web/index.html](file:///Users/yangyang/Desktop/qimingxing/body_web/index.html)、[body_web/styles.css](file:///Users/yangyang/Desktop/qimingxing/body_web/styles.css)：支持 `task.status`、`tool.approval_resolved` 与 `ui.state`（`pending_approvals`）并增加后台任务查看/取消面板。

---

## 二、量化验收结果汇总

| 验收项目 | 验证方式 | 结果 |
| --- | --- | --- |
| 全量单元测试 | `.venv/bin/pytest -q` | **191 passed**（最终 GA 1.0 回归） |
| 个人金标回归评测 | `.venv/bin/python -m brain.memory.cli eval` | **18 / 18 通过（100.0%）** |
| Web 调试台语法检查 | `node --check body_web/app.js` | **通过（退出码 0）** |
| Swift 6 原生壳编译 | `cd shell_macos && swift build -c release --arch arm64` | **通过（0 错误 0 警告）** |
| 原生 `.app` 独立打包 | `bash scripts/build_macos_app.sh` | **生成 `dist/Qimingxing.app`（166M，Ad-hoc 签名通过，无 Godot）** |
| 无 Godot 独立启停实测 | 启动 `dist/Qimingxing.app/Contents/MacOS/Qimingxing` 并检测 `/health` 与退出清理 | **`/health` 返回 `server_version: "1.0.0"`，Godot 进程数为 0，退出后零残留子进程** |
| SQLite 唯一入口约束 | 全仓 AST 扫描（`test_sqlite_discipline_and_jsonl6`） | **仅 `brain/memory/sqlite_store.py` 导入 `sqlite3`** |
| 邮件零外发红线 | 静态源码扫描（`test_mail_search_redacted_and_mail_draft_never_sends`） | **`brain/tools/macos_apps.py` 零 `.send(` / `send()` 调用** |
