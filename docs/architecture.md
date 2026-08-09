# 多端口架构详解

> 本项目是**多端口架构**：每个上游供应商一个独立监听端口，由 `targets.json` 配置驱动。
> 所有端口共享统一的 HTTP 解析 / 转发 / 429 翻译 / 重试逻辑。**端口-供应商绑定由 `targets.json` 定义，无需修改 server.py。**

## 端口总览

| 端口 | 供应商 | 分类 | handler | 协议 | 用途 |
|------|--------|------|---------|------|------|
| **8080** | aggregator | aggregate | aggregator | OpenAI | 聚合网关（虚拟模型路由 / 会话粘性 / 重试降级 / 配额熔断） |
 | **8079** | dashboard | — | FastAPI | 管理面 | dashboard 管理界面 + 全部 `/api/*` 管理 API（独立 app，由 `server.dashboardPort` 配置，默认 8079） |
 | **8081** | anthropic-compatible | — | FastAPI | Anthropic | Anthropic 翻译入口（`/v1/messages` 按 `models[]` 解析目标端口，翻译为 OpenAI 后转发到该本地端口）；仅保留 `/v1/messages`/`count_tokens`/`/v1/models`/`/api/tags`/`/`，其 `/dashboard`、`/api/*` 反向代理到 8079 |
| **8082** | copilot | crack | copilot | OpenAI | GitHub Copilot Enterprise 破解透传（企业 PAT） |
| **8083** | copilot | crack | copilot | OpenAI | 个人版 Copilot（上游 api.githubcopilot.com，与 8082 企业版账号隔离） |
| **8084** | codebuddy | crack | passthrough | OpenAI | CodeBuddy 破解透传 |
| **8085** | qclaw | crack | qclaw | OpenAI | QClaw 直连上游（自动解密 API Key） |
| **8086** | trae-work | crack | trae-work | OpenAI | Trae Work 破解透传（签到/额度/续期，OpenAI↔llm_utils_chat 转换） |
| **8090** | openrouter | free | passthrough | OpenAI | 免费透传（客户端带 key） |
| **8091** | nvidia | free | passthrough | OpenAI | 免费透传 |
| **8092** | gemini | free | **gemini-native** | OpenAI↔Gemini | **原生 Gemini 协议转换**（generateContent） |
| **8093** | opencode-zen | free | passthrough | OpenAI | 免费透传 |
| **8094** | open-go | paid | passthrough | OpenAI | 收费透传 |
| **8095** | deepseek | free | passthrough | OpenAI | DeepSeek 直连透传（`stripV1:true`，上游 `api.deepseek.com` 无 `/v1` 前缀，客户端带 key） |

- **分类**：`crack`（破解获取 token，注入 secrets.json）/ `free`（免费，透传客户端 key）/ `paid`（收费，透传客户端 key）/ `aggregate`（聚合网关，不持凭据）
- **isFree**：管理界面维护，标记供应商 key 是否免费（重试策略预留字段）

## base_url 规范（客户端接入）

**客户端 base_url 统一规范**：

| 分类 | base_url 后缀 | 说明 |
|------|--------------|------|
| crack 类（8082-8086） | `/v1` | **统一 `/v1`**，代理内部把 `/v1/*` 映射到下游（`routePrefix`） |
| gemini-native（8092） | `/v1` | 客户端走 OpenAI 协议入口，内部转原生 Gemini API |
| free/paid 透传（8090-8094） | `routePrefix`（如 `/api/v1`） | 直接透传上游同路径 |

示例：
```
OpenAI 协议：base_url = http://192.168.2.128:8090/api/v1，api_key = 任意（free 类用真实 key）
Anthropic 协议：base_url = http://192.168.2.128:8081，api_key = "dummy"
dashboard：      http://192.168.2.128:8079/dashboard
```

> dashboard 卡片详情页的 `base_url` 属性直接展示可粘贴即用的地址（局域网 IP + 端口 + 后缀）。dashboard 独立挂在 8079，8081 的 `/dashboard`、`/api/*` 请求会反向代理到 8079。

## targets.json schema

