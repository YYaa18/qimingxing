# DeepSeek Chat Completions 能力适配

| 项目 | 内容 |
|---|---|
| 日期 | 2026-11-25 |
| 文档来源 | https://api-docs.deepseek.com/zh-cn/api/create-chat-completion/ |
| 适用模型 | `deepseek-flash`、`deepseek-v4-pro` |
| 传输协议 | OpenAI-Compatible `POST {base_url}/chat/completions` |

---

## 1. 模型与能力

| 模型 | 上下文 | 最大输出 | 输入模态 | 思考 | 原生联网工具 |
|---|---|---|---|---|---|
| `deepseek-flash` | 1,048,576 | 393,216 | 文本 + 图片 | 支持 | 支持 |
| `deepseek-v4-pro` | 1,048,576 | 393,216 | 文本 | 支持 | 官方指南建议使用 flash |

模型能力通过 `/models` 的 `context_window`、`max_output_tokens`、`input_modalities`、`effort` 自动发现，写入 `models.db`；`deepseek-flash` 的图片能力同时写入内置 catalog 兜底。

---

## 2. 请求字段适配

| API 字段 | 适配方式 |
|---|---|
| `messages` | 支持 system / user / assistant / tool；user 内容可为字符串或 `text` + `image_url` 块 |
| `latest_reminder` | DeepSeek 请求会在最后一条 user 前插入 `YYYY-MM-DD,星期X,本地,App,中文`，让模型知道当前日期；JSON 内部任务不插入 |
| `thinking` / `reasoning_effort` | 设置页「DeepSeek 能力 → 深度思考 / 思考强度」；`auto` 不发送、`enabled`/`disabled` 显式发送，effort 支持 `low/high/max`；`model_routing.extra_json.effort` 可按槽位覆盖全局值 |
| `reasoning_content` | 工具循环中完整回传每个带 `tool_calls` 的 assistant 消息；不回传会导致 DeepSeek 400 |
| `tools` | 保留函数工具；`deepseek-flash` + 原生联网开关 + 全局联网搜索开启时，追加 `deepseek_web_search`、`deepseek_read_url`，并移除客户端 `web_search` / `web_fetch` |
| `tool_choice` | 固定 `auto`；不在思考模式使用 `required` 或指定具体工具（API 限制） |
| `response_format` | 记忆提取、技能蒸馏等内部 JSON 任务使用 `json_object`；同时关闭思考，保证快任务速度 |
| `temperature` | 思考模式不发送（API 不生效）；明确关闭思考时才发送；其他 provider 维持原行为 |
| `max_tokens` | 不显式发送，使用 DeepSeek 默认：非思考 8K、思考 64K（max 128K）；上下文预算只预留 8K，但 1M 窗口不会因此溢出 |
| `stream` / `stream_options` | 始终流式；`include_usage=true`，最后一块读取完整 usage |
| `safety_check` | 设置页「内容安全校验」开关，开启时发送 `{"level":"high"}` |
| `user_id` | 默认 `qimingxing-desktop`，用于 KVCache/调度隔离；可在 YAML `deepseek.user_id` 覆盖 |
| `stop` / `logprobs` / `top_logprobs` / penalties | 未使用；penalties 官方已标记废弃，logprobs 当前没有 UI 消费场景 |
| `title` | 响应字段已捕获，但当前不请求、不消费（工作区标题仍由用户问题生成） |
| 文件块 `file_id` / `file_data` | 未使用原生文件块；PDF/Word/PPT 走本地文本提取，图片走 `image_url` |
| 前缀续写 `prefix` / FIM | Beta/独立接口，未启用 |
| Anthropic Messages / Responses API | 未启用，统一走 OpenAI-Compatible Chat Completions |

---

## 3. 响应字段适配

| API 字段 | 适配方式 |
|---|---|
| `content` | 流式输出到 Live Line / 工作区 |
| `reasoning_content` | 流式捕获；工具循环回传，不直接展示（保持 Quiet Native 的无黑框设计） |
| `tool_calls` | 函数工具循环，沿用原有审批/执行/轨迹链路 |
| `server_tool_calls` | 原生联网工具步骤；在同一轮工具循环里原样回传，保留页面 signature |
| `server_tool_event` | `call_started` / `call_completed` 转成 `output.tool_status`，界面显示「正在联网搜索 / 正在阅读网页」 |
| `annotations` | `url_citation` 去重后追加到回答末尾「参考来源」，最多 6 条，标题截断 90 字 |
| `refusal` | 无正文时转成可见的拒绝提示，避免空白回答 |
| `finish_reason` | `insufficient_system_resource` / `aborted` / `content_filter` / `length` 在空回答时给出中文提示 |
| `usage.prompt_cache_hit_tokens` | 已有缓存命中统计，用于上下文校准 |
| `usage.completion_tokens_details.reasoning_tokens` | 已解析并保留 |
| `usage.completion_tokens_details.safety_tokens` | 已解析并保留 |
| `title` | 已解析保留，暂无 UI |

---

## 4. 原生联网搜索

- 仅 `deepseek-flash` + 全局 `tools.web_search.enabled` + 设置页「原生联网搜索」开启时启用。
- 声明：
  ```json
  "tools": [
    {"type": "deepseek_web_search"},
    {"type": "deepseek_read_url"}
  ]
  ```
