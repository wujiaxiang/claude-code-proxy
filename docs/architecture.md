# 多端口架构详解

> 本项目是**多端口架构**：每个上游供应商一个独立监听端口，由 `targets.json` 配置驱动。
> 所有端口共享统一的 HTTP 解析 / 转发 / 429 翻译 / 重试逻辑。**端口-供应商绑定由 `targets.json` 定义，无需修改 server.py。**

## 端口总览

| 端口 | 供应商 | 分类 | handler | 协议 | 用途 |
|------|--------|------|---------|------|------|
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

- **分类**：`crack`（破解获取 token，注入 secrets.json）/ `free`（免费，透传客户端 key）/ `paid`（收费，透传客户端 key）
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