```jsonc
{
  "targets": [
    {
      "label": "anthropic",        // 8081 的 anthropic-compatible 入口（对应 server.listenPort）
      "listenPort": 8081,
      "category": "free",          // 入口不持凭据，仅做翻译与路由
      "handler": "anthropic",      // 标记本 target 为 Anthropic 翻译入口
      "enabled": true,
      // —— models / modelDefaults 已迁移进此 target 嵌套（原顶层 models/modelDefaults 不再是有效数据源）——
      "modelDefaults": { "defaultPort": 8082 },   // 直连端口未命中时的默认端口（8081 未命中直接 404，不用它）
      "models": [                                  // 模型定义（名称/别名 → 下游端口+真实模型）
        { "name": "sonnet", "aliases": [], "target": { "port": 8080, "model": "agg:sonnet" } },
        { "name": "haiku", "aliases": [], "target": { "port": 8082, "model": "claude-haiku-4.5" } }
      ]
    },
    {
      "label": "copilot",          // 唯一标识（dashboard/API 用）
      "listenPort": 8082,          // 本机监听端口
      "category": "crack",         // crack / free / paid
      "handler": "copilot",        // passthrough / copilot / qclaw / gemini-native / trae-work / aggregator / anthropic
      "targetHost": "...",         // 上游 host
      "targetPort": 443,           // 上游端口
      "targetProtocol": "https",   // http / https
      "routePrefix": "",           // 上游路径前缀（/v1 → 映射规则见下）
      "crackTool": "crack_copilot.py",
      "secretRef": "copilot_token",     // secrets.json 的 key
      "apikeyEnv": "COPILOT_GHE_TOKEN", // 环境变量兜底
      "models": [...],                 // 模型白名单（字符串或 {id, enabled}）
      "extraHeaders": {"Copilot-Integration-Id": "..."},
      "cleanCodebuddyBody": false, // 剥离上游不兼容的推理类参数 + system prompt 热重写
      "cleanQclawBody": false,     // qclaw body 白名单清理
      "normalizeSse": false,       // SSE 帧规范化（修不合规上游，见下）
      "normalizeFinishReason": true, // normalizeSse 子开关：finish_reason "" → null
      "stripV1": false,            // 上游 OpenAI 兼容但无 /v1 前缀（如 DeepSeek）：客户端 /v1/xxx → /xxx
      "isFree": false,
      "enabled": true
    }
  ]
}
```

**secrets 优先级**：`secrets.json` > `apikeyEnv` 环境变量 > 客户端透传（free/paid）。

### 顶层 `server` 段（主服务运行配置）

`.env` 已废弃删除（备份 `.env.bak`），原非私密运行配置并入 `targets.json` 顶层 `server` 段。整段与任意子键均可省略，缺失时按默认补齐（用户 server 段与 `config_store.py` 的 `DEFAULT_SERVER_CONFIG` 做**一层深合并**：顶层子键缺失补默认，嵌套 dict 的键缺失补默认，但不递归）。旧部署用一次性脚本 `scripts/migrate_env_to_targets.py` 把旧 `.env` 迁进本段。

```jsonc
{
  "server": {
    "listenPort": 8081,                 // 原 ANTHROPIC_PORT，anthropic-compatible 入口端口（对应 targets[] 中 handler="anthropic" 的 8081 target）
    "dashboardPort": 8079,              // dashboard 管理面独立端口（与 8081 翻译入口分离，架构统一）
    "log": {                            // 原 DEBUG/LOG_FILE/LOG_RETENTION_DAYS/LOG_ROTATE_WHEN/LOG_ROTATE_INTERVAL
      "debug": false,
      "file": "",
      "retentionDays": 7,
      "rotateWhen": "midnight",
      "rotateInterval": 1
    },
    "cache": {                          // 原 CACHE_ENABLED/CACHE_MAX_SIZE/CACHE_TTL_SECONDS/CACHE_MAX_ITEM_SIZE_KB
      "enabled": true,
      "maxSize": 500,
      "ttlSeconds": 3600,
      "maxItemSizeKb": 100
    }
  }
}
```

> **只剩这四个键**（`listenPort` / `dashboardPort` / `log` / `cache`）。早期这里还有四个键（选择默认 provider、legacy 大中小模型名、copilot 配置、qclaw baseUrl），它们随 legacy 单端口模式一并删除，已不在 `DEFAULT_SERVER_CONFIG` 中；旧配置里残留会被深合并逻辑静默丢弃，不报错。models/modelDefaults 也已从顶层迁入 `targets[]` 的 anthropic target 嵌套。
>
> 原 copilot 段的 GHE host / integration id / 大中小模型名已**函数化**（`gateways/copilot.py` 的 `_copilot_api_base()` / `_copilot_integration_id()` / `_copilot_*_model()`），每次调用从 copilot target（8082/8083）的 `targetHost` / `extraHeaders` / `modelRoles` 实时推导；原 qclaw 段的 baseUrl 同理由 qclaw target 的 `targetHost` 承载。两者都不再需要独立配置项。

### 全量配置导出/导入（v2）

`GET /api/config/export` / `POST /api/config/import` 把两个 gitignored 配置源打包成**单个 JSON**（v2 格式：`{version, exportedAt, targets, secrets}`，`version=2`）。运行配置已并入 `targets.server` 段，导出结构中**不再有独立 `env` 段**。导入校验 version + `validate_targets`（失败 422 且不写任何文件），成功写两文件 + `_reload_targets()`（端口 diff）+ `_refresh_secrets()` 即时生效。

### SSE 帧规范化（normalizeSse）

部分上游返回的 SSE 帧不符合 OpenAI 协议，而 `passthrough` 是纯字节转发，畸形帧会原样传给客户端。开启 `normalizeSse` 后，代理对该 target 的流式响应逐帧清洗。

**当前用例**：codebuddy（`copilot.tencent.com`）思考帧夹带空 `content`，导致客户端把思考链切成逐 token 换行（详见 [codebuddy.md](codebuddy.md) §3.5）。

**处理链路**：

