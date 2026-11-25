# 启明星：上下文管理与模型能力改造方案

- 状态：Phase A/B 后端已实施（2026-11-25），完整首次配置向导待补
- 日期：2026-11-25
- 范围：Brain 上下文拼装、压缩策略、模型配置存储、原生设置页
- 目标：解决“上下文太紧、模型信息过旧、模型能力不可配”的问题

---

## 1. 结论摘要

当前上下文管理是**固定小预算 + 按消息条数压缩**：

- 总预算固定 6000 tokens；
- 最近消息 2000 tokens；
- 滚动摘要 prompt 要求 220 字、代码上限 300 字；
- 未压缩消息达到 20 条（约 10 轮）才压缩，每次压 12 条（6 轮）；
- L0 系统提示本身约 1143 tokens，占掉近 1/5 总预算。

本机实测（2026-11-25）：

| 项 | 实际值 |
|---|---:|
| 当前会话消息 | 110 条 |
| 滚动摘要 | 295 字 / 约 264 tokens |
| 摘要水位 | msg_id 96（前 96 条只靠 295 字摘要） |
| 水位后最近消息 | 14 条 / 1671 tokens |
| L0 系统提示 | 1143 tokens |
| 活跃长期事实 | 9 条 / 879 tokens（L1 预算只允许 400） |

结论：对“桌面编码/任务型 Agent”来说，**6000 确实偏紧**，且压缩粒度过粗，容易丢失文件、代码决策和待办细节。

改造方向：

1. 模型能力（context window / max output / tools / vision / cache）进 SQLite，不再依赖 YAML；
2. DeepSeek、Gemini 优先通过厂商 API 自动发现模型与上下文，OpenAI-Compatible/未知模型才手动询问；
3. 上下文预算从固定 6000 改成“按当前模型窗口推导”；
4. 压缩触发从“按轮数”改为“按未压缩 token”，摘要结构化并扩长；
5. 给 L3 技能/附件与工具循环补 token 上限。

---

## 2. 当前实现

### 2.1 每轮拼装流程

入口：`brain/hub.py:2150` 附近。

```text
1. 取当前 active session 与 rolling_summary
2. 取摘要水位之后最近 20 条消息（limit=20）
3. 取最多 30 条 active 长期事实
4. 检索相关记忆 top_k=5
5. 召回相关技能
6. ContextBuilder.build 拼 L0-L3
7. 调 LLM
```

关键代码：

- 最近消息查询：`brain/memory/sqlite_store.py:425`（`after_msg_id=last_summarized_msg_id`，`limit=20`）
- 事实与召回：`brain/hub.py:2170`、`:2182`
- 拼装调用：`brain/hub.py:2194`

### 2.2 四层结构与预算

定义：`brain/config.py:217`

| 层 | 内容 | 预算 |
|---|---|---:|
| L0 | 静态系统提示 | 无单独上限，实测 1143 |
| L1 | 用户画像/长期事实 | 400 |
| L2 | 滚动摘要 | 350 |
| L2 | 最近原始消息 | 2000 |
| L3 | 记忆召回 | 500 |
| L3 | 桌面/OCR | 1500 |
| 总 | 全部消息 | 6000 |

实现：`brain/context_builder.py:148` 起。

- L0 + L1 组成静态 system prefix，并计算 hash，方便 Prefix Cache：`context_builder.py:145`
- L2 摘要作为 system message：`:159`
- L2 最近消息从新到旧装入 2000 预算：`:171`
- L3 召回/技能/附件/桌面上下文拼进当前 user message：`:183`
- 总预算超出时，只从最旧的 L2 最近消息开始丢：`:245`

Token 估算不是真正 tokenizer：

```text
中文 1.3 tokens/字 + 非中文 0.35 tokens/字 + 每条消息 4
图片固定 256 tokens
```

实现：`brain/context_builder.py:31`

### 2.3 压缩机制

实现：`brain/memory/extractor.py:458`。

```text
触发：未压缩消息 >= compress_trigger_turns * 2
默认：10 * 2 = 20 条

每次：最早 compress_batch_turns * 2 条
默认：6 * 2 = 12 条
```

- 摘要 prompt 要求不超过 220 字，代码硬截断 300 字：`extractor.py:68`、`:494`
- 水位推进到本批最后一条 msg_id：`:497`
- 每轮结束后异步触发，每轮最多压一批：`extractor.py:220`
- 压缩与记忆提取共用快模型 `@slot:fast_utility` / `fast_deepseek`：`extractor.py:293`
- 如果 `memory.extraction.enabled=false`，压缩也不会运行：`extractor.py:197`

