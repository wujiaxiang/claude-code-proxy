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
| 5 | **qclaw** | LiteLLM → QClaw 本地网关 | ✅ | 桌面版 |
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

### QClaw
```ini
PREFERRED_PROVIDER=qclaw
BIG_MODEL=pool-deepseek-v4-pro
MEDIUM_MODEL=pool-deepseek-v4-pro
SMALL_MODEL=pool-deepseek-v4-flash
BIG_REASONING=high
MEDIUM_REASONING=low
SMALL_REASONING=low
```

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

```bash
# 启动服务后运行完整兼容性测试（含 OpenAI 端点）
python test_claude_api.py
```

测试覆盖：连通性、模型名还原、System Prompt、流式响应、多轮对话、参数透传、Stop Sequences、Tools、Token 计数、**OpenAI `/v1/chat/completions` 端点**（12 个场景）。

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
                         ├─ qclaw         → LiteLLM → QClaw 网关
                         └─ copilot       → LiteLLM → Copilot Enterprise ──▶ Claude/GPT

任意 Agent / 自定义工具
  ──OpenAI 格式──▶ :8082/v1/chat/completions
                         └─ 所有 provider → LiteLLM → 对应上游（统一路径）
```

> **注：** `gemini-openai` 是唯一走 httpx 的 provider；其余 5 种均通过 LiteLLM 统一路由。

完整配置参考 `.env.example`
