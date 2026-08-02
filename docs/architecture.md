# 多端口架构详解

> 本项目是**多端口架构**：每个上游供应商一个独立监听端口，由 `targets.json` 配置驱动。
> 所有端口共享统一的 HTTP 解析 / 转发 / 429 翻译 / 重试逻辑。**端口-供应商绑定由 `targets.json` 定义，无需修改 server.py。**

## 端口总览

| 端口 | 供应商 | 分类 | handler | 协议 | 用途 |
|------|--------|------|---------|------|------|
| **8080** | aggregator | aggregate | aggregator | OpenAI | 聚合网关（虚拟模型路由 / 会话粘性 / 重试降级 / 配额熔断） |
| **8081** | anthropic-compatible | — | FastAPI | Anthropic | Anthropic 入口 + dashboard（`/v1/messages` 翻译为 OpenAI 后内部请求 8082） |
| **8082** | copilot | crack | copilot | OpenAI | GitHub Copilot Enterprise 破解透传 |
| **8084** | codebuddy | crack | passthrough | OpenAI | CodeBuddy 破解透传 |
| **8085** | qclaw | crack | qclaw | OpenAI | QClaw 直连上游（自动解密 API Key） |
| **8086** | trae-work | crack | passthrough | OpenAI | 预留（`enabled=false`） |
| **8090** | openrouter | free | passthrough | OpenAI | 免费透传（客户端带 key） |
| **8091** | nvidia | free | passthrough | OpenAI | 免费透传 |
| **8092** | gemini | free | **gemini-native** | OpenAI↔Gemini | **原生 Gemini 协议转换**（generateContent） |
| **8093** | opencode-zen | free | passthrough | OpenAI | 免费透传 |
| **8094** | open-go | paid | passthrough | OpenAI | 收费透传 |

- **分类**：`crack`（破解获取 token，注入 secrets.json）/ `free`（免费，透传客户端 key）/ `paid`（收费，透传客户端 key）/ `aggregate`（聚合网关，不持凭据）
- **isFree**：管理界面维护，标记供应商 key 是否免费（重试策略预留字段）

## base_url 规范（客户端接入）

**客户端 base_url 统一规范**：

| 分类 | base_url 后缀 | 说明 |
|------|--------------|------|
| crack 类（8082/8084/8085/8086） | `/v1` | **统一 `/v1`**，代理内部把 `/v1/*` 映射到下游（`routePrefix`） |
| gemini-native（8092） | `/v1` | 客户端走 OpenAI 协议入口，内部转原生 Gemini API |
| free/paid 透传（8090-8094） | `routePrefix`（如 `/api/v1`） | 直接透传上游同路径 |

示例：
```
OpenAI 协议：base_url = http://192.168.2.128:8090/api/v1，api_key = 任意（free 类用真实 key）
Anthropic 协议：base_url = http://192.168.2.128:8081，api_key = "dummy"
```

> dashboard 卡片详情页的 `base_url` 属性直接展示可粘贴即用的地址（局域网 IP + 端口 + 后缀）。

## targets.json schema

```jsonc
{
  "anthropicForwardPort": 8082,   // 8081 内部转发目标
  "targets": [
    {
      "label": "copilot",          // 唯一标识（dashboard/API 用）
      "listenPort": 8082,          // 本机监听端口
      "category": "crack",         // crack / free / paid
      "handler": "copilot",        // passthrough / copilot / qclaw / gemini-native
      "targetHost": "...",         // 上游 host
      "targetPort": 443,           // 上游端口
      "targetProtocol": "https",   // http / https
      "routePrefix": "",           // 上游路径前缀（/v1 → 映射规则见下）
      "crackTool": "crack_copilot.py",
      "secretRef": "copilot_token",     // secrets.json 的 key
      "apikeyEnv": "COPILOT_GHE_TOKEN", // 环境变量兜底
      "models": [...],                 // 模型白名单（字符串或 {id, enabled}）
      "modelMapping": {"opus": "...", "sonnet": "...", "haiku": "..."},
      "extraHeaders": {"Copilot-Integration-Id": "..."},
      "isFree": false,
      "enabled": true
    }
  ]
}
```

**secrets 优先级**：`secrets.json` > `apikeyEnv` 环境变量 > 客户端透传（free/paid）。

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
- **`quotaErrorPatterns`**：配额/积分不足类错误的正则列表（大小写不敏感）。**与 429 限流严格区分**：429 由 `_VENDOR_ERROR_PATTERNS` 翻译但不摘除端口；配额匹配会**熔断**该端口
- 校验（config_store.py）：aggregator target 必须有非空 `virtualModels`，每个虚拟模型必须有非空 `defaultPool`，`fallbackPool` 必须为列表；`targetHost`/`targetPort` 允许为空

### 路由策略

- **加权随机**：每次从可用成员中按权重做加权随机选择（权重越大命中概率越高）
- **会话粘性**：同一会话的连续请求尽量走同一成员。粘性 key = `(虚拟模型 id, session_id)`，本地内存缓存 + TTL（`sessionAffinityTtlSeconds`）。session_id 取自请求头 `x-session-id` / `x-conversation-id`，或 body 的 `conversation_id` / `session_id` / `user`。命中熔断成员的粘性缓存自动失效并重选
- **未知虚拟模型**：body 的 `model` 不在已配置列表 → **400 快速失败**（不静默转发）

### 重试与降级

1. 默认池尝试 `defaultRetries` 次（每次换成员，避免同一成员重复失败）
2. 默认池全部失败 → 降级池尝试 `fallbackRetries` 次（走 `fallbackPool` 成员，成功记为降级）
3. 全部失败 → **503**（`AllPoolsExhausted`）；降级池为空则默认池失败后直接 503