稳态：

```text
20 条未压缩 → 压 12 条 → 剩 8 条
之后每轮 +2 条，约 6 轮后再次达到 20 条
```

### 2.4 会话与保留

- 一个 active session 会跨应用重启/跨天持续；清空历史时才新建 session。
- 摘要与水位存在 `sessions.rolling_summary` / `sessions.last_summarized_msg_id`。
- 消息热层保留 10000 条，超过后归档到 `messages_archive`；与上下文无直接关系。
- 会话生命周期：`brain/memory/sqlite_store.py:303`、`:328`

### 2.5 当前问题

1. **触发按条数，不按 token**：20 条长回答和 20 条短消息触发一样。
2. **最近 20 条实际到不了 20 条**：2000 token 会在 12–14 条左右截断。
3. **摘要太短**：295 字覆盖 96 条消息，技术细节基本丢失。
4. **L3 无完整上限**：技能和附件没有独立 token 预算；总超限时只丢 recent。
5. **工具循环不受 6000 约束**：`ToolExecutor` 持续追加工具结果，没有裁剪。
6. **模型配置过旧**：默认 deepseek-chat / deepseek-reasoner 已退役；没有按模型窗口计算预算。

---

## 3. 外部产品调研

| 产品 | 做法 |
|---|---|
| **Cline** | 每模型 Model Configuration：Context Window、Max Output、图片、Computer Use、价格；自定义 OpenAI-Compatible 提供 32K/64K/128K/200K/512K/1M 预设，1K–10M 校验。 |
| **Roo Code** | 每模型 Context Window / Max Output / 能力/价格；上下文压缩阈值可按 profile 单独设置；自定义 OpenAI-Compatible。 |
| **Continue** | YAML 每模型 `defaultCompletionOptions.contextLength` / `maxTokens`；自定义端点用 `provider: openai + apiBase`，显式声明 `capabilities: [tool_use]`。 |
| **Cherry Studio** | 内置模型目录 + 用户覆盖；自定义模型编辑抽屉手填 context window / max output；曾有“未知模型 context 为空”的 bug。 |
| **LibreChat** | `modelSpecs.maxContextTokens`、自定义端点 `tokenConfig.<model>.context`；超限做 sliding window。 |
| **Chatbox** | 每模型 Context Window、Max Output、Vision/Reasoning/Tool use；留空才用默认。 |

共同结论：

1. 上下文窗口是**每模型**配置，不是每厂商。
2. Context Window 与 Max Output 分开。
3. 已知模型自动带默认值，未知模型必须问用户。
4. 不能静默写死 128K；必须持久化用户覆盖。
5. 模型切换应同时切换 API 配置、能力和预算。

---

## 4. 最新模型与厂商 API

> 模型名称和规格会变化，以下以 2026-11-25 调研为准；实现时以**实时 API 返回**为准，目录只做兜底。

### 4.1 DeepSeek

- 当前主线：DeepSeek V4 Flash / V4 Pro，官方标称 **1M context / 384K output**。
- 旧 `deepseek-chat` / `deepseek-reasoner` 已于 2026-07-24 退役。
- `GET /models` 返回：
  - `id`、`name`
  - `context_window`
  - `max_output_tokens`
  - `input_modalities` / `output_modalities`
  - `effort`（thinking/non-thinking）
  - `api_capabilities`

**可以自动获取上下文和最大输出，不需要用户手填。**

### 4.2 Google Gemini

- 当前主线：Gemini 4 系列（Carbon / Neon / Hydrogen / Argon），主流为 **1,048,576 input / 65,536 output**；Hydrogen 为通用对话默认档。
- 原生 `GET /v1beta/models`（`x-goog-api-key` Header，不使用 URL Query）返回：
  - `name` / `displayName`
  - `inputTokenLimit`
  - `outputTokenLimit`
  - `supportedGenerationMethods`
- 过滤 `generateContent` 可得到聊天模型；`-preview` 模型需要提示弃用风险。

**可以自动获取上下文和最大输出。**

### 4.3 OpenAI-Compatible / 自定义

- `GET /models` 通常只返回模型 ID，没有上下文长度。
- 需要：内置目录 + 用户手动填写。
- Ollama 可用 `/api/show` 的 `model_info` 读取 context length，作为补充。

