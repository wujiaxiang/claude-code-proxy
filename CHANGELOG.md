# Changelog

## 2026-07-16 — qclaw-local provider 方案落地 + 19000 网关机制完整破解

### 新增

#### `qclaw-local` provider（方案2：寄生代理）

- 新增 `qclaw_inject.js`：通过 Electron inspector 在 QClaw 主进程内注入 HTTP 服务器（19001 端口），使用 QClaw 自带的 axios 实例（含签名拦截器）转发请求到 19000 网关
- `server.py` 新增 `qclaw-local` provider 支持：
  - `QCLAW_LOCAL_BASE_URL = http://127.0.0.1:19001`
  - 透传链路和 LiteLLM provider 链路均支持 qclaw-local
  - 启动诊断自动选择对应 base URL
- 架构：`client → server.py(8083) → 19001(寄生服务器) → 19000(QClaw 网关) → 上游 LLM`
- 测试通过：非流式 ✅、流式（SSE）✅、Anthropic 格式 ✅

#### 19000 网关进程来源检查机制完整破解

- **HMAC-SHA256 签名算法完全破解**（密钥 + payload 格式 + 算法验证通过）
- **确认网关采用 OS 级 PID 反查机制**，通过 koffi FFI 直接调用 IPHLPAPI.DLL 的 `GetExtendedTcpTable`
- **结论：独立签名不可行**，PID 由 Windows 内核管理，用户态无法伪造
- 详见 `QCLAW_19000_GATEWAY_REVERSE.md`（完整逆向调研报告）

### 变更

- `server.py` 中 qclaw-local 的注释更新为"寄生转发服务器"，引用 `qclaw_inject.js`
- `.gitignore` 新增对临时调研脚本（`_*` 前缀）和调研目录的忽略规则

### 清理

- 删除 89 个临时调查脚本（`_*.js`/`_*.py`/`_*.txt`）和 2 个调研目录（`_app_asar_extracted/`、`_asar_regions/`）
- 将方案2 核心脚本从 `_reinject_v3.js` 重命名为 `qclaw_inject.js` 并加生产级注释

### 使用方法

```bash
# 1. QClaw 需以 --inspect=9229 模式启动
# 2. 注入寄生转发服务器
node qclaw_inject.js

# 3. 启动代理（本地调试用 8083 端口避免与 8082 冲突）
$env:PREFERRED_PROVIDER = "qclaw-local"
$env:PORT = "8083"
python server.py
```

---

## 2026-07-16 — QClaw 19000 端口签名机制逆向分析（调查记录）

### 概述

为推进 `qclaw-local` provider（走 19000 端口本地网关）方案，对 QClaw v0.2.33.617 的 19000 端口 HTTP API 签名认证机制进行了系统逆向分析。本次为**调查记录**，尚未产出代码变更；签名算法的具体实现仍待破解（V8 字节码保护）。

### 关键发现

#### 1. 19000 端口端点认证差异

| 端点 | 方法 | 认证 | 返回 |
|------|------|------|------|
| `/proxy/llm/models` | GET | **无需认证** | 200 + 模型列表 |
| `/proxy/llm/chat/completions` | POST | **需要签名头部** | 403 / 9002（缺签名时） |

- `/proxy/llm/models` 即使不带任何认证头部也返回 200，列出所有可用模型（modelroute, pool-hy3-preview, pool-deepseek-v4-pro/flash, pool-glm-5.2/5.1, pool-kimi-k2.7-code-highspeed, pool-kimi-k2.6, pool-minimax-m3/m2.7 等）
- `/proxy/llm/chat/completions` 必须携带完整的签名头部集合，否则网关返回 403 + 错误码 9002

#### 2. 签名算法：HMAC-SHA256（非 Ed25519）

**重要纠正**：早期文档（含 2026-07-09 条目）记载"19000 端口使用 Ed25519 设备签名认证，无法绕过"。本次逆向证实该结论不准确：

- **Ed25519** 仅用于 WebSocket 握手时的设备身份认证（`noble-ed25519` 库），不是 HTTP API 调用的签名机制
- **HTTP API 签名** 使用 **HMAC-SHA256**（在字节码中定位到 `createHmac` 调用，位置 1423170）
- HMAC-SHA256 是对称签名，只要拿到密钥即可伪造合法签名，比 Ed25519 容易突破

#### 3. 签名相关 HTTP 头部

通过字符串扫描 `out/main/index.cjsc` 定位到以下签名头部集合：

