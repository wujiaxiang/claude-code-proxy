# Claude Code Proxy

让 Claude Code 通过第三方 API 运行，无需 Anthropic 官方账号。
**策略模式设计，新增 provider 只需注册一个函数。**

---

## 支持的 Provider（6 种）

| # | Provider | 后端 | 工具 | 说明 |
|---|----------|------|------|------|
| 1 | **anthropic** | LiteLLM → DeepSeek/Anthropic | ✅ ⭐ | 原生兼容，最快 |
| 2 | **gemini** | LiteLLM（`gemini/` 前缀） | ✅ | 内置 thoughtSignature 签名处理 |
| 3 | **gemini-openai** | httpx → Gemini `/v1beta/openai` | ✅ | Google OpenAI 兼容端点 |
| 4 | **openai** | LiteLLM → OpenAI/兼容 | ✅ | 通用 |
| 5 | **qclaw** | httpx/LiteLLM → QClaw 上游直连 | ✅ | 桌面版，自动解密 API Key |
| 6 | **copilot** | LiteLLM → GitHub Copilot Enterprise | ✅ | 企业账号，双端点，推理文本透传 |

---

## 快速启动

```bash
cd ~/claude-code-proxy
/usr/bin/python3 -m pip install fastapi uvicorn litellm httpx python-dotenv pydantic

# 编辑 .env（参考 .env.example）
# 然后启动：
PREFERRED_PROVIDER=anthropic /usr/bin/python3 server.py
```

---

## Provider 配置速查

### Anthropic（DeepSeek，推荐）
```ini
PREFERRED_PROVIDER=anthropic
ANTHROPIC_API_KEY=sk-你的Key
ANTHROPIC_BASE_URL=https://api.deepseek.com/anthropic
BIG_MODEL=deepseek-v4-pro
MEDIUM_MODEL=deepseek-v4-flash
SMALL_MODEL=deepseek-v4-flash
```

### Gemini 原生
```ini
PREFERRED_PROVIDER=gemini
GEMINI_API_KEY=AIza...
BIG_MODEL=gemini-2.5-flash
MEDIUM_MODEL=gemini-3.1-flash-lite
SMALL_MODEL=gemini-3.1-flash-lite
```

### Gemini OpenAI 兼容
```ini
PREFERRED_PROVIDER=gemini-openai
GEMINI_API_KEY=AIza...
GEMINI_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai
BIG_MODEL=gemini-2.5-flash
MEDIUM_MODEL=gemini-3.1-flash-lite
SMALL_MODEL=gemini-3.1-flash-lite
```

### OpenAI
```ini
PREFERRED_PROVIDER=openai
OPENAI_API_KEY=sk-你的Key
BIG_MODEL=gpt-4.1
MEDIUM_MODEL=gpt-4.1-mini
SMALL_MODEL=gpt-4.1-mini
```

### QClaw（上游直连 + 自动解密 API Key）

从 QClaw 桌面客户端本地存储自动解密 API Key，直连腾讯上游 `mmgrcalltoken.3g.qq.com`（OpenAI 兼容接口）。无需 19000 本地网关，无需手动填 Key。

```ini
PREFERRED_PROVIDER=qclaw
# API Key 自动从 QClaw 客户端解密，无需手动填写
# 如需手动指定：QCLAW_API_KEY=sk-xxxxxxxx
BIG_MODEL=pool-glm-5.2
MEDIUM_MODEL=pool-deepseek-v4-pro
SMALL_MODEL=pool-deepseek-v4-flash
```

**前置条件**：已安装 QClaw 桌面客户端并完成一次登录（保证本地存储中有加密的 API Key）。

**可用模型**（11 个）：
`modelroute`（Auto）· `pool-deepseek-v4-pro` · `pool-deepseek-v4-flash` · `pool-glm-5.2` · `pool-glm-5.2-night` · `pool-glm-5.1` · `pool-kimi-k2.7-code-highspeed` · `pool-kimi-k2.6` · `pool-minimax-m3` · `pool-minimax-m2.7` · `pool-hy3-preview`

