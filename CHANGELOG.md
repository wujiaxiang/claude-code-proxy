# Changelog

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