### 4.4 结论

```text
DeepSeek / Gemini：API 自动发现优先
OpenAI-Compatible / 未知模型：手动询问上下文
所有模型：允许用户覆盖，source 标记来源
```

---

## 5. 模型配置进 SQLite

### 5.1 为什么不再用 YAML

- 模型配置是用户高频维护的数据，不是静态运行参数；
- Provider/Key/Model/能力/路由需要事务和查询；
- 首次配置、刷新模型、切换模型都应由 UI 驱动；
- 用户不需要接触 YAML。

**范围约定：模型相关全部进 SQLite；端口、记忆、工具等非模型运行配置暂时保留 YAML。**

### 5.2 数据库

建议独立数据库，避免和对话记忆混在一起：

```text
~/Library/Application Support/Qimingxing/models.db
```

权限：`0600`（含 `-wal` / `-shm`）。

### 5.3 表结构

```sql
CREATE TABLE providers (
    provider_id    TEXT PRIMARY KEY,
    label          TEXT NOT NULL,
    protocol       TEXT NOT NULL DEFAULT 'openai_compatible',
    base_url       TEXT NOT NULL DEFAULT '',
    api_key        TEXT NOT NULL DEFAULT '',
    auth_required  INTEGER NOT NULL DEFAULT 1,
    enabled        INTEGER NOT NULL DEFAULT 1,
    is_builtin     INTEGER NOT NULL DEFAULT 0,
    created_at     REAL NOT NULL,
    updated_at     REAL NOT NULL
);

CREATE TABLE provider_models (
    provider_id        TEXT NOT NULL,
    model_id           TEXT NOT NULL,
    label              TEXT NOT NULL DEFAULT '',
    context_window     INTEGER NOT NULL DEFAULT 0,
    max_output_tokens  INTEGER NOT NULL DEFAULT 0,
    supports_tools     INTEGER NOT NULL DEFAULT 0,
    supports_vision    INTEGER NOT NULL DEFAULT 0,
    supports_cache     INTEGER NOT NULL DEFAULT 0,
    supports_reasoning INTEGER NOT NULL DEFAULT 0,
    source             TEXT NOT NULL DEFAULT 'manual',  -- provider_api | manual | catalog | default
    metadata_json      TEXT,
    last_synced_at     REAL,
    enabled            INTEGER NOT NULL DEFAULT 1,
    created_at         REAL NOT NULL,
    updated_at         REAL NOT NULL,
    PRIMARY KEY (provider_id, model_id),
    FOREIGN KEY (provider_id) REFERENCES providers(provider_id) ON DELETE CASCADE
);

-- 已拍板：不新增 profiles 实体表，槽位扩展参数放在 model_routing.extra_json。
CREATE TABLE model_routing (
    slot        TEXT PRIMARY KEY,  -- chat / fast_utility / vision_ocr / tts / asr
    provider_id TEXT NOT NULL,
    model_id    TEXT NOT NULL,
    voice       TEXT NOT NULL DEFAULT '',
    extra_json  TEXT NOT NULL DEFAULT '{}',  -- temperature / effort / max_output_override 等
    updated_at  REAL NOT NULL,
    FOREIGN KEY (provider_id) REFERENCES providers(provider_id) ON DELETE RESTRICT
);

CREATE TABLE meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
```

实现约定：`brain/model_config_store.py` 独立管理 `models.db`，并加入全仓 sqlite3 AST 守卫测试白名单。

### 5.4 默认种子

首次启动只写厂商模板和模型目录兜底，不写 Key、不自动选模型：

```text
DeepSeek
  GET /models 自动发现（V4 Flash / Pro）
  catalog 兜底：1M / 384K

Google Gemini
  GET /v1beta/models 自动发现（Gemini 4 系列）
  catalog 兜底：1,048,576 / 65,536

OpenAI / 兼容接口
  空模板：用户填 Base URL、Key、模型 ID
  兼容 OpenAI、OpenRouter、LM Studio、Ollama、中转站
```

### 5.5 模型发现适配器

