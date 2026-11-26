# 启明星（Qimingxing）— v1.0.2

通用 AI 桌面数字人助手 — v1.0.2（原生 macOS SwiftUI 壳 + Python Brain + Router–Worker 调度 + 长期记忆/上下文工程 + 联网搜索与富媒体）。

## 架构概览

采用**星型本地三层解耦架构**（`ws://127.0.0.1:18790/ws`）：

- `protocol/`：Pydantic v2 通信协议与信封模型（Single Source of Truth），支持一键导出 `protocol.schema.json`。
- `brain/`：FastAPI WebSocket 大脑服务：
  - `context_builder.py`：Prefix-Cache 友好 4 层静动分层 Prompt 拼装（`L0 静态人设` → `L1 稳定画像` → `L2 滚动摘要/近期原文` → `L3 召回记忆/焦点/OCR`）、硬性 Token 预算分配与超长 OCR 头尾保留截断。
  - `memory/`：后端无关的三层长期记忆子包（`MemoryBackend` + 默认 `SQLiteMemoryStore` WAL/FTS5 + 动态分类注册表 `memory_categories` + 热/冷消息分离 `messages_archive`、`NullEmbeddingProvider` + `OpenAICompatibleEmbeddingProvider` RRF 混合留槽、`MemoryExtractor` 异步批量/口令提炼与 `ADD/UPDATE/DELETE/NONE` 冲突消解、日常闲聊分级沉淀、会话滚动压缩、脱敏桌面足迹沉淀、运维 `cli.py`）。
  - `hub.py`、`artifact_store.py`、`context_store.py`、`intent_detector.py`、`screen_router.py`、`stream_parser.py`、`llm_router.py`。
  - `worker.py`、`watchdog.py`、`task_runner.py`：Router/Worker 物理隔离、Capability Packs、租约锁、运行中追问、两级 Watchdog 与断点恢复。
  - `doc_office.py`、`tools/doc_tools.py`：PDF/Excel/CSV/Word/Markdown 结构读取、确定性表格加工、PDF 拆分合并与 XLSX/DOCX/PDF 生成。
  - `rich_content.py`、`media_cache.py`、`tools/image_search.py`：结构化来源/媒体元数据、Tavily 图片搜索、受限 HTTPS 下载与持久媒体缓存。
- `senses/`：跨平台真实桌面感官客户端（macOS / Windows 双平台 `WindowProvider` 极薄隔离 + `PrivacyGuard` 三档隐私模式与敏感黑名单 + `ScreenCapture` 跨平台截屏压缩上传）。
- `mock_senses/`：离线模拟感官客户端（供无桌面环境或单测使用）。
- `body_web/`：零构建三文件前端（`index.html`、`app.js`、`styles.css`），阶段三零前端改动。
- `shell_macos/`：原生 Swift 6 壳，包含 Quick Entry、Live Line、工作区/媒体预览双宿主、设置中心、审批与菜单栏。
- `tests/`：全量自动化测试集（225 项用例，覆盖上下文、记忆、联网、图片搜索、富媒体、Router–Worker 调度、文档/表格/PDF 处理与壳侧模型/缓存约束）。

## 快速开始

### 1. 安装依赖（以 `pyproject.toml` 为单一事实来源）

```bash
uv venv --python 3.12 .venv
source .venv/bin/activate
uv pip install -e ".[dev]"
```

### 2. 配置模型、记忆与隐私选项（可选）

```bash
cp config.example.yaml config.yaml
export DEEPSEEK_API_KEY="your-key"
```

### 3. 导出协议 Schema

```bash
python -m protocol.export_schema
```

### 4. 一键启动与关闭（推荐）

```bash
# 一键后台启动 Brain + 桌面感官 Senses，并自动在浏览器打开 http://127.0.0.1:18790/
./qimingxing.sh start

# 查看运行状态 / 实时日志 / 重启 / 一键关闭
./qimingxing.sh status
./qimingxing.sh logs
./qimingxing.sh restart
./qimingxing.sh stop
```

如需分终端手动启动：

1. 启动大脑（Brain）：

```bash
python -m brain.main
```

2. 启动跨平台桌面感官客户端（Senses）：

```bash
python -m senses.main
```

3. 浏览器访问 `http://127.0.0.1:18790/` 打开躯体页面。

### 5. 长期记忆审计与运维 CLI

```bash
python -m brain.memory.cli list --type facts
python -m brain.memory.cli search "Python"
python -m brain.memory.cli inspect <id>
python -m brain.memory.cli delete <id>
python -m brain.memory.cli export backup.jsonl
python -m brain.memory.cli import backup.jsonl
python -m brain.memory.cli reindex
python -m brain.memory.cli categories
python -m brain.memory.cli archive --keep 10000 --batch 2000

# 回环只读调试接口
curl http://127.0.0.1:18790/api/memory/debug
```

### 6. 运行自动化测试

```bash
pytest -v
```