```
上游 chunk → _SseLineBuffer 重组完整行 → 诊断统计(基于原始行) → _normalize_codebuddy_sse_line → 写出
```

**清洗规则**（判据是**值是否为空**，不是字段名）：

| 条件 | 动作 |
|------|------|
| `reasoning_content` 非空且 `content == ""` | 删 `content` 键 |
| `content` 非空且 `reasoning_content == ""` | 删 `reasoning_content` 键 |
| `tool_calls == []` / `function_call is None` / `refusal == ""` / `extra_fields is None` | 删该键 |
| `function_call == {"name":"","arguments":""}` | 删（首帧是空内容 dict 而非 null，需单独判断） |
| `finish_reason == ""` | 归一为 `null`（受 `normalizeFinishReason` 控制） |
| 上述字段**有内容**时 | **一律保留**（`tool_calls`/`function_call` 删了会断工具调用链） |

**设计约束**（新增同类逻辑时必须遵守）：

1. **必须先重组行再改写**——SSE 帧会被 TCP 任意切断，纯透传时无所谓，但逐帧改写前不重组就会切坏 JSON
2. **诊断统计基于改写前的原始行**——否则规范化自身的 bug 会掩盖上游真实异常
3. **失败一律原样透传**——解析失败/畸形帧/`[DONE]`/keep-alive 都不改写，绝不吞帧或中断流
4. **未改动的帧返回原对象**——不重新序列化，保住零开销
5. **空值判定要带类型校验**——用 `type()` 比对避免 `0`/`False` 被当成空值误删

> **为什么不能"保守保留"空字段**：曾误以为保留 `tool_calls:[]` 等空字段更安全（怕破坏依赖键存在性的客户端），实际恰恰相反——Vercel AI SDK 正是按"键是否出现"分段，见 `tool_calls` 键就结束当前 reasoning part，导致 opencode 把思考链切成数百块。改 SSE 前先确认目标客户端用哪个 SDK、按什么规则分段。

> `normalizeSse` 为真时会自动启用逐帧处理链路（与 SSE 诊断日志共用）。诊断日志附带 `normalized=N` 表示本次改写的帧数。

> **配置来源约定**：配置文件只有两个——`targets.json`（运行配置 + Target 定义）与 `secrets.json`（私密凭据）。`.env` 已废弃删除（备份 `.env.bak`），原非私密运行配置并入了 `targets.json` 顶层 `server` 段。私密凭据唯一事实源是 `secrets.json`（dashboard 可热编辑、mtime 热重载 2s 生效）。`apikeyEnv` 仅为兼容旧部署的兜底。已收敛：`COPILOT_GHE_TOKEN` 并入 `secrets.json` 的 `copilot_token`（同源，server.py 翻译层热重载同步），`CODEBUDDY_TOKEN` 冗余已删。

**热重载**：`targets.json` / `secrets.json` mtime 轮询（2s），修改后自动生效，无需重启。

## 聚合网关（8080）

聚合网关是**多下游统一入口**：客户端只连一个端口（8080，OpenAI 协议），用**虚拟模型 id**（如 `agg:sonnet`）请求，代理按配置把请求路由到多个真实下游 target 端口（8082 copilot / 8084 codebuddy / 8090 openrouter 等），并在成员之间做加权选择、会话粘性、失败重试与配额熔断。路由目标永远是**本地其他 target 端口**，聚合层本身不直连任何上游。

### targets.json 配置

聚合网关是一个普通 target（`handler: "aggregator"`），额外字段：

```jsonc
{
  "label": "aggregator",
  "listenPort": 8080,
  "category": "aggregate",
  "handler": "aggregator",
  "enabled": true,
  "targetHost": "",            // aggregator 不直连上游，targetHost/targetPort 置空
  "name": "聚合网关",
  "poolDefaults": {            // 全局默认（虚拟模型级可覆盖）
    "defaultRetries": 2,       //   默认池每个成员尝试次数
    "fallbackRetries": 1,      //   降级池尝试次数
    "sessionAffinityTtlSeconds": 3600,  // 会话粘性缓存 TTL
    "probeIntervalSeconds": 300,        // 熔断端口探测间隔
    "weight": 1                //   成员默认权重（未显式指定时）
  },
  "quotaErrorPatterns": ["insufficient credit", "quota exceeded", "credits exhausted", "余额不足", "积分不足", "配额不足", "resource exhausted"],
  "virtualModels": {
    "agg:sonnet": {
      "defaultPool": [
        {"port": 8082, "model": "claude-sonnet-5", "weight": 3},
        {"port": 8084, "model": "deepseek-v4-pro", "weight": 2},
        {"port": 8090, "model": "openrouter/auto"}
      ],
      "fallbackPool": [{"port": 8094, "model": "gpt-5.6-luna"}],
      "defaultRetries": 3,     // 虚拟模型级覆盖 poolDefaults
      "fallbackRetries": 1
    }
  }
}
```

字段语义：