| Provider | 接口 | 能拿到 |
|---|---|---|
| DeepSeek | `GET {base}/models` | id、context_window、max_output_tokens、modalities、effort、api_capabilities |
| Gemini | `GET https://generativelanguage.googleapis.com/v1beta/models`，鉴权走 `x-goog-api-key` Header | name、displayName、inputTokenLimit、outputTokenLimit、supportedGenerationMethods |
| OpenAI-Compatible | `GET {base}/models` | 通常只有 ID |
| Anthropic | `GET {base}/v1/models` | ID / display_name（上下文需目录） |
| Ollama | `/api/tags` + `/api/show` | 模型列表；context length |

### 5.6 合并与覆盖规则

```text
优先级：用户手动覆盖 > 厂商 API 自动同步 > 内置目录 > 保守默认 32K / 4K
```

- `source=manual` 的 context/output 不被自动同步覆盖；
- 自动同步只 upsert 厂商返回的模型；
- 厂商不再返回的模型标记 `enabled=0`，不删除，避免 profile 外键断；
- UI 提示“模型已下线，请重新选择”；
- `metadata_json` 保留原始返回，方便后续增加能力字段。

### 5.7 迁移

现有 `config.yaml` 中的 providers / profiles / model_routing / Key 不能丢：

1. 启动时如果 `models.db` 为空，读取 YAML 模型段；
2. 连同 API Key 一次性写入 SQLite；
3. 写 `meta.migrated_from_yaml=1`；
4. 备份为 `config.yaml.bak-models`；
5. 成功后 SQLite 为唯一事实源，YAML 模型段不再读取；
6. 失败则保留 YAML 回退。

迁移后，旧 `deepseek-chat` / `deepseek-reasoner` 如果不在 `/models` 返回中，标记不可用，不自动切换模型。

### 5.8 安全

- `models.db` / `-wal` / `-shm` 全部 `0600`；
- API Key 第一版仍为本地明文（与现在 config.yaml 一致），但文件权限收紧；
- 后续可迁移到 macOS Keychain，SQLite 只存引用；
- 暂不上 Keychain：当前是 ad-hoc 签名，Keychain 迁移会频繁触发密码授权；
- 首次填 Key 自动同步一次模型列表，之后仅由用户点击「刷新模型」触发。

---

## 6. 首次配置与设置页

### 6.1 默认状态

- 不预置可用 Key，不自动选中模型；
- Quick Entry 首次打开显示“先连接一个模型”；
- 没有可用模型时不发起对话请求，避免 401/空 profile。

### 6.2 三张厂商卡

```text
[ DeepSeek ]      [ Google Gemini ]      [ OpenAI / 兼容接口 ]
官方接口           官方 OpenAI 兼容端点      通用 OpenAI-Compatible
```

### 6.3 首次配置流程

```text
1. 选厂商
2. 填 API Key / Base URL（预填）
3. 点击「获取模型」
   DeepSeek → 自动列出 V4 Flash/Pro + 上下文/输出
   Gemini   → 自动列出 Gemini 4 系列 + 上下文/输出
   OpenAI 兼容 → 只列 ID，上下文留空
4. 选择模型
   有元数据：直接显示，允许覆盖
   无元数据：弹窗询问上下文窗口（预设 32K/64K/128K/200K/512K/1M）
5. 测试连接
6. 设为默认，进入 Quick Entry
```

### 6.4 模型编辑抽屉

```text
模型 ID        [deepseek-flash]
显示名称       [DeepSeek V4 Flash]
上下文窗口     [1048576]
最大输出       [393216]
能力           [x] 工具  [ ] 视觉  [x] 缓存  [x] Reasoning
来源           厂商 API · 2026-11-25 同步
预算预览       输入预算 24K（15%，上限 24K）
[刷新模型] [手动覆盖] [保存]
```

- 校验：1K–10M；Max Output 必须小于 Context Window。
- 预算预览让用户理解“窗口会换来多少上下文”。

---

## 7. 动态上下文预算（定稿）

### 7.1 公式

```text
request_max_tokens = 8192（默认，可在设置中调低）
reserved_output    = min(max_output_tokens or request_max_tokens, request_max_tokens)
tier_cap           = 16000 | 32000 | 64000（默认 32000，均衡档）
raw_target         = clamp(window * 0.25, 8000, tier_cap)
total_input        = max(4096, min(raw_target, window - reserved_output - 2048))
l0_tools           = L0 系统提示 + 当前挂载 tools JSON Schema
available          = max(512, total_input - l0_tools)
```

关键修正：

