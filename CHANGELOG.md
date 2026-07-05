# Changelog

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