- **`virtualModels`**：虚拟模型 id → 配置的字典。每个虚拟模型含 `defaultPool`（必填，非空）+ `fallbackPool`（可空）+ `defaultRetries` / `fallbackRetries`（可省略，回退 `poolDefaults`）
- **池成员**：`{"port": 下游端口, "model": 真实模型名, "weight": 可选权重}`；`weight` 缺省用 `poolDefaults.weight`（默认 1，平等）
- **`poolDefaults`**：聚合级默认值，被虚拟模型级字段覆盖
- **`quotaErrorPatterns`**：配额/积分不足类错误的正则列表（大小写不敏感），作为**状态码无法分类时的文本兜底**（详见下文「失败分类」）。**与 429 限流严格区分**：429 由 `_VENDOR_ERROR_PATTERNS` 翻译但不摘除端口；配额文本命中会**熔断**该端口
- 校验（config_store.py）：aggregator target 必须有非空 `virtualModels`，每个虚拟模型必须有非空 `defaultPool`，`fallbackPool` 必须为列表；`targetHost`/`targetPort` 允许为空

### 路由策略

- **加权随机**：每次从可用成员中按权重做加权随机选择（权重越大命中概率越高）
- **会话粘性**：同一会话的连续请求尽量走同一成员。粘性 key = `(虚拟模型 id, session_id)`，本地内存缓存 + TTL（`sessionAffinityTtlSeconds`）。session_id 取自请求头 `x-session-id` / `x-conversation-id`，或 body 的 `conversation_id` / `session_id` / `user`。命中熔断成员的粘性缓存自动失效并重选
- **未知虚拟模型**：body 的 `model` 不在已配置列表 → **400 快速失败**（不静默转发）

### 重试与降级

1. 默认池尝试 `defaultRetries` 次（默认每次换成员，避免同一成员重复失败；唯一例外是 408/5xx 的「同端点重试 1 次」，见下文失败分类）
2. 默认池全部失败 → 降级池尝试 `fallbackRetries` 次（走 `fallbackPool` 成员，成功记为降级）
3. 全部失败 → **503**（`AllPoolsExhausted`）；降级池为空则默认池失败后直接 503

注意：并非所有非 2xx 都会进重试循环。`pass_through_to_client`（400/404/422）和 `unclassified` 分类会**立即返回客户端**，不消耗剩余重试次数——客户端自己请求写错了，换个下游端口也是一样的错。

### 失败分类（classify_failure 五分类）

每次下游响应都先过 `AggregatorEngine.classify_failure(status_code, body_text)`，返回五种分类之一，后续的重试 / 熔断 / 降级 / 透传全部由分类决定。默认池与降级池用**完全一致**的五分类语义，唯一差异是降级池成功记 `degraded` 且不更新会话粘性。

| 分类 | 含义 | 触发条件 | 行为 |
|---|---|---|---|
| `success` | 成功 | 2xx；或无状态码（`None`）且文本未命中配额词 | 正常返回 + 会话粘性生根（降级池记 `degraded`，不生根） |
| `retry_same` | 可重试 | 408 / 429 / 500 / 502 / 503 / 504 / 508 | **429**：直接换端点，不熔断；**408/5xx**：同端点重试 1 次，仍失败换端点，全程不熔断 |
| `retry_other_or_fallback` | 应换端点 | 401 / 402 / 403；或未知状态码经文本兜底命中 `quotaErrorPatterns` | **熔断该端口**（reason 具体化）+ 换端点 |
| `pass_through_to_client` | 直接透传 | 400 / 404 / 422 | 不重试、不换端点、不熔断，响应原样返回客户端 |
| `unclassified` | 未分类 | 有状态码但不在映射表，且文本未命中 | 按透传处理 + 打 `WARNING` 日志（端口 / 模型 / 状态码 / body 前 200 字符） |

状态码 → 分类的映射表是代码内置常量 `_HTTP_STATUS_CLASSIFICATION`（`gateways/aggregator/engine.py`）：

```
400 → pass_through_to_client   408 → retry_same    429 → retry_same
401 → retry_other_or_fallback  422 → pass_through_to_client
402 → retry_other_or_fallback  500/502/503/504/508 → retry_same
403 → retry_other_or_fallback  404 → pass_through_to_client
```

**⚠️ 两条硬规则（判定优先级，务必记住）**

1. **状态码严格优先于文本。** 2xx 响应即使 body 里含 `insufficient credit` / 「余额不足」这类配额词，也**绝不**判定为失败或熔断——这些词完全可能出现在正常对话内容里（用户问「什么是 quota exceeded」），对 2xx 做文本兜底必然误熔断。代码里 2xx 分支直接 `return "success"`，根本不调用 `quota_error_fn`。
2. **文本兜底只在状态码无法分类时生效。** 只有状态码不在 `_HTTP_STATUS_CLASSIFICATION` 里（未知非 2xx 状态码），才回退去匹配 `quotaErrorPatterns`。命中 → `retry_other_or_fallback`（reason 记 `quota_text`），未命中 → `unclassified`。状态码为 `None`（无状态码信号，如测试注入的裸字符串）且文本未命中时，安全默认 `success`。