- `max_output_tokens == context_window`（部分中转站/Ollama 会这样上报）时，`reserved_output` 被 `request_max_tokens` 截住，不会出现负预算；
- 24K 固定上限取消，改为 16K / 32K / 64K 三档，默认 32K；
- 1M 窗口默认拿到 32K 输入预算，不再被 24K 卡死；
- L0 与 tools schema 是刚性底座，必须先扣除，不能等拼完再靠丢 recent 兜底。

### 7.2 保底刚需 + 弹性分配

| 层级 | 规则 | 说明 |
|---|---|---|
| **L0 + Tools Schema** | 刚性实算，不截断 | 系统提示 + 已激活工具 Schema 的真实占用 |
| **L1 长期事实** | 保底 600，上限 1200 | 当前 9 条活跃事实 879 tokens 不再被 400 腰斩 |
| **L2 滚动摘要** | 保底 1000，上限 1500 | 与 800–1200 字结构化摘要对齐 |
| **L3 动态上下文** | 技能 ≤1000；附件 ≤2000；OCR ≤2000；记忆召回 ≤800；总占用 ≤ total_input × 35% | 空项不占额度；按实际内容动态占用 |
| **L2 最近原始消息** | `available - 以上实占`，保底目标 3000 | 剩余全部给连续原始对话 |

预算不足时的降级顺序：

```text
先压 L3 → 再把摘要/L1 降到保底 → 最后才让 recent 低于 3000
```

### 7.3 示例

| 模型窗口 | reserved_output | tier_cap | total_input |
|---:|---:|---:|---:|
| 32K | 8192 | 32K | 8K |
| 64K | 8192 | 32K | 16K |
| 128K（DeepSeek） | 8192 | 32K | 32K |
| 1M（Gemini / DeepSeek V4） | 8192 / 393216 | 32K | 32K |
| 1M，用户选 64K 档 | 8192 | 64K | 64K |

> `reserved_output` 取 `min(max_output, request_max_tokens)`；即使模型最大输出是 384K，实际请求只预留 8K，输入预算不会被输出上限吃掉。

### 7.4 工具 Schema 计量

`ContextBuilder` 不直接持有工具定义，由 hub 在每轮：

```text
tools_schema = tool_registry.to_openai_tools()
tools_tokens = estimate_tools_tokens(tools_schema)
resolved = resolve_context_budget(profile, ..., tools_tokens)
```

纯聊天分支不使用工具时也按保守值扣除，避免“看起来没超，实际请求超”。

### 7.5 Token 估算校准

保留轻量启发式估算做本地兜底，但每次 LLM 返回后：

```text
ratio = usage.prompt_tokens / estimated_prompt_tokens
scale = clamp(0.7 * scale + 0.3 * ratio, 0.6, 1.8)
```

- 校准只影响后续预算计数，不改变模型窗口；
- 对代码/JSON 场景的估算偏差会逐步收敛；
- 若后续引入 `tiktoken`，替换 `TokenMeter` 实现即可。

### 7.6 接入点

- `hub._run_turn` 已拿到当前 `profile`：`brain/hub.py:1957`；
- 每轮解析 effective budget，传入 `ContextBuilder`；
- 切换模型后下一轮自动使用新预算；
- `BuildStats` 记录 `total_input / l0_tools / recent_budget / tools_tokens / calibration_scale`，便于调试。

---

## 8. 压缩与工具上限（定稿）

### 8.1 高低水位滞回

```text
高水位 = l2_recent_budget × 1.0
低水位 = l2_recent_budget × 0.55
```

- 未压缩 token 达到高水位才触发；
- 触发后循环压缩，直到回落到低水位，或达到单轮最大批次数（默认 4）；
- 两批之间摘要水位推进，L0 + L1 保持不变，连续多轮内 L2 摘要字节稳定，最大化 Prefix Cache 命中；
- 压缩异步执行，不阻塞当前回答。

同时：

- `sqlite_store.get_messages` 的最近消息拉取上限从 20 放宽到 **100**；具体取多少由 recent token 预算决定；
- 滚动压缩与 `memory.extraction.enabled` **解耦**，新增独立 `compress_enabled`（默认 true）；
- `memory.extraction.enabled=false` 只停事实/情景提取，不影响滚动压缩。

### 8.2 结构化摘要

- 目标 800–1200 字，硬上限 1600 字；
- 固定 5 段：
  - 已完成/已确认
  - 关键文件与路径
  - 技术决策与原因
  - 待办/未解决问题
  - 用户偏好与约束