| 头部 | 用途推测 |
|------|----------|
| `x-signature` | 主签名值（HMAC 输出） |
| `x-sign-signature` | 备用/二级签名 |
| `x-server-timestamp` | 服务端时间戳（时间同步用） |
| `x-client-timestamp` | 客户端时间戳（防重放） |
| `x-nonce` | 一次性随机数（防重放） |
| `x-qclaw-version` | 客户端版本号 |
| `x-auth-version` | 认证协议版本 |
| `x-token` | 网关 token（gateway.auth.token） |
| `x-conversation-message-id` | 会话消息 ID |
| `x-media-attachment` | 媒体附件标识 |

#### 4. 签名状态机

在字节码中识别出 `llmSignature` 相关的状态字符串：

- `llm_signature_ok` — 签名验证通过
- `llm_signature_time_sync_failed` — 客户端/服务端时间同步失败
- `llm_signature_missing_header` — 缺少必要签名头部
- `llm_signature_inject_failed` — 签名注入失败

#### 5. 关键函数名（位于 `out/main/index.cjsc`）

| 函数名 | 字节码内偏移 | 推测职责 |
|--------|-------------|----------|
| `signRequestBody` | 202554 | 对请求体执行签名 |
| `injectLlmSignature` | 212961 | 将签名头部注入 HTTP 请求 |
| `buildRequestSign` | 208290 | 构造待签名 payload |
| `buildUpstreamHeaders` | 208586 | 构造发往上游的完整头部集合 |

#### 6. 签名代码保护机制

- 签名实现位于 `out/main/index.cjsc`（6,924,424 bytes），是 **V8 字节码文件**（非明文 JS）
- 字符串常量可读（函数名、头部名、状态字符串），但函数体为 V8 bytecode，无法直接还原源码
- asar 包内其他 `.js` 文件均不包含签名逻辑（已全量扫描确认）
- asar 包总大小 143,256,891 bytes，包含 7555 个文件

### 调查方法

1. **asar 解包**：用 Python 手动解析 asar 格式（pickle header + JSON header + content），提取文件清单和偏移
2. **字符串归属定位**：编写 `_find_string_owner.py` 扫描所有 asar 内文件，确认签名字符串仅出现在 `out/main/index.cjsc`
3. **上下文提取**：对每个关键字符串提取前后 1KB 上下文，确认相邻符号关系
4. **端点探测**：用 QClaw 自带的 `node.exe`（v22.22.3）直接请求 19000 端口，验证认证差异
5. **openclaw CLI 探查**：运行 `openclaw doctor --generate-gateway-token` 生成独立 token，但证实该 token 与 QClaw 运行时使用的 token 不同（QClaw 通过 `OPENCLAW_GATEWAY_TOKEN` 环境变量注入运行时 token）

### 待解决问题

1. **HMAC 密钥来源**：HMAC-SHA256 的密钥是固定值、设备派生值、还是从 gateway.auth.token 派生？需逆向 V8 字节码或运行时 hook 才能确认
2. **签名 payload 构造**：待签名字符串的具体拼接格式（哪些头部参与签名、顺序如何、是否包含请求体 hash）
3. **时间同步协议**：`x-server-timestamp` 与 `x-client-timestamp` 的校验逻辑（是否要求服务端先返回时间戳才能签名）
4. **V8 字节码反编译**：需用 `v8-decompiler` 或运行时 `--print-bytecode` 才能还原 `signRequestBody` 等函数的实现

### 替代突破方案（来自 Google 调研）

- **方案 A**：获取 `gateway.auth.token` + Ed25519 签名（适用于 WebSocket 握手，非 HTTP API）
- **方案 B**：MITM 抓包截获 QClaw 客户端发往 19000 的合法请求，复制 `device` 结构体在 5 分钟时间窗内重放
- **方案 C**：修改 `~/.openclaw/openclaw.json` 将 `gateway.auth.mode` 从 `ed25519`/`strict` 降级为 `none`/`token`（需文件写权限且重启网关）
- **方案 D**（本次新增）：直接逆向 HMAC-SHA256 签名算法，在代理端自行构造合法签名头部

### 关键路径

