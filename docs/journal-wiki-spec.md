# 启明星日记本 / 本地 Wiki 规格（Journal & Wiki）

| 项目 | 内容 |
|---|---|
| 文档状态 | Draft — 待 Review |
| 日期 | 2026-11-25 |
| 目标 | 用开源能力做一个**可持续维护的长久日记本**，而不是花哨工作台 |
| 核心体验 | 助手把产出写进本地 Wiki，告诉用户"放在哪 + 本地链接"，用户点开即可回顾 |
| 关联文档 | `docs/ga1-avatar-noncritical.md`、GA 1.0 规格 |

---

## 1. 结论

**做，而且这是比 Workbench 更正确的第一优先级。**

- **不做自研 Wiki 引擎，不做空间化工作台。**
- **以 Markdown 文件为唯一事实来源**，保证十年后仍然可读、可迁移、可备份。
- **本地 Wiki 渲染器用开源项目**：首选 **SilverBullet（MIT）**，备选 **Memos（MIT）**。
- 助手通过工具写入 Markdown / 附件，然后返回一个本地 URL。
- 原生壳只负责"打开日记本"窗口和通知，不负责渲染逻辑。

一句话：**Wiki 是成果的归档与回顾层，不是新的主界面。**

---

## 2. 需求映射

| 需求 | 方案 |
|---|---|
| 各种媒体信息记载 | Markdown + 附件目录；图片/PDF/音视频文件以链接嵌入 |
| 产出放到某个 Wiki 下 | 每日日志页 + 主题页 + 来源页；工具自动写入 |
| 给出可访问本地连接 | 本地 Wiki 服务 `http://127.0.0.1:<port>/...` |
| 方便回顾 | 日记前后翻页、主题页 linked mentions、搜索、标签、周/月摘要 |
| 可持续维护 | Markdown 明文 + 可选 git + 可替换渲染器；无专有数据库锁定 |
| 不做花哨工作台 | 单窗口 Web 视图 + 菜单项 + 通知，不做画布/3D |

---

## 3. 开源选型

### 3.1 候选对比

| 项目 | License | 存储 | 日记 | 媒体 | 搜索/回顾 | 本地链接 | 维护性 | 结论 |
|---|---|---|---|---|---|---|---|---|
| **SilverBullet** | MIT | Markdown 文件夹（space） | ✅ 内置 Journal | ✅ 图片/PDF 与 md 同目录 | ✅ 双向链接/Linked Mentions/Search | ✅ 本地服务/PWA | 活跃，~6.6k stars | **首选** |
| **Memos** | MIT | SQLite（非 Markdown） | ✅ 时间线 | ✅ | ✅ 标签/搜索 | ✅ 单二进制 | 活跃，~64k stars | 备选（日记优先） |
| **LeafWiki** | MIT | Markdown + SQLite 索引 | ⚠️ 需自建每日页 | ✅ | ✅ 全文搜索/修订 | ✅ 单二进制 | 活跃，~1.2k stars | 不推荐（见 3.2） |
| **An Otter Wiki** | MIT | Markdown in git | ⚠️ 需自建 | ✅ 附件 | ⚠️ 无全文搜索 | ✅ Python/Flask | 活跃，~1.6k stars | Python-only 备选 |
| **AIWiki (iTradingAI)** | MIT | 本地 Markdown + JSON | ❌ 无 GUI/服务 | ⚠️ 未明确 | ⚠️ CLI/MCP | ❌ 无本地 Web | 早期，v1.0.2，10 stars | 借鉴其 schema，不作依赖 |
| **LLM Wiki** | GPL-3.0 | Markdown + LanceDB | ❌ 文档中心 | ✅ 丰富 | ✅ | ✅ Tauri 本地 API | 活跃，~22k stars | 不推荐（GPL + 重型） |
| **Trilium** | AGPL-3.0 | SQLite/自有 | ⚠️ Day notes | ✅ | ✅ | ✅ | 活跃 | 不推荐（AGPL + 复杂） |
| **MkDocs Material** | MIT | Markdown | ❌ | ✅ | ✅ | ✅ 静态站 | **2027-05 EOL** | 不推荐（长期风险） |
| **Wiki.js / Outline / BookStack** | AGPL/BSL | 数据库 | ❌ | ✅ | ✅ | ✅ | 重 | 不推荐（重、非文件优先） |

### 3.2 关键发现