性能上还有个惰性优化：状态码已能确定分类（2xx 或命中映射表）时压根不读 body，只有需要文本兜底才调 `_extract_body_text`。流式响应（`text/event-stream`）一律返回空文本，绝不消费流 body（否则会破坏 `_write_response` 的流式转发）。

### 熔断规则与探测恢复

**会触发熔断（trip）的只有四种**，reason 是具体分类字符串，不再是笼统的 `quota_error`：

| 状态码 / 条件 | trip reason | 说明 |
|---|---|---|
| 401 | `401_auth` | token 过期 / 无效 |
| 402 | `402_billing` | 欠费 |
| 403 | `403_forbidden` | 被封禁 / 无权限 |
| 未知状态码 + 文本命中 `quotaErrorPatterns` | `quota_text` | 上游用非标准方式包装的配额耗尽 |

**绝不触发熔断的两类**：

- **429（限流）**：账号级瞬时问题，同端点重试无意义 → 立刻换端点。之所以不 trip，是因为熔断粒度是端口，trip 300s 会误伤共享该端口的其它虚拟模型和会话，而限流本身几十秒就自行恢复了。计入 `error_types` 的 `429_rate_limit`。
- **5xx / 408（网络抖动）**：同端点重试 1 次（首次失败计入 `error_types` 的 `{status}_transient`，如 `500_transient`），仍失败才换端点（记 `5xx_persistent`），全程不熔断。单次抖动就摘端口会误伤健康节点。

**熔断行为**：粒度是**端口**——`trip(port)` 会摘除该端口下*所有*虚拟模型池中的*所有*成员（同一端口可能同时服务 `agg:sonnet` 和 `agg:opus`）。熔断期间路由跳过该端口，粘性缓存若指向它也失效重选。

**探测恢复**：`_aggregator_prober` 每 5s 检查一次熔断端口是否到达 `probeIntervalSeconds` 间隔，到期发最小探测请求（`{"model": "probe", "max_tokens": 1}`），状态码 < 500 视为恢复并解除熔断，否则保持熔断并重置计时。

**与 server.py 429 翻译层的关系（既有设计不变）**：聚合层的 `quotaErrorPatterns` 与统一转发层翻译 429 用的 `_VENDOR_ERROR_PATTERNS` 是**两套完全分离的机制**。后者只翻译状态码文案，不参与聚合层的任何熔断决策。

### 如何新增一条错误规则

先判断这条规则该落在**代码常量表**还是 **targets.json 配置**：

| 场景 | 改哪里 | 生效方式 |
|---|---|---|
| 状态码语义清晰的标准错误（401/402/403/429/5xx 这类） | `gateways/aggregator/engine.py` 的 `_HTTP_STATUS_CLASSIFICATION`（必要时同步 `_RETRY_OTHER_REASONS`） | 改代码，需重启服务 |
| 上游用非标准文本包装配额错误的边缘情况 | `targets.json` 里该 aggregator target 的 `quotaErrorPatterns` | 改配置，mtime 轮询 2s 自动热重载 |

**例 1：某上游用 418 表示配额耗尽，希望被正确分类并熔断。**
这是状态码规则，改代码常量表。在 `_HTTP_STATUS_CLASSIFICATION` 加一行：

```python
418: "retry_other_or_fallback",
```

想让 trip reason 更可读，再在 `_RETRY_OTHER_REASONS` 加 `418: "418_quota"`；不加则统一记为 `quota_text`。改完重启服务。

**例 2：某上游用非标准 body 文本 `over capacity` 表示欠费。**
这是文本兜底，改 `targets.json` 该 aggregator target 的 `quotaErrorPatterns` 数组：

```jsonc
"quotaErrorPatterns": ["insufficient credit", "quota exceeded", "余额不足", "over capacity"]
```

保存即热重载生效。**但要先确认上游返回的状态码**：文本兜底只在状态码不在 `_HTTP_STATUS_CLASSIFICATION` 里时才会被查。如果上游返回 200 带这段文本，2xx 直接判 `success`，这条正则永远不会被触发（这是刻意设计，见上文硬规则 1）；如果上游返回的是 402/403 这类已在表里的状态码，也走不到文本兜底——那种情况本来就已经被正确熔断了，不需要加正则。真正需要这条正则的场景是：上游返回一个**表里没有的非 2xx 状态码**（如 419、520），body 里带自定义配额文案。

### 认证边界

聚合层**不解析任何 `secretRef` / `apikeyEnv`**，也不持有任何凭据。它只把客户端的 `Authorization` 原样透传给所选下游端口，鉴权由各下游 target 自己处理（crack 注入 secrets、free/paid 透传客户端 key 等逻辑全部不变）。

### 监控

`GET /api/aggregate/status`（即 `engine.get_stats()`）返回，不含任何密钥：