> ⚠️ 上游要求请求必须包含 `system` 消息，代理会自动补全。上游拒绝 `python-httpx` 默认 User-Agent，代理会自动伪装为 `OpenAI/JS 6.39.1`。

### GitHub Copilot Enterprise（企业账号白嫖）

使用 bmw.ghe.com 企业账号 PAT 通过 LiteLLM 调用 Copilot API，**无需任何额外费用**。

```ini
PREFERRED_PROVIDER=copilot
COPILOT_GHE_TOKEN=github_pat_11ABNBEOY0...   # gh auth token --hostname bmw.ghe.com
COPILOT_GHE_HOST=copilot-api.bmw.ghe.com     # 默认值，按实际企业域名修改
COPILOT_INTEGRATION_ID=copilot-developer-cli  # 默认值，勿修改
COPILOT_BIG_MODEL=claude-sonnet-4.6           # Opus 系列 → 此模型
COPILOT_MEDIUM_MODEL=claude-sonnet-4.6        # Sonnet 系列 → 此模型
COPILOT_SMALL_MODEL=claude-haiku-4.5          # Haiku 系列 → 此模型
```

**获取 PAT**：
```bash
gh auth token --hostname bmw.ghe.com
```

**可用模型**（企业 Copilot `/models` API 实际返回，18 个 chat 模型）：

| 分类 | 模型 ID |
|------|---------|
| Claude | `claude-haiku-4.5` · `claude-sonnet-4.5` · `claude-sonnet-4.6` · `claude-opus-4.5` · `claude-opus-4.6` · `claude-opus-4.8` |
| GPT | `gpt-5.5` · `gpt-5.4` · `gpt-5.4-mini` · `gpt-5.3-codex` · `gpt-5-mini` · `gpt-4.1` · `gpt-4.1-2025-04-14` · `gpt-4o-mini` · `gpt-4o-mini-2024-07-18` · `gpt-3.5-turbo` · `gpt-3.5-turbo-0613` |
| Gemini | `gemini-2.5-pro` |

> ⚠️ `gpt-5.4-mini` 在 `/models` 列表中出现，但实际调用 `/chat/completions` 会被拒绝，不建议设为 SMALL_MODEL。

> **推理文本透传**：Claude 模型（如 `claude-sonnet-4.6`）会在流式响应中下发 `reasoning_text`，proxy 将其追加到正文 content 中输出，客户端可直接看到推理过程。

**双端点**：所有 provider 均开放两个端点：

| 端点 | 格式 | 适合 | 路由方式 |
|------|------|------|---------|
| `:8082/v1/messages` | Anthropic | Claude Code / Cline | LiteLLM（所有 provider 统一路径） |
| `:8082/v1/chat/completions` | OpenAI | 任意 Agent / 自定义工具 | LiteLLM（所有 provider 统一路径） |

```bash
# Anthropic 格式（Claude Code 默认）
curl http://localhost:8082/v1/messages \
  -H "x-api-key: dummy" -H "anthropic-version: 2023-06-01" \
  -d '{"model":"claude-sonnet-20241022","messages":[{"role":"user","content":"hi"}],"max_tokens":100}'

# OpenAI 格式（任意 provider 均可用）
curl http://localhost:8082/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"claude-sonnet-20241022","messages":[{"role":"user","content":"hi"}]}'
```

---

## 模型映射（3 级梯度）

| Claude Code 请求模型 | 映射到 | 环境变量 |
|---------------------|--------|---------|
| Opus 系列 | `BIG_MODEL` | `COPILOT_BIG_MODEL`（copilot 专用） |
| Sonnet 系列 | `MEDIUM_MODEL` | `COPILOT_MEDIUM_MODEL`（copilot 专用） |
| Haiku 系列 | `SMALL_MODEL` | `COPILOT_SMALL_MODEL`（copilot 专用） |