- QClaw 安装目录：`C:\Program Files\QClaw\v0.2.33.617\`
- asar 包：`C:\Program Files\QClaw\v0.2.33.617\resources\app.asar`
- 签名代码：`out/main/index.cjsc`（asar 内偏移 51,313,148，绝对偏移 53,091,540）
- 配置存储：`%APPDATA%\QClaw\app-store.json`（含加密的 `authGateway.providers.qclaw.apiKey`）
- openclaw 配置：`~/.openclaw/openclaw.json`（独立实例，与 QClaw 运行时 token 不同）
- QClaw 自带 Node：`C:\Program Files\QClaw\v0.2.33.617\resources\node\node.exe`（v22.22.3）
- QClaw 自带 openclaw：`C:\Program Files\QClaw\v0.2.33.617\resources\openclaw\node_modules\openclaw\openclaw.mjs`

### 文件变更

本次为纯调查记录，**无代码变更**。以下为调查过程中产生的临时分析脚本（待清理）：

| 文件 | 用途 |
|------|------|
| `_parse_asar.py` | 解析 asar 格式 |
| `_find_auth_gateway_v2.py` | 搜索 auth gateway 模式 |
| `_find_string_owner.py` | 定位签名字符串归属文件 |
| `_extract_cjsc.py` | 提取并分析 index.cjsc |
| `_extract_key_regions.py` | 提取关键区域到独立文件 |
| `_find_llm_signature.py` | 搜索 llmSignature 函数 |
| `_find_sign_impl.py` | 搜索签名实现 |
| `_extract_sign_impl.py` | 提取 signRequestBody / injectLlmSignature |
| `_search_signing_all.py` | 全 asar 搜索签名模式 |
| `_test_19000_node.js` | 用 node.exe 测试 19000 端口 |
| `_test_sign_with_qclaw.js` | 尝试加载 QClaw 内部模块 |
| `_app_asar_extracted/` | 提取的 cjsc 和上下文文件 |

---

## 2026-07-15 — 本地 token 估算（tiktoken）

### 概述

QClaw 上游网关不返回 `usage` 字段，导致 Claude Code 等客户端无法显示用量。引入 `tiktoken` 在代理端本地估算 input/output tokens，替换原本硬编码的 0 值。

### 变更

- **新增依赖** `tiktoken>=0.7.0`（[pyproject.toml](file:///c:/Users/Administrator/claude-code-proxy-main/pyproject.toml)）
- **新增工具函数**（[server.py L44-L155](file:///c:/Users/Administrator/claude-code-proxy-main/server.py#L44-L155)）：
  - `_get_tokenizer(model)` — 缓存 cl100k_base tokenizer 实例
  - `_extract_text_from_content(content)` — 从 str / list[dict] 抽取纯文本（兼容 Anthropic content blocks 与 OpenAI message content）
  - `_estimate_messages_tokens(messages, model, system, tools)` — 估算输入 tokens（参考 OpenAI 公式：每条 msg 4 + role + text，加 3 priming）
  - `_estimate_text_tokens(text, model)` — 估算输出 tokens
- **替换硬编码 0 值**（5 个位置）：
  - `_convert_oai_to_anthropic` — fallback 路径的 `Usage(input_tokens=0, output_tokens=0)`
  - `convert_litellm_to_anthropic` — QClaw 网关不返回 usage 时估算 prompt/completion tokens
  - `handle_streaming` — `message_start.input_tokens` 用请求 messages 估算；`message_delta.output_tokens` 在 early-exit 和 final 两个路径都用累积响应文本估算
  - `/v1/chat/completions` 透传响应 — 缺失 usage 时注入估算的 `prompt_tokens` / `completion_tokens` / `total_tokens`
  - `/v1/messages/count_tokens` — ImportError fallback 从硬编码 1000 改为 tiktoken 估算
- **`handle_qclaw_streaming`** — output_tokens 估算从 `len(accumulated.split())`（按空格分词）改为 tiktoken

### 估算精度

- DeepSeek/GLM/Kimi/MiniMax/Claude 均使用 `cl100k_base`（经验上误差 ±10%）
- 仅做客户端展示用途，不影响上游计费

### 文件变更

| 文件 | 变更 |
|------|------|
| `pyproject.toml` | 新增 tiktoken 依赖 |
| `server.py` | 新增估算工具函数 + 替换 5 处硬编码 0 值 |

---

## 2026-07-09 (v2) — 测试套件整合

### 概述

将根目录三个历史测试文件整合为一个统一测试套件 `test_suite.py`，消除功能重叠，统一 QClaw 模型名适配。

### 变更

- **新增 `test_suite.py`**（869 行）：整合 `test_claude_api.py` / `test_messages_endpoint.py` / `tests.py`
  - 15 大类测试场景，38 个测试点
  - 合并 `test_messages_endpoint.py` 的 thinking 场景（adaptive/enabled/budget/历史 422 bug/工具组合）和 SSE 事件序列验证
  - 合并 `tests.py` 的 argparse 支持（`--simple` / `--tools` / `--oai` / `--no-streaming`）
  - 统一根据 `PREFERRED_PROVIDER` 动态选择模型名
- **删除 `test_claude_api.py`**（528 行）
- **删除 `test_messages_endpoint.py`**（262 行）
- **删除 `tests.py`**（691 行）
- **`README-zh.md`** 更新测试章节，反映整合后的使用方式

### 文件变更

| 文件 | 变更 |
|------|------|
| `test_suite.py` | 新增，整合三个历史测试文件 |
| `test_claude_api.py` | 删除 |
| `test_messages_endpoint.py` | 删除 |
| `tests.py` | 删除 |
| `README-zh.md` | 更新测试章节 |

---

## 2026-07-09 — QClaw 上游直连 + API Key 自动解密

### 概述

彻底放弃 19000 本地网关方案（Ed25519 设备签名认证无法绕过），改用 GetQClawAPIKey 方法：从 QClaw 客户端本地存储解密 API Key，直连上游 `mmgrcalltoken.3g.qq.com` OpenAI 兼容接口。支持指定具体模型（如 `pool-deepseek-v4-flash`），流式和非流式均正常。

### 修复

- **`QCLAW_BASE_URL`** 默认值从 `http://127.0.0.1:19000/proxy/llm` 改为 `https://mmgrcalltoken.3g.qq.com/aizone/v1`
- **新增 `_decrypt_qclaw_api_key()`**：从 `%APPDATA%\QClaw\Local State` 读取 DPAPI 保护的 AES-256 密钥，用 AES-256-GCM 解密 `app-store.json` 中的 v10 密文，得到 `sk-...` API Key
- **新增 `_dpapi_unprotect()`**：Windows DPAPI 解密（ctypes 调用 CryptUnprotectData）
- **移除所有 `__QCLAW_AUTH_GATEWAY_MANAGED__`** 引用，改用解密的真实 API Key
- **移除 `x-agent-id` 请求头**（上游不需要）
- **移除 `Connection: close`**（不再需要避免网关缓存，恢复 keepalive）
- **`max_keepalive_connections`** 从 0 改为 10（恢复连接复用）
- **所有 httpx 客户端添加 `trust_env=False`**（绕过系统代理，解决 Python urllib/httpx 因系统代理导致请求失败的问题）
- **移除 403/9002 专属重试逻辑**（直连上游不会有 9002）
- **保留 `User-Agent: OpenAI/JS 6.39.1`**（上游拒绝 python-httpx 默认 UA，返回 400 "invalid request"）
- **`.env`** 移除旧 19000 URL，新增可选 `QCLAW_API_KEY` 环境变量覆盖
- **`test_claude_api.py`** 适配 QClaw 模式：透传链路用 pool-* 模型名，根据 PREFERRED_PROVIDER 动态选择

