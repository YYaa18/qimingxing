# 启明星（Qimingxing）— MVP 2.0

通用 AI 桌面数字人助手 — 阶段二（MVP 2.0：日常自然对话 + 用户授权下的跨平台桌面感知）实现。

## 架构概览

采用**星型本地三层解耦架构**（`ws://127.0.0.1:18790/ws`）：

- `protocol/`：Pydantic v2 通信协议与信封模型（Single Source of Truth），支持一键导出 `protocol.schema.json`。
- `brain/`：FastAPI WebSocket 大脑服务，含 `hub.py`（轻量编排）、`artifact_store.py`（本地回环临时图片存取与用完即焚）、`history_store.py`（`MemoryStore` 抽象接口 + 滑动窗口）、`context_store.py`（30 分钟合并桌面活动时间线）、`intent_detector.py`（双阶段看屏意图判定）、`screen_router.py`（Vision 直看 / DeepSeek OCR 双通道）、`stream_parser.py`（方式 1 轻量标签流式解析）与 `llm_router.py`（多 Profile 热切换与 `turn_id` 取消）。
- `senses/`：跨平台真实桌面感官客户端（macOS / Windows 双平台 `WindowProvider` 极薄隔离 + `PrivacyGuard` 三档隐私模式与敏感黑名单 + `ScreenCapture` 跨平台截屏压缩上传）。
- `mock_senses/`：离线模拟感官客户端（供无桌面环境或单测使用）。
- `body_web/`：零构建三文件前端（`index.html`、`app.js`、`styles.css`），含三档隐私开关、窗口感知预览、看屏提示与附带屏幕提问按钮。
- `tests/`：阶段一 + 阶段二全量自动化测试集（27 项用例）。

## 快速开始

### 1. 安装依赖（以 `pyproject.toml` 为单一事实来源）

```bash
uv venv --python 3.12 .venv
source .venv/bin/activate
uv pip install -e ".[dev]"
```

### 2. 配置模型与隐私选项（可选）

```bash
cp config.example.yaml config.yaml
export DEEPSEEK_API_KEY="your-key"
```

### 3. 导出协议 Schema

```bash
python -m protocol.export_schema
```

### 4. 启动服务与联调

1. 启动大脑（Brain）：

```bash
python -m brain.main
```

2. 启动跨平台桌面感官客户端（Senses）：

```bash
python -m senses.main
```

3. 浏览器访问 `http://127.0.0.1:18790/` 打开躯体页面（默认处于 `仅窗口` 模式，切换至 `按需看屏` 后即可自然语言触发看屏）。

### 5. 运行自动化测试

```bash
pytest -v
```