基于**子串包含**匹配，短名 `sonnet` / `haiku` / `opus` 也有效。

---

## Claude Code 配置

```json
// ~/.claude/settings.json
{
  "env": {
    "ANTHROPIC_BASE_URL": "http://127.0.0.1:8082",
    "ANTHROPIC_API_KEY": "dummy",
    "ANTHROPIC_AUTH_TOKEN": "dummy",
    "API_TIMEOUT_MS": "3000000",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": 1,
    "CLAUDE_CODE_EFFORT_LEVEL": "low",
    "CLAUDE_CODE_SUBAGENT_MODEL": "sonnet"
  }
}
```

切换 provider 只需改 `.env` 一行 `PREFERRED_PROVIDER`，Claude Code 配置不用动。

---

## 调试

```bash
# 开启详细日志，同时写入文件
DEBUG=true LOG_FILE=/tmp/proxy.log PREFERRED_PROVIDER=copilot python server.py

# 实时跟踪
tail -f /tmp/proxy.log
```

| 变量 | 默认 | 说明 |
|------|------|------|
| `DEBUG` | `false` | `true` 启用详细日志（请求/响应/模型映射） |
| `LOG_FILE` | 空 | 文件日志路径（`DEBUG=false` 也生效，会记录 warning/error） |
| `LOG_RETENTION_DAYS` | `7` | 轮转日志保留天数，过期自动清理 |
| `LOG_ROTATE_WHEN` | `midnight` | 日志轮转周期单位（如 `midnight`、`H`） |
| `LOG_ROTATE_INTERVAL` | `1` | 轮转周期步长 |

---

## 测试

统一测试套件 [test_suite.py](test_suite.py)，整合自历史三个文件（`test_claude_api.py` / `test_messages_endpoint.py` / `tests.py`），覆盖翻译链路 `/v1/messages` 和透传链路 `/v1/chat/completions`。

```bash
# 启动服务后运行全部测试（15 大类，38 个测试点）
python test_suite.py

# 分场景运行
python test_suite.py --simple       # 基础场景：连通性/模型名/system/消息格式
python test_suite.py --tools        # 工具 + thinking + 错误处理
python test_suite.py --oai          # 仅 OpenAI 透传端点
python test_suite.py --no-streaming # 跳过流式测试
```

环境变量可覆盖默认模型名（QClaw 模式用 `pool-*`）：

```bash
PREFERRED_PROVIDER=qclaw \
BIG_MODEL=pool-glm-5.2 \
MEDIUM_MODEL=pool-deepseek-v4-pro \
SMALL_MODEL=pool-deepseek-v4-flash \
python test_suite.py
```

测试覆盖：连通性、模型名还原、System Prompt、流式 SSE 事件序列、多轮对话、参数透传、Stop Sequences、Tools、Tool Choice、**Thinking**（adaptive/enabled/budget/历史 422 bug/工具组合）、Token 计数、错误处理、性能基准、**OpenAI `/v1/chat/completions` 透传端点**（11 个场景）。

---

## 架构

```
Claude Code / Cline
  ──Anthropic 格式──▶ :8082/v1/messages
                         │
                         ├─ anthropic     → LiteLLM → DeepSeek / Anthropic
                         ├─ gemini        → LiteLLM → Gemini 原生
                         ├─ gemini-openai → httpx   → Gemini OpenAI 端点
                         ├─ openai        → LiteLLM → OpenAI / 兼容
                         ├─ qclaw         → LiteLLM → mmgrcalltoken.3g.qq.com（上游直连）
                         └─ copilot       → LiteLLM → Copilot Enterprise ──▶ Claude/GPT

任意 Agent / 自定义工具
  ──OpenAI 格式──▶ :8082/v1/chat/completions
                         │
                         ├─ anthropic / gemini → LiteLLM（翻译模式）
                         └─ qclaw / openai / copilot / gemini-openai → httpx（透传模式，直连上游）
```