- Prompt 明确要求：**不粘贴大段历史代码**，只保留精确文件路径、函数/类名、报错码、已确认决策；具体代码需要时用工具重新读取最新文件。

### 8.3 L3 与工具循环

L3：

- 技能 ≤1000；
- 附件/挂载清单 ≤2000；
- 桌面/OCR ≤2000；
- 记忆召回 ≤800；
- 总 L3 ≤ total_input × 35%；
- 超限时先压技能/附件，再压 OCR，最后压召回。

工具循环：

```text
tool_loop_max = min(32000, window - reserved_output - current_prompt_tokens)
```

- 单次工具输出头尾截断，保留 6K–8K；
- 循环内超预算时，保留最近 2–3 步工具完整输出；
- 更早的超长 tool 消息替换为一行折叠标记，例如：
  `[第 2 步 shell_exec 输出已折叠，共 5400 字符]`
- 不在循环中调用 LLM 做摘要，零额外延迟，不破坏当前 KV 缓存。

### 8.4 验收指标

- 长对话 30+ 轮后，最近原始消息仍能保持在 3000 tokens 以上；
- 压缩触发后，L0+L1 前缀 hash 不变；
- 1M 模型默认输入预算 32K；切到 64K 档后为 64K；
- 工具连续 6 步、单步输出 10K 时，请求不因上下文超限 400。

---

## 9. 分两期实施

### Phase A：上下文引擎核心改造（先做）

**目标**：不改模型存储，先在现有配置对象上让上下文明显舒展。

涉及文件：

- `brain/config.py`
- `brain/context_builder.py`
- `brain/context_budget.py`（新增）
- `brain/hub.py`
- `brain/memory/sqlite_store.py`
- `brain/memory/extractor.py`
- `brain/tools/executor.py`

交付：

1. `ProfileConfig` 增加 `context_window` / `max_output_tokens` / `request_max_tokens`，先用内置目录 + 配置覆盖；
2. 新增预算解析器：三档 16K/32K/64K、保底+弹性分配、L0+tools 计量、输出预留；
3. `get_messages` 放宽到 100；高低水位滞回压缩；结构化摘要 800–1200 字；与 extraction 解耦；
4. L3 分项上限 + 工具循环动态上限 + 旧观察折叠；
5. `usage.prompt_tokens` 校准。

**验收**：长对话上下文显著舒展；Prefix Cache 稳定；工具长输出不顶爆；现有 128 单测 + 18/18 金标通过。

### Phase A 实施记录（2026-11-25）

已落地：

- `ProfileConfig` 增加 `context_window` / `max_output_tokens` / `request_max_tokens` / `capability_source`；无显式值时用 `brain/model_catalog.py` 的 DeepSeek/Gemini 目录兜底。
- 新增 `brain/context_budget.py`：三档 16K/32K/64K、输出预留、L0+tools 计量、L1/summary/recent/L3 的 floor+cap 分配。
- `ContextBuilder` 支持 resolved budget、工具 token 扣除、L3 总占比裁剪、recent 余量分配、`usage.prompt_tokens` 校准。
- `get_messages` 拉取上限改为 100；压缩改为 token 高低水位滞回（无动态预算时保留旧 turn-count 兼容路径）；`compress_enabled` 与 extraction 解耦；摘要改为 800–1200 字/5 段结构。
- `ToolExecutor` 增加动态上下文上限、单次工具输出头尾截断、旧工具观察折叠。
- `/api/memory/debug` 新增 `last_build_stats`。

本机实测（现有会话）：

```json
{
  "resolved_total_input": 32000,
  "l0_tokens": 1143,
  "tools_tokens": 4938,
  "l1_tokens": 377,
  "l2_summary_tokens": 284,
  "l2_recent_tokens": 1671,
  "recent_budget": 23931,
  "total_with_tools": 9098,
  "calibration_scale": 1.0
}
```

说明：旧 `config.yaml` 的 6000 预算段在加载时自动切到新默认值（不改写用户文件）；用户可通过后续设置页/新字段调整。

`pytest`：128 passed。Phase B 尚未开始。

### Phase B：models.db + 自动发现 + 设置页

**目标**：把模型配置从 YAML 迁到 SQLite，首次配置向导可用。

涉及文件：

- `brain/model_config_store.py`（新增）
- `brain/model_discovery.py`（新增）
- `brain/main.py`
- `SettingsWindowController.swift`
- `Models.swift`

交付：

