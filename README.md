# 启明星（Qimingxing）— MVP 1.0

通用 AI 桌面数字人助手（含 3A 游戏辅助）阶段一（MVP 1.0：协议与极简闭环）实现。

## 架构概览

采用**星型本地三层解耦架构**（`ws://127.0.0.1:18790/ws`）：

- `protocol/`：Pydantic v2 通信协议与信封模型（Single Source of Truth），支持一键导出 `protocol.schema.json`。
- `brain/`：FastAPI WebSocket 大脑服务，含多角色路由、多云端模型 Profile 热切换、方式 1（轻量标签流式状态机）解析与 `turn_id` 并发打断。
- `mock_senses/`：独立模拟感官 WebSocket 客户端，向大脑注入 `context.game_state` 与 `context.window_changed`。
- `body_web/`：零构建、零 Python 依赖的极简单文件 HTML/JS 躯体客户端。
- `tests/`：协议、流式状态机、多角色路由与并发打断自动化测试集。

## 快速开始

### 1. 安装依赖

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. 配置模型 Profile（可选）

默认内置 `mock_local` 本地模拟模型，无需 API Key 即可完整体验闭环。如需对接真实云端大模型：

```bash
cp config.example.yaml config.yaml
export DEEPSEEK_API_KEY="your-key"
export OPENAI_API_KEY="your-key"
export ANTHROPIC_API_KEY="your-key"
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

2. 打开躯体页面（Body）：浏览器访问 `http://127.0.0.1:18790/` 或直接打开 `body_web/index.html`。
3. （可选）启动独立模拟感官客户端（Mock Senses）：

```bash
python mock_senses/simulator.py --preset elden_ring
```

### 5. 运行自动化测试

```bash
pytest -v
```