> **注：** `/v1/chat/completions` 在 qclaw/openai/copilot/gemini-openai 模式下走 httpx 透传（绕过 LiteLLM），其余走 LiteLLM 翻译。

完整配置参考 `.env.example`

---

## QClaw 上游直连方案（设计文档）

### 三层架构与放弃 19000 网关的原因

QClaw 有三层架构：
1. **Layer 1**：QClaw Electron 应用（`127.0.0.1:19000` auth gateway）
2. **Layer 2**：openclaw runtime（随机端口，如 60227/49613）
3. **Layer 3**：上游 LLM 服务（`mmgrcalltoken.3g.qq.com`）

**放弃 19000 网关**：该端口使用 Ed25519 设备签名（私钥请求签名）认证，只有 QClaw 进程树内的进程才能访问。客户端外无法模拟，反复尝试导致 9002 "该功能暂不可用" 错误。

**放弃动态端口**：60227 等动态端口是 agent 级别会话接口，非 LLM 级别，且每次启动端口变化。

**最终方案**：用 GetQClawAPIKey 方法（参考 `github.com/wenjiazhu1980/GetQClawAPIKey`），从 QClaw 客户端本地存储解密 API Key，直连 Layer 3 上游。

### API Key 解密链路（Windows）

```
%APPDATA%\QClaw\Local State
  └─ os_crypt.encrypted_key (base64)
       ├─ 前缀 "DPAPI" (5 字节)
       └─ DPAPI blob → CryptUnprotectData() → AES-256 密钥 (32 字节)

%APPDATA%\QClaw\app-store.json
  └─ authGateway.providers.qclaw.apiKey.cipherText (base64)
       ├─ 前缀 "v10" (3 字节)
       ├─ nonce (12 字节)
       ├─ 密文 (变长)
       └─ GCM tag (16 字节)
       → AES-256-GCM 解密 → API Key (sk-...)
```

代码实现：`server.py` 中 `_dpapi_unprotect()` + `_decrypt_qclaw_api_key()`。

### 关键约束（踩坑记录）

| 问题 | 原因 | 解决方案 |
|------|------|---------|
| Python urllib 返回 400 | 系统代理干扰 | httpx 添加 `trust_env=False` |
| httpx 返回 400 "invalid request" | 上游拒绝 `python-httpx` 默认 User-Agent | 伪装为 `User-Agent: OpenAI/JS 6.39.1` |
| 只传 user 消息返回 400 | 上游要求必须同时有 system + user | 代理自动补全 system 消息 |
| 19000 网关返回 9002 | Ed25519 设备签名认证，客户端外无法模拟 | 放弃 19000，直连上游 |
| Anthropic 专属字段导致 400 | `thinking`/`reasoning_effort`/`metadata` 等非标准字段 | `_clean_qclaw_body()` 白名单过滤 |

### 排查指南

**1. API Key 解密失败**
```
⚠️ QClaw API Key not available
```
- 确认 QClaw 客户端已安装并登录过
- 检查 `%APPDATA%\QClaw\app-store.json` 是否存在 `authGateway.providers.qclaw.apiKey`
- 检查 `%APPDATA%\QClaw\Local State` 是否存在 `os_crypt.encrypted_key`
- 手动指定：`.env` 中设置 `QCLAW_API_KEY=sk-xxxx`

**2. 启动诊断返回非 200**
```
startup diag: QClaw upstream = 400
```
- 400 = 请求格式问题（检查 User-Agent / system 消息）
- 401/403 = API Key 过期，重新登录 QClaw 客户端刷新 Key

**3. 重新解密 API Key**
如果 Key 过期，打开 QClaw 客户端重新登录，然后重启 proxy 即可自动重新解密。也可手动解密验证：
```python
from server import _decrypt_qclaw_api_key
key = _decrypt_qclaw_api_key()
print(key)
```

### 关键文件位置