### 文件变更

| 文件 | 变更 |
|------|------|
| `server.py` | API Key 解密 + 上游直连 + trust_env + User-Agent + 清理 9002 逻辑 |
| `.env` | 移除旧网关 URL，新增可选 API Key |
| `test_claude_api.py` | 适配 QClaw 模式，动态模型名 |
| `README-zh.md` | 新增 QClaw 上游直连设计文档 + 解密链路 + 排查指南 |

### 背景

- QClaw 19000 端口使用 Ed25519 设备签名认证，客户端外无法模拟
- 60227 等动态端口是 agent 级别会话接口，非 LLM 级别
- 上游 `mmgrcalltoken.3g.qq.com` 是标准 OpenAI 兼容接口，用解密的 API Key 即可调用
- Python urllib 受系统代理影响返回 400，Node.js fetch 和 httpx(trust_env=False) 正常

---

## 2026-07-08 (v2) — QClaw body 字段清理 + 死代码修复

### 概述

排查发现 QClaw 网关非常稳定，问题出在代理把客户端请求的 body 原封不动透传给网关，非标准字段（如 `thinking`、`reasoning_effort`、`metadata` 等 Anthropic 专属参数）导致网关返回 9002。

### 修复

- **新增 `_clean_qclaw_body()` 函数**：白名单过滤，只保留标准 OpenAI chat completion 字段
- **qclaw 透传路径**：发送前调用 `_clean_qclaw_body()` 清理 body（line 1721）
- **`_qclaw_provider`**：加强 litellm 请求清理，移除 `thinking`/`reasoning`/`reasoning_effort`/`extra_body`/`provider_specific_fields`/`custom_llm_provider`/`model_info`（lines 470-474）
- **修复死代码**：`/v1/chat/completions` 9002 fallback 路径中不再引用已删除的 `_qclaw_fallback_chat_completion`，改用直连 httpx（lines 1854-1872）
- **调试日志**：透传前打印 body keys 方便排查（line 1722）