1. **AIWiki 本身不是产品级 Wiki**：MIT、Node 20+、CLI + MCP，本地 Markdown + source capsules + index/graph，但没有 Web 服务/GUI，媒体支持未明确，v1.0.2 仅 10 stars。**借鉴它的“来源胶囊”结构，不把它当运行时依赖。**
2. **LeafWiki 不监听文件系统**：外部写入 `.md` 后必须触发 resync（`kill -USR1`，Windows 不支持信号）或 Admin UI 手动重建。助手持续写入会不断卡在索引不更新上，不适合本项目。
3. **MkDocs Material 将于 2027-05-05 EOL**，继任者 Zensical（MIT/Rust）仍在 beta。长期日记不应押注 EOL 项目。
4. **SilverBullet 的 Journal 是内置能力**：默认 `Journal/YYYY-MM-DD`，有模板、前后翻页、日期选择器；主题页的 **Linked Mentions** 会自动形成倒序活动日志，正好满足“方便回顾”。
5. **SilverBullet 以 Markdown 文件夹为 space**：图片/PDF 与笔记同目录，符合“可持续维护”的要求；MIT，Rust 后端 + TS 前端，预编译二进制可本地运行。
6. **Memos 是“日记优先”的备选**：MIT、单 Go 二进制、时间线、媒体、REST + MCP，写入最可靠；但数据在 SQLite，不是 Markdown 事实来源，Wiki 结构弱。

### 3.3 推荐

**首选：SilverBullet**
- Markdown space + 内置 Journal + Linked Mentions + 搜索 + 本地链接；
- 助手直接写 Markdown 文件或用 space HTTP API，用户获得 `http://127.0.0.1:18791/Journal/2026-11-25`；
- 可替换：即使以后换渲染器，Markdown 文件仍在。

**备选：Memos**
- 如果用户更想要“时间线日记”而不是“Wiki 结构”，Memos 更快；
- 但需接受 SQLite 存储与较弱的 Wiki 层级/双向链接。

**不采用**：LeafWiki（无文件监听）、MkDocs Material（EOL）、Trilium/LLM Wiki（AGPL/GPL + 重型）、Wiki.js/Outline（数据库 + 重）。

---

## 4. 总体架构

```
Qimingxing.app (Swift 原生壳)
  ├─ 菜单栏：打开日记本 / 今日回顾 / 在浏览器打开
  ├─ 日记窗口：WKWebView → http://127.0.0.1:18791/...
  └─ 启动/守护 SilverBullet 本地服务（space = Markdown 目录）
        ▲
        │ 文件 / HTTP API
┌───────┴────────┐
│ brain (Python)  │
│ journal 工具    │
└───────┬────────┘
        │ 写入 Markdown + 附件
┌───────▼──────────────────────────────┐
│ Wiki Space（唯一事实来源，Markdown）   │
│ Journal/2026/2026-11-25.md           │
│ topics/<slug>.md                     │
│ sources/2026/11/<source-id>.md       │
│ assets/2026/11/<hash>.<ext>          │
│ index.md / tags/ / .qimingxing/      │
└──────────────────────────────────────┘
```

- **不依赖 Godot 头像**；头像关闭时日记本照常工作。
- **不依赖 Workbench**；日记本是成果的归档与回顾层。
- 原生壳只做窗口与生命周期；渲染交给 SilverBullet。

---

## 5. 数据模型

### 5.1 目录结构

```
wiki/
  index.md
  Journal/
    2026/
      2026-11-25.md
  topics/
    qimingxing.md
    macos-automation.md
  sources/
    2026/
      11/
        src_ab12cd34.md
  assets/
    2026/
      11/
        img_ab12cd34.png
  tags/
    work.md
    research.md
  .qimingxing/
    meta.json
    templates/
      journal.md
  .git/                     # 可选，本地版本历史
```

### 5.2 每日日志页

`Journal/2026/2026-11-25.md`：

```markdown
---
date: 2026-11-25
type: journal
tags: [work, qimingxing]
sources: [src_ab12cd34]
tasks: [task_ab12cd34]
---

# 2026-11-25

## 09:40 后台任务：浏览器标签页周报
- 汇总了 7 天内 128 个标签页，来源 [[src_ab12cd34]]
- 产出：[[topics/qimingxing-weekly]]
- 附件：![[assets/2026/11/img_ab12cd34.png]]

## 14:10 与用户的讨论
- 决定不做 Workbench，先做日记本
- 关联 [[topics/journal-wiki]]
```

### 5.3 主题页

`topics/<slug>.md`：

```markdown
---
title: Qimingxing 周报
type: topic
tags: [qimingxing, weekly]
sources: [src_ab12cd34]
created_at: 2026-11-25T09:40:00+08:00
---

# Qimingxing 周报

（正文）

## 来源
- [[sources/2026/11/src_ab12cd34]]
```