- 思考开启时是深度搜索（search / open_page / find_in_page），关闭时是普通搜索；两种模式都由 DeepSeek 服务端执行，不消耗客户端工具审批。
- 联网工具不计费，只有模型 token 计费。
- 响应引用在 `annotations`，正文没有角标；客户端在流结束后追加「参考来源」。
- 若回复使用了联网工具，官方限制不能继续该回复；当前应用按普通新回合处理。

---

## 5. 图片与多模态

- `deepseek-flash` 的 `input_modalities` 包含 `image`，模型能力写入 `models.db` 与 catalog。
- 屏幕截图/用户图片在 `profile.supports_vision=true` 时直接以 `image_url` data URL 发送；否则走 vision slot 或 DeepSeek OCR 文本兜底。
- 图片格式支持 JPEG/PNG/GIF/WebP；`detail` 未显式设置，使用服务端默认 `auto`。

---

## 6. 思考模式与工具循环

1. 默认 `auto`：不发送 `thinking`，API 默认开启思考、effort=high。
2. 显式 `enabled`：发送 `thinking.type=enabled`，可选 `reasoning_effort`。
3. 显式 `disabled`：发送 `thinking.type=disabled`，不发送 effort，temperature 生效。
4. 工具调用轮次：
   - assistant 消息保留 `content`、`reasoning_content`、`tool_calls`；
   - 如响应含 `server_tool_calls`，一并原样保留；
   - 下一轮请求完整回传，避免 400；
   - 不带 tools 的普通多轮不强制回传 reasoning_content。
5. 思考模式不支持 `tool_choice=required` 或指定具体工具，本应用固定 `auto`。

---

## 7. JSON 模式

- 记忆提取、滚动摘要、技能蒸馏等内部任务使用 `response_format={"type":"json_object"}`。
- 同时发送 `thinking.type=disabled`，减少延迟和 reasoning token。
- 提示词本身已要求只输出合法 JSON；解析器仍保留代码块清理和容错。

---

## 8. 缓存、用量与价格

- `prompt_cache_hit_tokens` 用于成本估算和上下文校准；`prompt_cache_miss_tokens` 可由 prompt 总量推导。
- `user_id` 固定为 `qimingxing-desktop`，让同一安装的 KVCache 稳定命中；多用户共用 Key 时可在 YAML 覆盖。
- 思考 token 计入 `completion_tokens`，额外解析到 `reasoning_tokens` 仅用于诊断。
- 成本估算按**高峰价**（保守）：
  - `deepseek-flash`：输入 2 元 / 缓存命中 0.04 元 / 输出 8 元（每 1M tokens）；
  - `deepseek-v4-pro`：输入 9 元 / 缓存命中 0.30 元 / 输出 27 元（每 1M tokens）；
  - 北京时间周一至周五 9:00–12:00、14:00–18:00 为高峰，其余空闲时段半价；实际账单以官方为准。
- 服务端联网搜索/网页抓取的工具调用免费，返回内容仍按模型 token 计费。
- 超时：DeepSeek 请求最少 120 秒（深度搜索/思考可能较慢）；SSE `: keep-alive` 注释会被忽略。
- 429 为账号级并发/速率超限；界面给出可读提示，由用户择机重试。

---

## 9. 错误处理

| HTTP | 提示 |
|---|---|
| 400 | 请求体/参数错误，透传精简后的上游信息 |
| 401 | API Key 无效或已失效 |
| 402 | 账户余额不足 |
| 408 / 504 | 请求超时，稍后重试 |
| 413 | 请求体/文件过大 |
| 422 | 参数有误 |
| 429 | 速率或并发超限 |
| 500 / 503 | 服务故障/繁忙，稍后重试 |

流式响应中 HTTP 200 但 `error` 负载也会被识别并抛出。

---

## 10. 未启用或暂不适配

- FIM 补全、Beta 前缀续写：当前没有对应产品入口，且需要 `base_url=/beta`。
- Anthropic Messages / Responses API：统一使用 Chat Completions，避免三套协议分叉。
- 原生文件块（`file_id` / `file_data`）：附件继续走本地文本提取 + 图片多模态，避免上传生命周期和隐私面扩大。
- `title`：API 可返回会话标题，但工作区目前按天聚合、标题取用户问题，暂不消费。
- `logprobs` / `stop` / `top_logprobs`：没有 UI 或产品需求，避免无意义参数。
- 频率/存在惩罚：官方已标记废弃，不发送。

---

## 11. 验证

- `tests/test_deepseek_adapter.py`：思考/reminder/JSON/工具引用/错误映射/非 DeepSeek 不泄漏。
- `tests/test_tools_engine.py`：工具循环回传 `reasoning_content` 与 `server_tool_calls`。
- `tests/test_settings_api.py`：DeepSeek 设置持久化与 0600。
- 真实 API 验证（使用本机已保存 Key）：
  - 带 reasoning_content 的函数工具二次请求 200；
  - 原生 `deepseek_web_search` + `deepseek_read_url` 返回 200 与 `url_citation`；
  - `response_format=json_object` 返回 200；
  - `latest_reminder` 格式 `2026-11-25,星期三,本地,App,中文` 返回 200。