### 文件变更

| 文件 | 变更 |
|------|------|
| `server.py` | +30 / -6 |

---

## 2026-07-08 — QClaw 透传 9002 修复

### 概述

修复 QClaw 透传路径中 litellm keep-alive 连接触发网关 9002 后，透传 fallback 也被污染的问题。

### 根因

litellm 先用 keep-alive 连接发请求 → 网关返回 9002 → 网关在进程/IP 级别缓存 9002 → 紧接着的透传 fallback（同一秒内）也中 9002。

### 修复

- **qclaw 透传 header 加 `Connection: close`**：避免 keep-alive 复用被污染的连接 (line 1643)
- **流式透传路径**：每次重试新建 `httpx.AsyncClient` + `asyncio.sleep(0.5)` 让网关缓存过期 + 新增 403 重试逻辑 (lines 1682-1709)
- **非流式透传路径**：重试前 `asyncio.sleep(0.5)` 让网关缓存过期 (lines 1714-1716)

### 文件变更

| 文件 | 变更 |
|------|------|
| `server.py` | +13 / -8 |

---

## 2026-07-06 — 全 provider 透传扩展

### 概述

将 `/v1/chat/completions` 的透传模式从仅 qclaw 扩展到 `openai`、`gemini-openai`、`copilot` 四个 provider。
这些 provider 的后端本身即为 OpenAI 兼容格式，无需经过 LiteLLM 翻译。

### 改动

- **透传 provider 列表扩展**：`qclaw` → `qclaw / openai / copilot / gemini-openai`
- **openai 透传**：去掉 `openai/` 前缀，注入 `Authorization: Bearer`，转发到 `OPENAI_BASE_URL`
- **copilot 透传**：模型映射（haiku/sonnet/opus → COPILOT_*_MODEL），注入 `Copilot-Integration-Id`，清理空 content 和无效 tool_choice
- **gemini-openai 透传**：去掉 `gemini/` 前缀，注入 `Authorization: Bearer`，转发到 `GEMINI_BASE_URL`
- **保留翻译模式**：`anthropic` 和 `gemini`（原生 API）仍走 LiteLLM 翻译
- 通用：所有透传 provider 共享全局 httpx 连接池 + 3 次重试

### 文件变更

| 文件 | 变更 |
|------|------|
| `server.py` | +64 / -30 |

---

## 2026-07-06 — QClaw 透传 + 连接池 + 模型注册扩展

### 概述

为支持 QClaw 网关作为后端 provider，对 `server.py` 进行了 +132/-14 行改动。
核心目标：qclaw 模式下 `/v1/chat/completions` 直接透传请求到 QClaw 网关，绕过 litellm，
同时修复模型映射和注册问题。

### 新增

- **全局 httpx 连接池** (`get_http_client()` + FastAPI `lifespan`)
  - 复用连接，避免长时间运行后端口/连接泄漏
  - 最多 50 并发连接，20 个 keepalive，超时 300s
  - 应用关闭时自动清理

- **`/v1/chat/completions` qclaw 透传模式** (+68 行)
  - qclaw 模式下直接转发请求体给 QClaw 网关，不做模型映射和协议转换
  - 自动去掉 `qclaw/` 前缀
  - 自动补 system message（QClaw 网关强制要求）
  - 连接错误 / 网关 5xx 自动重试 3 次
  - 支持 streaming 和非 streaming 两种模式

- **QClaw 模型注册扩展** (3 → 11 个)
  - 新增: `pool-hy3-preview`, `pool-glm-5.2`, `pool-glm-5.2-night`, `pool-glm-5.1`,
    `pool-kimi-k2.7-code-highspeed`, `pool-kimi-k2.6`, `pool-minimax-m3`, `pool-minimax-m2.7`

### 修复

- **`validate_model_field` 模型映射** — qclaw/copilot 模式下不再错误添加 `openai/` 前缀
  - 影响 `MessagesRequest` 和 `TokenCountRequest` 两个类的 `haiku`/`sonnet`/`opus` 映射逻辑
  - qclaw 模式现在直接使用模型名，不加 provider 前缀

### 文件变更

| 文件 | 变更 |
|------|------|
| `server.py` | +132 / -14 |

### 合并信息

- 分支: `feat/qclaw-passthrough` → `main`
- 合并方式: `--allow-unrelated-histories`（两条历史线无共同祖先）
- 冲突解决: 采用 `feat/qclaw-passthrough` 分支的 `server.py`