- **`virtual_models`**：虚拟模型 id → `{"端口:模型": {requests, ok, err, degraded, avg_latency_ms, error_types}}`。`error_types` 是该成员按错误类型分列的计数字典，键就是上文分类表里的 reason 字符串（`401_auth` / `402_billing` / `403_forbidden` / `quota_text` / `429_rate_limit` / `500_transient` 等 `{status}_transient` / `5xx_persistent` / `pass_through_to_client` / `unclassified`）。这是区分「token 过期」「欠费」「被封禁」「限流触顶」的关键依据——同样是错误计数上涨，401 要去刷 token，402 要去充值，429 只需等限流窗口过去
- **`session`**：会话粘性 `cache_size / hits / lookups / hit_rate`
- **`breakers`**：熔断端口 → `{state, reason, tripped_at}`。`state` ∈ `normal` / `tripped` / `probing`，`reason` 为具体分类字符串，`tripped_at` 是熔断发生时的 Unix 时间戳
- **`started_at` / `uptime_seconds`**

dashboard「聚合网关」分组的 8080 卡片由前端 `loadAggregateStatus()` fetch 该接口填充，**10s 自动刷新**：

- 每个虚拟模型一个可折叠详情块，内含默认池 / 降级池表格（端口·模型 / 权重 / 请求 / 成功率 / 错误 / 降级 / 延迟）；10s 刷新会保留用户已展开的折叠状态

#### ⚠️ 高危事件区

**有熔断端口**（`breakers` 非空）**或**有错误类型计数（汇总后的 `error_types` 非空）时，卡片才额外渲染「⚠️ 高危事件」区块；两者都为空时**整个区块不渲染**（一切正常时不占版面，也没有「无熔断端口」之类的空状态文案）。区块内两部分同样各自按需渲染：

- **熔断端口**（仅 `breakers` 非空时）：每条一行——端口号 + 状态指示灯 + reason 徽标（`401_auth` / `402_billing` / `403_forbidden` / `quota_text`）+ `tripped_at` 本地化时间（`formatTs`；时间戳为 0 或缺失时该时间省略不显示）
- **错误类型统计**（仅汇总非空时）：把所有虚拟模型所有成员的 `error_types` 汇总相加，按计数**从高到低降序**排列，每行显示中文标签（`aggErrorLabel`，如 `401_auth` → 「凭据失效」）+ 原始 key + 计数。计数为 0 的类型不会出现（`error_types` 里根本没有这个键）

这个区块的价值在于**一眼分辨故障性质**：401 是 token 过期（去重新破解），402 是欠费（去充值），403 是被封禁（换账号），429 是限流触顶（等窗口过去，不必人工介入），`5xx_persistent` 是上游持续不可用（查上游状态）。没有它，dashboard 上只能看到「错误数涨了」，无从判断该动哪个网关。

### 配置编辑（dashboard，非黑盒）

- `GET/PUT /api/aggregate/config`：读写聚合 target 的 `virtualModels / poolDefaults / quotaErrorPatterns / name`。PUT 为**整体替换语义**（`virtualModels` 传完整 map；未提供的字段保留现值），校验后写 targets.json 并热重载（引擎自动 reload，保留会话/熔断状态）
- `GET/PUT /api/models`：读写 `targets[]` 中 `handler="anthropic"` 的 8081 target 嵌套的 `modelDefaults / models`（原顶层 `models/modelDefaults` 已迁入此 target）
- dashboard：8080 卡片「✏️ 编辑配置」modal（虚拟模型增删/池成员 port/model/weight/retries/降级池编辑）、8081 卡片「✏️ 模型定义」modal（modelDefaults + models[] 增删改，操作的就是该 8081 target 的嵌套字段）

### 模型定义（models）

8081 与所有 OpenAI 协议直连端口统一用 `models[]` 做别名解析（`_resolve_model_alias`）。该 `models[]` 与 `modelDefaults` 现位于 `targets[]` 中 `handler="anthropic"` 的 8081 target 嵌套内（server.py 启动时读此 target 填充 `_MODELS_CFG`，顶层残留的 `models/modelDefaults` 不再生效）：

- **命中且 `target.port` 等于请求到达的端口** → 只改写模型名继续原上游转发
- **命中且指向另一端口**（含聚合网关 `agg:xxx`）→ 整体改路由到该端口
- **未命中** → 8081 直接返回 404（legacy 单端口模式下线后不再有兜底路径，模型必须先在 `models[]` 里配路由）；直连端口未命中则原样透传模型名给自己上游

示例（嵌套在 anthropic target 内）：

```jsonc
{
  "label": "anthropic",
  "listenPort": 8081,
  "handler": "anthropic",
  "modelDefaults": { "defaultPort": 8082 },
  "models": [
    { "name": "sonnet", "aliases": [], "target": { "port": 8080, "model": "agg:sonnet" } },
    { "name": "haiku", "aliases": [], "target": { "port": 8082, "model": "claude-haiku-4.5" } }
  ]
}
```

**防环约束**：任何 `models[].target.port` 或聚合网关池成员 `port` 不得等于 8081（anthropic-compatible 自身端口），否则配置校验报错。

**管理入口**：dashboard 8081 卡片「✏️ 模型定义」→ `GET/PUT /api/models`（实际操作 anthropic target 嵌套的 models）