1. `models.db`（0600、外键修正、`extra_json`、YAML 无损迁移与备份）；
2. DeepSeek / Gemini（`x-goog-api-key` Header）/ OpenAI-Compatible / Ollama 模型发现；
3. 首次配置向导、模型编辑抽屉（含预算预览）、退役模型一键迁移提示；
4. `model_config_store.py` 加入 sqlite3 AST 守卫白名单。

**验收**：迁移不丢 Key；DeepSeek/Gemini 自动拿到上下文；Offline/未知模型手动问；全量测试通过。

### Phase B 实施记录（2026-11-25）

已落地（v1）：

- 独立 `models.db`（0600）：`providers` / `provider_models` / `model_routing` / `meta` 四张表；启动自动建表、种子与 YAML 迁移，旧 `deepseek-chat` / `deepseek-reasoner` 标记 `enabled=0`。
- 模型能力在 `provider_models` 中按模型存储，字段包含 context/max output/tools/vision/cache/reasoning/source/metadata/last_synced_at。
- 模型发现：
  - DeepSeek `/models` 解析 context/output/modalities；
  - Gemini 原生 `/v1beta/models`，只发送 `x-goog-api-key` Header；同一 Key 不能再作为 `Authorization: Bearer` 发送，否则 Google 会按 OAuth 校验并返回 401。过滤 `generateContent`；
  - OpenAI-Compatible 只取 ID，能力用目录/手动兜底；
  - Ollama `/api/tags` 基础支持。
- 设置 API 的模型部分切到 `models.db`：providers/model_routing 保存到 SQLite，非模型设置继续写 YAML；发现接口 `POST /api/models/discover`。
- 设置页：默认只保留 DeepSeek / Google Gemini / OpenAI 三家；服务商卡片内可编辑 Base URL、API Key，展开编辑每模型上下文/输出/工具/视觉，并提供「刷新模型」「测连通」；测试/刷新结果直接显示在对应卡片内，输入未保存的 Key 显示「待保存」；刷新设置页时会保留未保存的 Key。支持添加 OpenAI 兼容或 Anthropic 协议的自定义厂商，自定义厂商可移除（清除 Key 与路由，二次确认后随保存生效）；首次无 Key 时显示「开始使用」提示；Quick Entry 无可用 profile 时显示「配置模型」入口。
- 内置模型种子与实时目录对齐：DeepSeek 为 V4 Flash / V4 Pro，Gemini 为 4 Carbon / 4 Neon / 4 Hydrogen / 4 Argon；启动时只清理本产品写入的过期 catalog 种子，发现结果、迁移结果和手动编辑的模型不删除。
- 能力路由的模型改为按槽位过滤的下拉选择：对话/后台快任务排除 ASR、OCR、TTS、embedding、图像模型；看屏/OCR 允许 OCR/视觉模型；语音识别、语音合成只列对应模型；切换厂商时自动选择兼容默认模型，不再需要手输模型 ID。
- DeepSeek 专属能力（思考模式、reasoning_content 回传、原生联网搜索/读链接、引用、图片输入、JSON 模式、缓存/错误码/价格）见 [deepseek-api-adaptation.md](deepseek-api-adaptation.md)。
- Gemini 原生协议适配（thinkingLevel、thoughtSignature、函数调用、Google Search/URL Context、grounding 引用、多模态、用量/价格）见 [gemini-api-adaptation.md](gemini-api-adaptation.md)。
- 运行时会为每家厂商最新一代 chat 模型合成可切换 profile（`routed_chat` + `model_*`），避免模型发现后快捷菜单膨胀；更早的模型仍可在设置路由中选择。

尚未完成：

- 完整的多步首次配置向导（当前是提示 + 手动 Save）；
- ~~单独的 `profiles` 持久表~~：已拍板不新增，槽位扩展参数改放 `model_routing.extra_json`；

- 定时自动刷新（当前是首次/手动刷新）。

`pytest`：172 passed；`models.db` 实测仅保留 DeepSeek / Gemini / OpenAI 三家，DeepSeek 自动发现返回 2 个 V4 模型（1M / 393216），Gemini 种子切换为 4 Carbon / 4 Neon / 4 Hydrogen / 4 Argon；旧厂商与过期 catalog 种子被清理，音频/OCR 模型与手动能力不被误删。

Provider Adapter 与 KV Cache 观测（2026-11-26）：

