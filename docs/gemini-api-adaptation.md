# Gemini Native API 能力适配

| 项目 | 内容 |
|---|---|
| 日期 | 2026-11-26 |
| 文档来源 | https://ai.google.dev/api/generate-content 、https://ai.google.dev/gemini-api/docs/* |
| 适用模型 | Gemini 4 Carbon / Neon / Hydrogen / Argon，以及 Gemini 3.x |
| 传输协议 | 官方 Gemini 原生 `generateContent` / `streamGenerateContent` |

---

## 1. 为什么切到原生协议

此前 Gemini 走 OpenAI-Compatible 端点，只能覆盖文本、函数调用和基础多模态。原生 `generateContent` 额外提供：

- `thinkingConfig` / `thinkingLevel`
- `thoughtSignature`（Gemini 3/4 函数调用必需）
- `googleSearch` / `urlContext` 内置工具
- `groundingMetadata` 引用
- `promptFeedback`、完整 `finishReason`、`thoughtsTokenCount` / `cachedContentTokenCount`

因此官方 `generativelanguage.googleapis.com` 现在走原生协议；自定义 Gemini 代理仍走 OpenAI-Compatible，避免误判。

---

## 2. 模型与思考

| 模型 | 上下文 | 默认输出 | 思考级别 |
|---|---|---|---|
| `gemini-4-carbon` | 1,048,576 | 65,536 | low / medium / high |
| `gemini-4-neon` | 1,048,576 | 65,536 | low / medium / high / xhigh |
| `gemini-4-hydrogen` | 1,048,576 | 65,536 | low / medium / high / xhigh |
| `gemini-4-argon` | 1,048,576 | 65,536 | low / medium / high |

- Gemini 4 不能完全关闭思考；设置页只提供自动 / 低 / 中 / 高 / 最高。
- `thinkingLevel` 写入 `generationConfig.thinkingConfig`；`auto` 不发送，使用模型默认。
- `includeThoughts` 保持关闭，不把思考摘要塞进 Live Line；思考 token 仍通过 `thoughtsTokenCount` 计费与统计。
- `xhigh` 仅 Neon / Hydrogen 支持，其余模型自动回退 `high`。
- Gemini 2.5 不支持 `thinkingLevel`，请求会自动省略该配置。

---

## 3. 函数调用与 thoughtSignature

- OpenAI 风格函数工具转换为原生 `functionDeclarations`。
- 原生函数调用返回 `functionCall` + `thoughtSignature`；签名原样保存在 `ToolCallRequest.thought_signature`。
- 工具循环的 assistant 消息携带签名；下一轮 `contents` 中签名放回对应 `functionCall` part。
- Gemini 3/4 的函数调用签名是强制的；缺失会返回 `MISSING_THOUGHT_SIGNATURE`。
- 并行调用：签名只出现在第一个函数调用 part；后续调用不带签名。函数结果按 FC1、FC2、FR1、FR2 的顺序回传，不能交错。
- `functionResponse.response` 优先使用工具输出中的 JSON 对象；纯文本包成 `{"result": "..."}`。
- 已用真实 API 验证单工具与并行工具循环均可完成。

---

## 4. Google Search / URL Context

- 设置页「Gemini 能力」提供两个开关，默认关闭：
  - `Google 搜索` → `{"googleSearch": {}}`
  - `网页上下文` → `{"urlContext": {}}`
- 与函数工具同时开启时，自动加：
  ```json
  "toolConfig": {"includeServerSideToolInvocations": true}
  ```
  否则 Gemini 会返回 400（Built-in tools with Function calling）。
- `groundingMetadata.groundingChunks[].web` 提供 `uri` + `title`；客户端去重后追加「参考来源」，最多 6 条。
- `searchEntryPoint` 的 HTML 不注入原生界面。
- 计费：Gemini 3+ 每月共享 5,000 次免费检索，之后 $14 / 1,000 次；默认关闭以避免无意消耗。
- URL context 读取消息中的链接；已验证可读取官方文档并返回来源。

---

## 5. 多模态

- 用户 `image_url` data URL → 原生 `inlineData`（mimeType + base64）。
- HTTP(S) 图片 → `fileData`（fileUri）。
- Base64 音频 → `inlineData`（audio/*）。
- 已用真实 API 验证截图输入返回 200。
- 原生 Files API / `fileData` 上传生命周期未接入；PDF/Word 等继续走本地文本提取。

---

## 6. JSON 模式

- 内部记忆提取、摘要、技能蒸馏等任务设置：
  ```json
  "generationConfig": {"responseMimeType": "application/json"}
  ```
- 同时使用 `thinkingLevel=LOW`，降低延迟。
- 不发送 Google Search / URL Context / 函数工具，避免内部任务联网。
- 已用真实 API 验证返回纯 JSON。

---

## 7. 用量与价格

- `usageMetadata` 映射：
  - `promptTokenCount` + `toolUsePromptTokenCount` → 输入；
  - `candidatesTokenCount` + `thoughtsTokenCount` → 输出；
  - `cachedContentTokenCount` → 缓存命中；
  - `totalTokenCount` → 总用量。
- 成本估算按标准付费层、USD→CNY≈7.2：
  - Hydrogen：输入 10.8 / 命中 0.54 / 输出 54（元每 1M）；
  - Neon：21.6 / 1.08 / 108；
  - Carbon / Argon：28.8 / 1.44 / 144（按 2026-12-01 起的新价保守估算）。
- 思考 token 按输出计费；Google Search 检索费不计入 token 价格，设置页有提示。
- 缓存目前只使用隐式缓存；`cachedContentTokenCount` 已解析。

---

## 8. 错误、截断与安全

- `promptFeedback.blockReason` → 可见的拒绝提示。
- `finishReason` 映射：`MAX_TOKENS → length`，安全类 → `content_filter`，其余保留。
- 空回答时对 `SAFETY`、`BLOCKLIST`、`PROHIBITED_CONTENT`、`SPII`、`IMAGE_SAFETY`、`RECITATION`、`MISSING_THOUGHT_SIGNATURE` 给中文提示。
- `finishMessage` 在工具调用失败时追加到回答。
- Gemini 请求超时下限 120 秒。
- 默认使用 Google 安全阈值，未开放自定义 `safetySettings`。

---

## 9. 未启用

- Code Execution、Google Maps、File Search、Computer Use、MCP servers 等内置工具未开放；应用已有 shell/MCP/搜索工具，避免能力重叠。
- 显式上下文缓存（`cachedContent`）未接入；Gemini 4 当前以隐式缓存为主。
- `responseSchema` 未逐任务下发，只使用 `responseMimeType=application/json`。
- 长输出 `background=True` / `continuationToken` 未接入；当前回答上限内使用流式即可。
- 思考摘要不展示；保持 Quiet Native 的无黑框设计。

---

## 10. 验证

- `tests/test_gemini_native.py`：消息/签名转换、工具声明、grounding、JSON、用量、价格、原生工具循环。
- 真实 API 验证：
  - 原生文本流式 200；
  - 单工具与双工具并行调用 200；
  - Google Search grounding 200 + 来源；
  - URL Context 200 + 来源；
  - `responseMimeType=application/json` 200；
  - 图片 `inlineData` 200；
  - 25 个应用工具声明与 `googleSearch` 同时请求 200。