### 配额熔断与探测恢复

- **触发**：下游响应体匹配 `quotaErrorPatterns`（如「余额不足」「积分不足」「resource exhausted」）→ 按**端口**熔断（该端口所有虚拟模型的所有成员一并摘除，`reason: "quota_error"`）
- **熔断期间**：路由跳过该端口（粘性缓存若指向它也失效重选）；状态由 `tripped_ports()` 暴露
- **探测恢复**：`_aggregator_prober` 每 5s 检查一次熔断端口是否到达 `probeIntervalSeconds` 间隔，到期发最小探测请求（`{"model": "probe", "max_tokens": 1}`），状态码 < 500 视为恢复并解除熔断，否则保持熔断并重置计时
- **与 429 的区别**：429 限流由统一转发层的 `_VENDOR_ERROR_PATTERNS` 处理（只翻译状态码、重试策略预留），**不触发熔断**；配额/积分不足才是熔断依据

### 认证边界

聚合层**不解析任何 `secretRef` / `apikeyEnv`**，也不持有任何凭据。它只把客户端的 `Authorization` 原样透传给所选下游端口，鉴权由各下游 target 自己处理（crack 注入 secrets、free/paid 透传客户端 key 等逻辑全部不变）。

### 监控

- `GET /api/aggregate/status`：返回 `configured` + 每虚拟模型每成员的 `requests / ok / err / degraded / avg_latency_ms` + 会话粘性（`cache_size / hits / lookups / hit_rate`）+ 熔断端口（`breakers`）。不包含任何密钥
- dashboard「聚合网关」分组新增 8080 卡片，运行时状态由前端 fetch `/api/aggregate/status` 填充，**10s 自动刷新**

### 配置编辑（dashboard，非黑盒）

- `GET/PUT /api/aggregate/config`：读写聚合 target 的 `virtualModels / poolDefaults / quotaErrorPatterns / name`。PUT 为**整体替换语义**（`virtualModels` 传完整 map；未提供的字段保留现值），校验后写 targets.json 并热重载（引擎自动 reload，保留会话/熔断状态）
- `GET/PUT /api/anthropic-forward`：读写 8081 转发目标配置（见下节）
- dashboard：8080 卡片「✏️ 编辑配置」modal（虚拟模型增删/池成员 port/model/weight/retries/降级池编辑）、8081 卡片「✏️ 转发配置」modal（defaultPort + 按模型映射增删改）

### 8081 转发配置（anthropicForward）

8081 收到 `/v1/messages` 后翻译为 OpenAI 并转发，默认发往 `anthropicForwardPort`（8082），`model` 字段原样透传。通过 targets.json 顶层 `anthropicForward` 可**按模型配置转发目标**（聚合模型或非聚合模型）：

```json
"anthropicForward": {
  "defaultPort": 8082,
  "modelMap": {
    "sonnet": { "port": 8080, "model": "agg:sonnet" },
    "haiku":  { "port": 8082, "model": "claude-haiku-4.5" }
  }
}
```

- 客户端请求 `model` 命中 `modelMap` → 按配置的 `port` + `model` 转发（如 `sonnet` → 8080 聚合网关的 `agg:sonnet`，走权重/会话粘性/熔断全链路）；未命中 → 走 `defaultPort` + 原样模型（现状行为）
- `defaultPort` 缺省回退 `anthropicForwardPort`；`modelMap` 为空 = 全部走默认端口

实现：`aggregator.py`（`AggregatorEngine`，纯路由/熔断/统计逻辑，无网络 I/O）+ `server.py`（`_handle_aggregate_request` 分发 / `_aggregator_prober` 探测 / reload 钩子 / `/api/aggregate/status`、`/api/aggregate/config`、`/api/anthropic-forward` / lifespan 预初始化引擎）+ `config_store.py`（aggregate/aggregator 校验 + 顶层 `anthropicForward` 校验与保留加载）。

## 路径重写（_rewrite_upstream_path）

优先级：**handler 映射表 > routePrefix 重写 > 原样**。

- **handler 映射表**（`_HANDLER_PATH_MAP`）：如 copilot 的 `/v1/chat/completions` → `/chat/completions`、`/v1/models` → `/models`（上游无 /v1 前缀）
- **routePrefix 重写**：客户端 `/v1/xxx` → `routePrefix + /xxx`（如 openrouter `/v1/chat/completions` → `/api/v1/chat/completions`）
- **原样**：其余路径直接透传

## handler 分发

| handler | 行为 |
|---------|------|
| `passthrough` | 原样透传（注入认证 + 可选模型映射） |
| `copilot` | 模型映射（opus/sonnet/haiku → COPILOT_*_MODEL）+ `Copilot-Integration-Id` + body 清理 |
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

`http://127.0.0.1:8081/dashboard`（任意 target 端口 `/dashboard` 也可访问，`/api/*` 自动代理回 8081）：

- **分类栏**：聚合网关（8081）/ 破解网关（crack）/ 直连网关（free/paid）三组
- **卡片头**：端口强调条 + 名称 + 分类 badge + 请求数 + 展开箭头
- **详情区**：kv 元信息（含可粘贴 base_url）+ 流量统计块（总请求/成功率/运行时长 + 进度条）+ 模型白名单表格 + token 编辑块
- **模型编辑弹框**（✏️ 编辑模型）：iOS 滑动开关、总开关（全开/全关/部分开 indeterminate）、搜索框、**下拉下游真实模型列表**（编辑态自动 fetch `/models`，失败降级配置）
- **8081 统计**：`/v1/messages` 请求数与模型级统计（中间件记录）

设计契约见 [`DESIGN.md`](../DESIGN.md)。