| 文件 | 用途 | 解密依赖 |
|------|------|---------|
| `%APPDATA%\QClaw\app-store.json` | 加密的 API Key + JWT + 用户信息 | 需配合 Local State 解密 |
| `%APPDATA%\QClaw\Local State` | DPAPI 保护的 AES-256 密钥 | 需当前用户 DPAPI |
| `~/.qclaw/qclaw.json` | QClaw 配置（authGatewayBaseUrl, guid） | 明文 |
| `~/.qclaw/openclaw.json` | openclaw runtime 配置（动态端口, auth token） | 明文 |

`app-store.json` 中加密的字段：
- `authGateway.providers.qclaw.apiKey` → API Key（sk-...）
- `secure.jwtToken` → JWT 令牌
- `secure.userInfo` → 用户信息（JSON：loginKey, guid, userId）

### server.py 关键函数

| 函数 | 作用 |
|------|------|
| `_decrypt_qclaw_api_key()` | API Key 解密入口，环境变量优先，否则从本地存储解密 |
| `_dpapi_unprotect()` | Windows DPAPI 解密（ctypes 调用 CryptUnprotectData） |
| `_clean_qclaw_body()` | 请求体白名单过滤，移除 Anthropic 专属字段 |
| `_qclaw_provider()` | LiteLLM provider 策略（翻译链路用） |
| `_passthrough_to_qclaw()` | httpx 透传（透传链路 + 异常 fallback 用） |
| `get_http_client()` | 全局 httpx 连接池（trust_env=False，绕过系统代理） |

### 请求链路详解

**透传链路**（`/v1/chat/completions`，PREFERRED_PROVIDER=qclaw）：
```
客户端 → server.py → _clean_qclaw_body() → 补 system 消息 → httpx(trust_env=False)
  → POST https://mmgrcalltoken.3g.qq.com/aizone/v1/chat/completions
  → Headers: Authorization: Bearer sk-xxx, User-Agent: OpenAI/JS 6.39.1
  → 响应原样返回给客户端
```

**翻译链路**（`/v1/messages`，Anthropic 格式）：
```
客户端 → server.py → convert_anthropic_to_litellm() → _qclaw_provider()
  → litellm.acompletion(api_key=sk-xxx, api_base=mmgrcalltoken, extra_headers={User-Agent})
  → 响应转换为 Anthropic 格式返回给客户端
```

两条链路都会：自动补 system 消息、清理非标准字段、伪装 User-Agent。

### 开发过程中的关键发现

1. **19000 网关认证机制**：Ed25519 设备签名（私钥请求签名），不是简单的 header/token。之前能访问是因为 QClaw 客户端启动时建立了 auth session，用了临时 token `__QCLAW_AUTH_GATEWAY_MANAGED__`。客户端外进程无法模拟。

2. **动态端口（60227 等）的本质**：是 openclaw runtime 的 agent 会话接口，不是 LLM 接口。每次 QClaw 启动端口都会变，且只接受 agent 级别的会话请求，不能用于 `/chat/completions`。

3. **上游 IP**：`mmgrcalltoken.3g.qq.com` 解析为 `60.29.254.103`（腾讯）。

4. **流式响应格式**：上游 SSE 流式响应包含 `reasoning_content` 字段（推理内容），代理在翻译链路中将其转换为 Anthropic 的 `thinking` block。

5. **User-Agent 黑名单**：上游拒绝 `python-httpx/x.x.x` 默认 UA，返回 400 "invalid request"。必须伪装为 `OpenAI/JS 6.39.1` 或 `node-fetch` 等。这是通过对比 Python httpx（400）和 Node.js fetch（200）发现的。

6. **系统代理干扰**：Python urllib 受系统代理影响返回 400，Node.js fetch 不受影响。解决方案是 httpx 添加 `trust_env=False`。

7. **Chrome 风格 os_crypt 加密**：QClaw 用 Electron 的 safeStorage，Windows 上是 DPAPI 保护 AES-256-GCM 密钥，macOS 上是 Keychain 密码派生 AES-128-CBC（参考 GetQClawAPIKey 项目的 `decryptChromiumV10()`）。