### 5.4 来源胶囊（借鉴 AIWiki）

`sources/2026/11/src_ab12cd34.md`：

```markdown
---
source_id: src_ab12cd34
url: https://example.com/article
title: ...
captured_at: 2026-11-25T09:30:00+08:00
content_hash: sha256:...
task_id: task_ab12cd34
model: deepseek-chat
---

（原始摘录 / 关键段落，4000 字截断并脱敏）
```

- 作用：产出可追溯；用户能回到原始来源；避免"AI 说了但不知道从哪来"。
- 运行记录：`.qimingxing/runs/<run-id>.json` 记录模型、tokens、费用、工具步骤。

### 5.5 附件

- 统一复制到 `assets/YYYY/MM/<hash>.<ext>`；
- Markdown 使用相对链接；
- 支持图片、PDF、音视频、任意文件；
- 单文件大小上限（建议 50MB，可配置）；
- 默认不远程引用图片，避免追踪像素与隐私泄漏。

---

## 6. 助手工具

| 工具 | 作用 | 返回 |
|---|---|---|
| `journal_append(text, date?, tags?, sources?, attachments?)` | 追加到当日日志；不存在则创建 | 页面路径 + 本地 URL |
| `wiki_create_page(title, content, tags?, sources?)` | 创建主题页 | 页面路径 + 本地 URL |
| `wiki_update_page(path, patch)` | 更新页面 | 本地 URL |
| `wiki_attach(path or bytes, name?)` | 复制附件到 assets | Markdown 链接 |
| `wiki_search(query, limit?)` | 搜索标题/正文/标签 | 命中列表 |
| `wiki_read(path)` | 读取页面 | 正文 |
| `wiki_open(path?)` | 返回本地 URL（必要时让壳打开） | URL |
| `wiki_digest(period)` | 生成周/月摘要页 | 页面路径 + URL |

规则：

- 写入前做密钥脱敏（复用 `redact_secrets`）；
- 附件先复制再引用，禁止引用 vault 外路径；
- 同名页面默认追加/更新，不静默覆盖用户内容；冲突时生成 `-2` 或询问；
- 每次写入返回 `{path, url}`，供助手在回复里直接告知用户。

---

## 7. 本地服务