- `brain/providers.py`：`ProviderAdapter` + OpenAICompatible / DeepSeek / GeminiNative / Anthropic 适配器；LLMRouter 的请求构建、缓存字段归一化统一走 adapter，新增厂商只需扩展 adapter 注册表。
- `usage_ledger.cached_input_tokens`：DeepSeek `prompt_cache_hit_tokens` / OpenAI·Gemini `prompt_tokens_details.cached_tokens` / Anthropic `cache_read_input_tokens` 统一归一化并钳位到 prompt；旧库启动补列并从旧列回填，JSONL 导出/导入兼容。
- `cli.py usage` 增加总览 + 分 slot 的加权命中率；连续 3 次 prompt≥4000 且缓存为 0 时输出前缀/工具顺序退化提示。
- L1 事实查询与渲染按 `fact_id` 稳定排序；工具 schema 按 name 排序并递归规范键序，避免 MCP 注册顺序影响前缀缓存。
- `tests/test_prefix_cache_guard.py`：PCH-01/02/03；`tests/test_provider_adapters.py`：适配器解析与请求形状。

Phase B 验收硬化（2026-11-26）：

- `model_routing.extra_json` 承载槽位 `voice` / `effort` / `temperature` / `max_output_override`；外键改为 `ON DELETE RESTRICT`，删除被引用 Provider 返回 409；
- 迁移前生成 `config.yaml.bak-models`（0600）；迁移异常时 `model_store_mode=yaml_fallback`，服务继续用 YAML 启动；
- L3 单条超长技能/召回按上限头尾截断；图片轮次不更新纯文本 Token 校准；
- ToolExecutor 收尾请求复用折叠并硬截断残留工具输出；折叠占位符保留工具名、关键参数与 work_history 线索；
- 滚动摘要改为分段感知裁剪，5 个固定标题保留，缺失【用户偏好与约束】从上一版回填；
- Ollama 发现并发读取 `/api/show` 的 `*.context_length` / `num_ctx`；Gemini preview 标记 `is_preview`；
- 发现器使用安全正整数解析；Provider patch 校验 1K–10M 上下文与 max_output < context，非法返回 422。

---

## 10. 已确认决策与待确认项

已确认：

- 模型配置全部进独立 `models.db`，不走 YAML；
- 不新增 `profiles` 实体表，保持 3 表精简结构；槽位级 `voice` / `effort` / `temperature` / `max_output_override` 存在 `model_routing.extra_json`；
- `models.db` 权限 0600（含 wal/shm），暂不上 Keychain；
- Gemini 第一版：原生 `models.list` 发现 + 官方 OpenAI-Compatible 端点对话；
- 模型发现：首次填 Key 自动拉一次，之后仅手动「刷新模型」；
- 退役模型：迁移后 `enabled=0`，设置页提示并可一键切到推荐 V4 模型，不静默替换；
- 预算档位：16K / 32K / 64K，默认均衡 32K；
- 压缩：高低水位滞回 + 800–1200 字结构化摘要；
- 工具循环：动态上限 + 旧观察折叠。

待确认：

1. `request_max_tokens` 默认 8192 是否合适（DeepSeek V4 输出上限很大，但实际请求不需要拉满）。
2. 摘要硬上限 1600 字是否足够。
3. 工具折叠保留最近 2 步还是 3 步。
4. L3 超限时的优先级（当前：技能 → 附件 → OCR → 召回）。
5. 旧模型一键迁移的推荐映射（V4 Flash / V4 Pro / 让用户选）。

---

## 11. 参考

- [DeepSeek – List Models](https://api-docs.deepseek.com/api/list-models)
- [DeepSeek V4 发布公告](https://api-docs.deepseek.com/news/news260424/)
- [Gemini API – Models](https://ai.google.dev/gemini-api/docs/models)
- [Gemini API – models.list](https://ai.google.dev/api/models)
- [Cline – OpenAI Compatible provider](https://docs.cline.bot/provider-config/openai-compatible)
- [Roo Code – OpenAI Compatible providers](https://roocodeinc.github.io/Roo-Code/providers/openai-compatible/)
- [Continue – config reference](https://docs.continue.dev/reference)
- [LibreChat – custom endpoint structure](https://www.librechat.ai/docs/configuration/librechat_yaml/object_structure/custom_endpoint)
- [LibreChat – default params](https://www.librechat.ai/docs/configuration/librechat_yaml/object_structure/default_params)
