# Anthropic API Proxy for Gemini, OpenAI & Copilot Enterprise 🔄

**Use Anthropic clients (like Claude Code) with Gemini, OpenAI, Copilot Enterprise, or direct Anthropic backends.** 🤝

A proxy server that lets you use Anthropic clients with multiple backends, all via a unified strategy pattern. Also exposes an OpenAI-compatible `/v1/chat/completions` endpoint for other agents and tools. 🌉


![Anthropic API Proxy](pic.png)

## Quick Start ⚡

### Prerequisites

- OpenAI API key 🔑
- Google AI Studio (Gemini) API key (if using Google provider) 🔑
- Google Cloud Project with Vertex AI API enabled (if using Application Default Credentials for Gemini) ☁️
- [uv](https://github.com/astral-sh/uv) installed.

### Setup 🛠️

#### From source

1. **Clone this repository**:
   ```bash
   git clone https://github.com/1rgs/claude-code-proxy.git
   cd claude-code-proxy
   ```

2. **Install uv** (if you haven't already):
   ```bash
   curl -LsSf https://astral.sh/uv/install.sh | sh
   ```
   *(`uv` will handle dependencies based on `pyproject.toml` when you run the server)*

3. **Configure Environment Variables**:
   Copy the example environment file:
   ```bash
   cp .env.example .env
   ```
   Edit `.env` and fill in your API keys and model configurations:

   *   `ANTHROPIC_API_KEY`: (Optional) Needed only if proxying *to* Anthropic models.
   *   `OPENAI_API_KEY`: Your OpenAI API key (Required if using the default OpenAI preference or as fallback).
   *   `GEMINI_API_KEY`: Your Google AI Studio (Gemini) API key (Required if `PREFERRED_PROVIDER=google` and `USE_VERTEX_AUTH=true`).
   *   `USE_VERTEX_AUTH` (Optional): Set to `true` to use Application Default Credentials (ADC) will be used (no static API key required). Note: when USE_VERTEX_AUTH=true, you must configure `VERTEX_PROJECT` and `VERTEX_LOCATION`.
   *   `VERTEX_PROJECT` (Optional): Your Google Cloud Project ID (Required if `PREFERRED_PROVIDER=google` and `USE_VERTEX_AUTH=true`).
   *   `VERTEX_LOCATION` (Optional): The Google Cloud region for Vertex AI (e.g., `us-central1`) (Required if `PREFERRED_PROVIDER=google` and `USE_VERTEX_AUTH=true`).
   *   `PREFERRED_PROVIDER` (Optional): Set to `openai` (default), `google`, or `anthropic`. This determines the primary backend for mapping `haiku`/`sonnet`.
   *   `BIG_MODEL` (Optional): The model to map `sonnet` requests to. Defaults to `gpt-4.1` (if `PREFERRED_PROVIDER=openai`) or `gemini-2.5-pro-preview-03-25`. Ignored when `PREFERRED_PROVIDER=anthropic`.
   *   `SMALL_MODEL` (Optional): The model to map `haiku` requests to. Defaults to `gpt-4.1-mini` (if `PREFERRED_PROVIDER=openai`) or `gemini-2.0-flash`. Ignored when `PREFERRED_PROVIDER=anthropic`.

   **Mapping Logic:**
   - If `PREFERRED_PROVIDER=openai` (default), `haiku`/`sonnet` map to `SMALL_MODEL`/`BIG_MODEL` prefixed with `openai/`.
   - If `PREFERRED_PROVIDER=google`, `haiku`/`sonnet` map to `SMALL_MODEL`/`BIG_MODEL` prefixed with `gemini/` *if* those models are in the server's known `GEMINI_MODELS` list (otherwise falls back to OpenAI mapping).
   - If `PREFERRED_PROVIDER=anthropic`, `haiku`/`sonnet` requests are passed directly to Anthropic with the `anthropic/` prefix without remapping to different models.

4. **Run the server**:
   ```bash
   uv run uvicorn server:app --host 0.0.0.0 --port 8082 --reload
   ```
   *(`--reload` is optional, for development)*

#### Docker

If using docker, download the example environment file to `.env` and edit it as described above.
```bash
curl -O .env https://raw.githubusercontent.com/1rgs/claude-code-proxy/refs/heads/main/.env.example
```

Then, you can either start the container with [docker compose](https://docs.docker.com/compose/) (preferred):

```yml
services:
  proxy:
    image: ghcr.io/1rgs/claude-code-proxy:latest
    restart: unless-stopped
    env_file: .env
    ports:
      - 8082:8082
```

Or with a command:

```bash
docker run -d --env-file .env -p 8082:8082 ghcr.io/1rgs/claude-code-proxy:latest
```

### Using with Claude Code 🎮

1. **Install Claude Code** (if you haven't already):
   ```bash
   npm install -g @anthropic-ai/claude-code
   ```

2. **Connect to your proxy**:
   ```bash
   ANTHROPIC_BASE_URL=http://localhost:8082 claude
   ```

3. **That's it!** Your Claude Code client will now use the configured backend models (defaulting to Gemini) through the proxy. 🎯

## Model Mapping 🗺️

The proxy automatically maps Claude models to either OpenAI or Gemini models based on the configured model:

| Claude Model | Default Mapping | When BIG_MODEL/SMALL_MODEL is a Gemini model |
|--------------|--------------|---------------------------|
| haiku | openai/gpt-4o-mini | gemini/[model-name] |
| sonnet | openai/gpt-4o | gemini/[model-name] |

### Supported Models

#### OpenAI Models
The following OpenAI models are supported with automatic `openai/` prefix handling:
- o3-mini
- o1
- o1-mini
- o1-pro
- gpt-4.5-preview
- gpt-4o
- gpt-4o-audio-preview
- chatgpt-4o-latest
- gpt-4o-mini
- gpt-4o-mini-audio-preview
- gpt-4.1
- gpt-4.1-mini

#### Gemini Models
The following Gemini models are supported with automatic `gemini/` prefix handling:
- gemini-2.5-pro
- gemini-2.5-flash

### Model Prefix Handling
The proxy automatically adds the appropriate prefix to model names:
- OpenAI models get the `openai/` prefix
- Gemini models get the `gemini/` prefix
- The BIG_MODEL and SMALL_MODEL will get the appropriate prefix based on whether they're in the OpenAI or Gemini model lists

For example:
- `gpt-4o` becomes `openai/gpt-4o`
- `gemini-2.5-pro-preview-03-25` becomes `gemini/gemini-2.5-pro-preview-03-25`
- When BIG_MODEL is set to a Gemini model, Claude Sonnet will map to `gemini/[model-name]`

### Customizing Model Mapping

Control the mapping using environment variables in your `.env` file or directly:

**Example 1: Default (Use OpenAI)**
No changes needed in `.env` beyond API keys, or ensure:
```dotenv
OPENAI_API_KEY="your-openai-key"
GEMINI_API_KEY="your-google-key" # Needed if PREFERRED_PROVIDER=google
# PREFERRED_PROVIDER="openai" # Optional, it's the default
# BIG_MODEL="gpt-4.1" # Optional, it's the default
# SMALL_MODEL="gpt-4.1-mini" # Optional, it's the default
```

**Example 2a: Prefer Google (using GEMINI_API_KEY)**
```dotenv
GEMINI_API_KEY="your-google-key"
OPENAI_API_KEY="your-openai-key" # Needed for fallback
PREFERRED_PROVIDER="google"
# BIG_MODEL="gemini-2.5-pro" # Optional, it's the default for Google pref
# SMALL_MODEL="gemini-2.5-flash" # Optional, it's the default for Google pref
```

**Example 2b: Prefer Google (using Vertex AI with Application Default Credentials)**
```dotenv
OPENAI_API_KEY="your-openai-key" # Needed for fallback
PREFERRED_PROVIDER="google"
VERTEX_PROJECT="your-gcp-project-id"
VERTEX_LOCATION="us-central1"
USE_VERTEX_AUTH=true
# BIG_MODEL="gemini-2.5-pro" # Optional, it's the default for Google pref
# SMALL_MODEL="gemini-2.5-flash" # Optional, it's the default for Google pref
```

**Example 3: Use Direct Anthropic ("Just an Anthropic Proxy" Mode)**
```dotenv
ANTHROPIC_API_KEY="sk-ant-..."
PREFERRED_PROVIDER="anthropic"
# BIG_MODEL and SMALL_MODEL are ignored in this mode
# haiku/sonnet requests are passed directly to Anthropic models
```

*Use case: This mode enables you to use the proxy infrastructure (for logging, middleware, request/response processing, etc.) while still using actual Anthropic models rather than being forced to remap to OpenAI or Gemini.*

**Example 4: Use Specific OpenAI Models**
```dotenv
OPENAI_API_KEY="your-openai-key"
GEMINI_API_KEY="your-google-key"
PREFERRED_PROVIDER="openai"
BIG_MODEL="gpt-4o" # Example specific model
SMALL_MODEL="gpt-4o-mini" # Example specific model
```

## API Endpoints 🔌

The proxy exposes **two endpoints**, both available for all providers:

| Endpoint | Format | Description |
|----------|--------|-------------|
| `/v1/messages` | Anthropic | For Claude Code and Anthropic SDK clients — translates between Anthropic and OpenAI formats via LiteLLM |
| `/v1/chat/completions` | OpenAI | For OpenAI-compatible clients (agents, tools, SDKs) |

### Per-Provider Behavior

| Provider | `/v1/messages` | `/v1/chat/completions` |
|----------|---------------|----------------------|
| `anthropic` | LiteLLM 翻译 | LiteLLM 翻译 |
| `gemini` | LiteLLM 翻译 | LiteLLM 翻译 |
| **`openai`** | LiteLLM 翻译 | **直接透传** |
| **`gemini-openai`** | LiteLLM 翻译 | **直接透传** |
| **`copilot`** | LiteLLM 翻译 | **直接透传** |
| **`qclaw`** | LiteLLM 翻译 | **直接透传** |

> **透传模式**：`openai`、`gemini-openai`、`copilot`、`qclaw` 四个 provider 在 `/v1/chat/completions` 上直接转发请求体给后端，不做格式翻译或模型映射。各 provider 仅注入必要的认证 header 和请求体预处理：
> - **qclaw**：去掉 `qclaw/` 前缀，自动补 system message，注入网关认证 header
> - **openai**：去掉 `openai/` 前缀，注入 `Authorization: Bearer`
> - **copilot**：模型映射（haiku/sonnet/opus → COPILOT_*_MODEL），注入 `Copilot-Integration-Id`，清理空 content 和无效 tool_choice
> - **gemini-openai**：去掉 `gemini/` 前缀，注入 `Authorization: Bearer`
>
> 所有透传 provider 共享全局 httpx 连接池，连接错误/5xx 自动重试 3 次。
>
> `anthropic` 和 `gemini`（原生 API）后端不是 OpenAI 兼容格式，`/v1/chat/completions` 上仍通过 LiteLLM 进行格式翻译。

## How It Works 🧩

This proxy works by:

1. **Receiving requests** in Anthropic's API format 📥
2. **Translating** the requests to OpenAI format via LiteLLM 🔄
3. **Sending** the translated request to the configured backend 📤
4. **Converting** the response back to Anthropic format 🔄
5. **Returning** the formatted response to the client ✅

The proxy handles both streaming and non-streaming responses, maintaining compatibility with all Claude clients. 🌊

## Contributing 🤝

Contributions are welcome! Please feel free to submit a Pull Request. 🎁

---

## Environment Variables Quick Reference 🔧

Copy `.env.example` to `.env` and fill in your keys. The proxy supports **6 providers** — pick one and set `PREFERRED_PROVIDER` accordingly.

### Common Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `PREFERRED_PROVIDER` | ✅ | `openai` | Active backend: `anthropic` / `gemini` / `gemini-openai` / `openai` / `qclaw` / `copilot` |
| `BIG_MODEL` | — | provider default | Maps `opus`-series requests |
| `MEDIUM_MODEL` | — | provider default | Maps `sonnet`-series requests |
| `SMALL_MODEL` | — | provider default | Maps `haiku`-series requests |
| `DEBUG` | — | `false` | Set `true` to enable verbose request/response logs |
| `LOG_FILE` | — | _(stdout)_ | Path to write file logs, e.g. `/tmp/proxy.log` (works with `DEBUG=false`, logs warnings/errors) |
| `LOG_RETENTION_DAYS` | — | `7` | Keep rotated log files for N days, then auto-clean |
| `LOG_ROTATE_WHEN` | — | `midnight` | Rotation time unit for file logs (`midnight`, `H`, etc.) |
| `LOG_ROTATE_INTERVAL` | — | `1` | Rotation interval count for `LOG_ROTATE_WHEN` |

---

### Provider 1 — Anthropic / DeepSeek (recommended)

Native Anthropic format; works with any Anthropic-compatible endpoint (DeepSeek, official Anthropic, etc.).

```ini
PREFERRED_PROVIDER=anthropic
ANTHROPIC_API_KEY=sk-ant-xxxxxxxxxxxxxxxx        # or DeepSeek key
ANTHROPIC_BASE_URL=https://api.deepseek.com/anthropic   # omit for official Anthropic
BIG_MODEL=deepseek-v4-pro
MEDIUM_MODEL=deepseek-v4-flash
SMALL_MODEL=deepseek-v4-flash
```

---

### Provider 2 — Gemini Native API

Uses LiteLLM with `gemini/` prefix. Built-in `thoughtSignature` handling for streaming.

```ini
PREFERRED_PROVIDER=gemini
GEMINI_API_KEY=AIzaSyXXXXXXXXXXXXXXXXXXXXXXXXXXXX
BIG_MODEL=gemini-2.5-pro
MEDIUM_MODEL=gemini-2.5-flash
SMALL_MODEL=gemini-2.5-flash
```

---

### Provider 3 — Gemini OpenAI-Compatible Endpoint

Uses Google's `/v1beta/openai` endpoint directly via httpx (OpenAI wire format).

```ini
PREFERRED_PROVIDER=gemini-openai
GEMINI_API_KEY=AIzaSyXXXXXXXXXXXXXXXXXXXXXXXXXXXX
GEMINI_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai
BIG_MODEL=gemini-2.5-pro
MEDIUM_MODEL=gemini-2.5-flash
SMALL_MODEL=gemini-2.5-flash
```

---

### Provider 4 — OpenAI Official / Compatible

Standard OpenAI API (or any OpenAI-compatible endpoint by overriding `OPENAI_BASE_URL`).

```ini
PREFERRED_PROVIDER=openai
OPENAI_API_KEY=sk-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
OPENAI_BASE_URL=https://api.openai.com/v1    # omit for official OpenAI
BIG_MODEL=gpt-4.1
MEDIUM_MODEL=gpt-4.1-mini
SMALL_MODEL=gpt-4.1-mini
```

---

### Provider 5 — QClaw Local Gateway

For use with the QClaw desktop gateway (local routing).

**双端点行为**：`/v1/messages` 走 LiteLLM 翻译（Claude→QClaw 三级模型映射）；`/v1/chat/completions` **直接透传**（无翻译，原样转发给 QClaw 网关，支持自动重试和连接池复用）。

**已注册模型**（11 个）：`modelroute`、`pool-hy3-preview`、`pool-deepseek-v4-pro`、`pool-deepseek-v4-flash`、`pool-glm-5.2`、`pool-glm-5.2-night`、`pool-glm-5.1`、`pool-kimi-k2.7-code-highspeed`、`pool-kimi-k2.6`、`pool-minimax-m3`、`pool-minimax-m2.7`

```ini
PREFERRED_PROVIDER=qclaw
BIG_MODEL=pool-deepseek-v4-pro
MEDIUM_MODEL=pool-deepseek-v4-pro
SMALL_MODEL=pool-deepseek-v4-flash
BIG_REASONING=high
MEDIUM_REASONING=low
SMALL_REASONING=low
```

---

### Provider 6 — GitHub Copilot Enterprise

Routes through your company's Copilot Enterprise API. No extra cost if your org already has Copilot seats.

```ini
PREFERRED_PROVIDER=copilot
COPILOT_GHE_TOKEN=github_pat_xxxxxxxxxxxxxxxxxxxx   # gh auth token --hostname <your-ghe-host>
COPILOT_GHE_HOST=copilot-api.your-company.ghe.com  # your enterprise hostname
COPILOT_INTEGRATION_ID=copilot-developer-cli        # default, do not change
COPILOT_BIG_MODEL=claude-opus-4.8
COPILOT_MEDIUM_MODEL=claude-sonnet-4.6
COPILOT_SMALL_MODEL=claude-haiku-4.5
```

**Get your PAT:**
```bash
gh auth token --hostname your-company.ghe.com
```

**Available models** (from Copilot Enterprise `/models` API):

| Category | Model IDs |
|----------|-----------|
| Claude | `claude-haiku-4.5` · `claude-sonnet-4.5` · `claude-sonnet-4.6` · `claude-opus-4.5` · `claude-opus-4.6` · `claude-opus-4.8` |
| GPT | `gpt-5.5` · `gpt-5.4` · `gpt-5.3-codex` · `gpt-5-mini` · `gpt-4.1` · `gpt-4o-mini` · `gpt-3.5-turbo` |
| Gemini | `gemini-2.5-pro` |

> ⚠️ `gpt-5.4-mini` appears in `/models` but is rejected at inference time — do not set it as `COPILOT_SMALL_MODEL`.

---

### Claude Code Integration ⚙️

Add to `~/.claude/settings.json` (works with any provider — only change `.env`):

```json
{
  "env": {
    "ANTHROPIC_BASE_URL": "http://127.0.0.1:8082",
    "ANTHROPIC_API_KEY": "dummy",
    "ANTHROPIC_AUTH_TOKEN": "dummy"
  }
}
```

Switch providers by changing `PREFERRED_PROVIDER` in `.env` — no Claude Code config changes needed.