实现：`gateways/aggregator/engine.py`（`AggregatorEngine`，纯路由/分类/熔断/统计逻辑，无网络 I/O）+ `gateways/aggregator/http_adapter.py`（`_handle_aggregate_request` 分发 / `_aggregator_prober` 探测）+ `server.py`（reload 钩子 / `/api/aggregate/status`、`/api/aggregate/config`、`/api/models` / lifespan 预初始化引擎）+ `config_store.py`（aggregate/aggregator 校验；`models/modelDefaults` 现从 anthropic target 加载，顶层残留仅保留兼容）。

## 路径重写（_rewrite_upstream_path）

优先级：**handler 映射表 > routePrefix 重写 > stripV1 剥离 > 原样**。

- **handler 映射表**（`_HANDLER_PATH_MAP`）：如 copilot 的 `/v1/chat/completions` → `/chat/completions`、`/v1/models` → `/models`（上游无 /v1 前缀）
- **routePrefix 重写**：客户端 `/v1/xxx` → `routePrefix + /xxx`（如 openrouter `/v1/chat/completions` → `/api/v1/chat/completions`）
- **stripV1 剥离**：`stripV1: true` 时把客户端 `/v1/xxx` → `/xxx`（上游 OpenAI 兼容但根路径直接挂端点，无 `/v1` 前缀，如 DeepSeek `api.deepseek.com/chat/completions`）。配合 `handler=passthrough` 使用，是「删除 /v1」的通用表达（routePrefix 是替换语义，无法删除）
- **原样**：其余路径直接透传

## 网关模块清单（gateways/）

除各端口 handler 对应的网关注册（`server.py` 的 `_HANDLER_PATH_MAP` / `_HANDLE_TARGET_REQUEST`）外，代理还包含若干**旁路/支撑模块**，放在 `gateways/` 下，遵循 AGENTS.md §7 的跨模块约定（函数内延迟导入 + `import server as _srv` 访问热重载全局，旁路模块用 `sys.modules.get("server")` 探测复用 logger，绝不首次拉起 server）：

- **gateways/rtk.py** — RTK token-saver（9Router `open-sse/rtk` 的 Python 移植，MIT，纯标准库）。在 Anthropic→OpenAI 翻译**前**确定性压缩超长 `tool_result`（头 120 行 + 尾 60 行，中段替换为 `... +N lines truncated` 标记），降 prompt token 消耗。只碰 `tool_result`，`text`/`thinking`/`tool_use` 一律零改动；常量 `RAW_CAP=10MiB` / `MIN_COMPRESS_SIZE=500` / `SMART_TRUNCATE_HEAD=120` / `TAIL=60` / `MIN_LINES=250`。**安全护栏**：`is_error=True` 的 tool_result 不压缩；过小（字节 < 500 或行数 < 250）/ 过大（> 10MiB）/ 压缩失败 / 压完变空或没变短 → 原样透传；所有异常静默 `pass`，绝不崩请求。开关为 8081 anthropic target 的 `tokenSaver: {enabled, minSize, maxSize}`（**默认关闭**，`enabled` 布尔、`minSize`/`maxSize` 字节阈值备用）；入口在 `anthropic_convert.convert_anthropic_request_to_openai` 翻译前注入，日志 `[RTK] saved XB / YB (Z%) via [smart-truncate] hits=N`。
- **gateways/usage_store.py** — 用量统计 SQLite 持久化（纯标准库 `sqlite3`，WAL，无第三方依赖）。把内存统计异步落盘，进程重启不丢。落盘路径 `.cache/usage.db`（表 `usage_daily`，主键 `(date, label, model)`，含 `requests/ok/err/translated429/prompt_tokens/completion_tokens`，无 cost 列、无 key/token/请求体）。公开函数 `init_db()` / `upsert_day()` / `get_trend(days)`：每个函数开头自愈建表，调用方漏调 `init_db()` 也不炸。flush 机制由 `server.py` 承担：内存 `_TODAY_ACCUM` 累加 → 60s 周期 `_usage_flush_loop` UPSERT（swap-then-write 防丢增量）+ 退出前 `_flush_usage_accum` 兜底。dashboard 新增「近 7 天 / 近 30 天请求量」趋势卡片（`get_trend(7)` / `get_trend(30)` 读取，渲染在 `dashboard/frontend.py` 的 `_build_trend_html`）。**降级**：DB 故障仅 `logger.warning`，返回 `False`/`{}`，不影响主请求链路。

## handler 分发

| handler | 行为 |
|---------|------|
| `passthrough` | 原样透传（注入认证 + 可选模型映射） |
| `copilot` | 模型映射（opus/sonnet/haiku → 从 target 的 `modelRoles` 实时推导）+ `Copilot-Integration-Id` + body 清理 |
| `qclaw` | 去前缀、自动补 system message、body 清理、注入 `User-Agent: OpenAI/JS 6.39.1` |
| `gemini-native` | **OpenAI ↔ Gemini 原生转换**（见下） |

> **codebuddy 特例（label=codebuddy）**：上游 `copilot.tencent.com` 对所有模型拒绝非流式 chat（错误码 11101），只接受 `stream:true`。
> 代理自动处理：客户端发非流式请求 → 检测到 11101 → 内部以 `stream:true` 重试 → 收集 SSE chunks 聚合为完整 JSON
> （含 `reasoning_content` / `tool_calls` / `usage`）→ 返回客户端。非流式客户端也可用，行为与 copilot/qclaw 一致。