- 进程：SilverBullet 预编译二进制，由原生壳启动/守护；
- 绑定：`127.0.0.1`，端口默认 `18791`（冲突时自动顺延并持久化）；
- space：`~/Library/Application Support/Qimingxing/wiki/`（macOS）/ `%APPDATA%\Qimingxing\wiki\`（Windows）；
- 健康检查：`GET /` 或专用 health；
- 认证：loopback 默认关闭；如开启，token 由壳生成并注入 WKWebView；
- 日志：`~/Library/Logs/Qimingxing/wiki.log`；
- 退出：壳退出时优雅停止；超时强杀；
- 失败：服务起不来时日记本禁用，但聊天/任务不受影响，菜单项给出"查看日志"。

**待 spike 验证**：

1. 外部写入 Markdown 后，SilverBullet 是否自动刷新/索引；若否，是否有 reload/reindex 接口或文件监听。
2. 图片/PDF/音视频的渲染与附件上传方式。
3. Journal 模板、`Journal/YYYY/YYYY-MM-DD` 前缀是否可配置。
4. 本地服务的启动时间、内存占用、端口配置、认证。
5. 预编译二进制在 macOS arm64 与 Windows 的分发方式。

---

## 8. 原生壳 UX

- 菜单栏新增：
  - **打开日记本**（WKWebView 窗口）
  - **今日回顾**（直接打开今天）
  - **在浏览器打开**
  - **在 Finder 中显示 Wiki**
- 日记窗口：
  - 工具栏：今天 / 前一天 / 后一天 / 搜索 / 在浏览器打开；
  - 关闭后保留窗口状态；
  - 不阻塞主 UI；服务异常时显示可重试页。
- 任务完成：
  - 原生通知：`已整理到今日日记`，动作"打开"；
  - 点击直达对应日志锚点或主题页。
- 不新增画布、卡片拖拽、3D 背景；保持简单。

---

## 9. 回顾与摘要

- **日记**：前后翻页、日期选择、标签过滤；
- **主题页**：Linked Mentions 自动汇总相关日志；
- **搜索**：标题/正文/标签；
- **周/月摘要**：助手生成 `Journal/Digests/2026-W48.md`，链接到每日条目；
- **On this day**：助手可查询去年/上月同一天；
- **任务回溯**：任务卡链接到日志页与来源胶囊。

---

## 10. 安全与隐私

| 项 | 要求 |
|---|---|
| 网络 | 仅 `127.0.0.1`；不监听公网 |
| 认证 | loopback 可选 token；默认关闭但记录风险 |
| 路径 | 所有写入限制在 space 内；拒绝 `..`/符号链接逃逸 |
| 内容 | 写入前脱敏；不记录 API Key；日志只记路径与计数 |
| 媒体 | 类型 allowlist、大小上限；不自动下载远程图片 |
| 备份 | Markdown + 附件可直接复制；可选本地 git；不默认推远端 |
| 敏感页 | 支持 `private: true` frontmatter，搜索/摘要默认跳过（若渲染器支持） |
| 截屏 | 日记窗口不参与头像截屏隐藏逻辑；如需隐私可后续加窗口排除 |

---

## 11. 失败模式

| 场景 | 行为 |
|---|---|
| Wiki 服务未启动 | 日记本菜单禁用/提示；聊天与任务正常 |
| 写入失败 | 任务结果仍在成果面板；返回错误并建议重试 |
| 端口占用 | 自动顺延端口并持久化 |
| space 缺失 | 自动创建目录与模板 |
| 外部写入未索引 | 触发 reload/resync；spike 决定机制 |
| 附件过大 | 拒绝并提示上限；保留原路径引用 |
| 服务崩溃 | 壳重启一次；失败后禁用日记本，不影响主功能 |
| 用户手动改文件 | 以文件为准；下次索引/重载后生效 |

---

## 12. 验收标准

| ID | 验收项 | 判定 |
|---|---|---|
| J1 | 助手写入当日日志 | 2s 内本地 URL 可见新内容 |
| J2 | 主题页 + 来源胶囊 | 页面互链、来源可回溯 |
| J3 | 媒体 | 图片/PDF 正常渲染；音视频可打开 |
| J4 | 回顾 | 前后翻页、搜索、Linked Mentions 可用 |
| J5 | 通知 | 任务完成通知可直达日志页 |
| J6 | 可持续 | 删除渲染器后 Markdown 仍可读；复制目录即可备份 |
| J7 | 隔离 | Wiki 服务崩溃不影响聊天/任务；头像关闭不影响日记 |
| J8 | 安全 | 仅 loopback；无密钥写入；路径逃逸被拒 |
| J9 | 外部写入 | 助手写文件后，UI 在约定时间内可见（spike 定阈值） |

---

## 13. 实施计划

| 阶段 | 内容 | 估时 |
|---|---|---|
| J0 | Spike：SilverBullet 运行、外部写入刷新、Journal 模板、媒体、端口/认证、内存 | 1–2 天 |
| J1 | Space 结构 + 模板 + Brain 工具 + 本地服务生命周期 + 菜单/WKWebView | 3–5 天 |
| J2 | 媒体附件 + 来源胶囊 + 周/月摘要 + 通知深链 + 验收 | 2–3 天 |
| J3 | 可选 git 自动提交、备份/导出、On this day | 1–2 天 |

总计 **1.5–2 周**。建议在 GA 1.0 核心稳定后立即做（v1.0.1 或 v1.1），优先级高于 Workbench。

---

## 14. 开放决策

| # | 问题 | 建议 |
|---|---|---|
| Q1 | SilverBullet vs Memos | SilverBullet（Markdown 优先、Journal 内置） |
| Q2 | 是否启用本地 git 自动提交 | 默认开启本地 git，不推远端 |
| Q3 | 远程图片是否复制到本地 | 默认复制，禁止远程追踪 |
| Q4 | loopback 是否加 token | 默认关闭；提供开关 |
| Q5 | 是否在 GA 1.0 内交付 | 建议 GA 后立即做，不阻塞 GA |
| Q6 | 来源胶囊是否 MVP 必需 | 建议 MVP 就带 frontmatter，正文可后补 |
| Q7 | Windows 是否随 v1.1 | 同步；SilverBullet 有预编译二进制 |
| Q8 | 日记默认目录 | `~/Library/Application Support/Qimingxing/wiki/` |

---

## 15. 与 Workbench 的关系

- 日记本是**成果归档与回顾层**，Workbench 是**空间化操作层**。
- 先做日记本；Workbench 可以后置，未来可把日记页作为卡片嵌入，但不再作为第一优先级。
- 两者共用 Markdown 事实来源与卡片/来源模型，不冲突。