## gemini-native 协议转换

8092 端口接受 **OpenAI 协议**请求，代理内部转换为 **Google 原生 generateContent API**：

| OpenAI | Gemini 原生 |
|--------|------------|
| `/v1/chat/completions` | `POST /v1beta/models/{model}:generateContent`（非流式）/ `:streamGenerateContent?alt=sse`（流式） |
| `messages[role=system]` | `systemInstruction.parts` |
| `messages[role=user/assistant]` | `contents[].parts[]` |
| `max_tokens` / `temperature` / `top_p` | `generationConfig.maxOutputTokens/temperature/topP` |
| `tools[].function` | `tools[].functionDeclarations` |
| `image_url` (data:) | `inline_data` (mime_type + base64) |
| 响应 `choices[]` | `candidates[]`（finishReason → finish_reason 映射） |
| `usage` | `usageMetadata`（prompt/candidates/totalTokenCount） |
| `/v1/models` | `GET /v1beta/models`（解析 `models[].name`） |

**认证**：客户端 `x-goog-api-key` / `Authorization` 优先，其次 `secrets.json` / `GEMINI_API_KEY` 环境变量。

## 破解工具 OS 支持矩阵

| 工具 | Windows | Linux/macOS |
|------|---------|-------------|
| `crack_copilot.py` | ✅（需 gh CLI） | ✅（gh CLI 跨平台） |
| `crack_codebuddy.py` | ✅（探测客户端目录） | ❌ 未实现 |
| `crack_qclaw.py` | ✅（DPAPI 解密 app-store.json） | ❌ 未实现（但 QCLAW_API_KEY 可手动设置） |
| `crack_traework.py` | ❌ 预留 | ❌ 未实现 |

> dashboard 的「重新破解」按钮通过 `_crack_env_check` 检测当前 OS 与依赖：
> 非 Windows 的 codebuddy/qclaw 破解会提示"仅支持 Windows，待后续补齐"并置灰按钮。
> **QClaw 例外**：即使无法本地破解，设置 `QCLAW_API_KEY` 环境变量或 dashboard 手动填写 key 后仍可直连上游。

## dashboard 管理界面

`http://127.0.0.1:8079/dashboard`（也可经任意 target 端口的 `/dashboard` 反向代理访问，实际由 8079 独立 app 承载，`/api/*` 同理）：

- **分类栏**：聚合网关（8081）/ 破解网关（crack）/ 直连网关（free/paid）三组
- **卡片头**：端口强调条 + 名称 + 分类 badge + 请求数 + 展开箭头
- **详情区**：kv 元信息（含可粘贴 base_url）+ 流量统计块（总请求/成功率/运行时长 + 进度条）+ 模型白名单表格 + token 编辑块
- **模型编辑弹框**（✏️ 编辑模型）：iOS 滑动开关、总开关（全开/全关/部分开 indeterminate）、搜索框、**下拉下游真实模型列表**（编辑态自动 fetch `/models`，失败降级配置）
- **8081 统计**：`/v1/messages` 请求数与模型级统计（中间件记录）

设计契约见 [`DESIGN.md`](../DESIGN.md)。

## 典型错误码速查

> 各网关详细排查见对应文档：🔗 [qclaw.md](qclaw.md) · [copilot.md](copilot.md) · [codebuddy.md](codebuddy.md) · [trae-work.md](trae-work.md)

| 错误码 | 网关 | 含义 | 常见处理 |
|---|---|---|---|
| `9002` | qclaw | 登录态/API Key 失效 | 重新提取 key 或 dashboard 更新 `qclaw_api_key` |
| `403` | copilot | token 无权限/过期/账号被踢 | 重新破解 `copilot_token` / `copilot_personal_token` |
| `11101` | codebuddy | 上游拒绝非流式 chat（只收流式） | 代理已自动转流式聚合，客户端无需处理 |
| `3003 all models failed` | trae-work | 模型不在多模态白名单时传图（glm 系） | 传图改用内置多模态模型（`Doubao_1_6` 等） |
| `1005` | trae-work | 同上（doubao 系）/ 模型不可用 | 同上；或换可用模型 |
| `4001 param is invalid` | trae-work | content 格式错误（如 `image` 字段） | 图片块用标准 `{"type":"image_url","image_url":{"url":...}}` |
| `401 / 402 / 403` | 聚合网关（8080） | 下游 token 过期 / 欠费 / 被封禁 | 按端口熔断，reason 分别为 `401_auth` / `402_billing` / `403_forbidden`，需人工介入 |
| `429` | 聚合网关（8080） | 下游账号级限流 | 直接换端点，**不熔断**（限流自行恢复，trip 会误伤共享该端口的其它虚拟模型） |
| `5xx / 408` | 聚合网关（8080） | 下游网络抖动 | 同端点重试 1 次，仍失败换端点，**不熔断** |
