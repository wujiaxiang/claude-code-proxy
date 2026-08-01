# Anthropic API Proxy for Gemini, OpenAI & Copilot Enterprise 🔄

**Use Anthropic clients (like Claude Code) with multiple backends via a multi-port architecture.** 🤝

A proxy server that exposes one Anthropic-compatible entry (8081) plus one OpenAI-compatible port per upstream vendor, all driven by a single `targets.json` config. 🔀

![Anthropic API Proxy](pic.png)

## Quick Start ⚡

### Prerequisites

- Python 3.10+ and [uv](https://github.com/astral-sh/uv) (or a venv)
- API keys for the upstream providers you want to use

### Setup 🛠️

1. **Clone this repository**:
   ```bash
   git clone https://github.com/wujiaxiang/claude-code-proxy.git
   cd claude-code-proxy
   ```

2. **Install dependencies**:
   ```bash
   uv sync   # or: python -m venv .venv && .venv/bin/pip install -r requirements.txt
   ```

3. **Create `targets.json`** (port/provider/category/handler/model config) and `secrets.json` (private tokens, gitignored).
   See [docs/architecture.md](docs/architecture.md) for the full schema.

4. **Create `.env`** with global config only (`DEBUG`, `LOG_FILE`, `LOG_RETENTION_DAYS`, `LOG_ROTATE_WHEN`, `LOG_ROTATE_INTERVAL`).

5. **Run the server**:
   ```bash
   .venv/bin/python server.py   # starts 8081 + all target ports from targets.json
   ```

### Using with Claude Code 🎮

Add to `~/.claude/settings.json` (any provider — only `targets.json` decides routing):

```json
{
  "env": {
    "ANTHROPIC_BASE_URL": "http://127.0.0.1:8081",
    "ANTHROPIC_API_KEY": "dummy",
    "ANTHROPIC_AUTH_TOKEN": "dummy"
  }
}
```

## Architecture (Multi-Port) 🏗️

| 端口 | 供应商 | 分类 | handler | 协议 |
|------|--------|------|---------|------|
| 8081 | anthropic-compatible | — | FastAPI | Anthropic（入口 + dashboard） |
| 8082 | copilot | crack | copilot | OpenAI |
| 8084 | codebuddy | crack | passthrough | OpenAI |
| 8085 | qclaw | crack | qclaw | OpenAI |
| 8086 | trae-work (预留) | crack | passthrough | OpenAI |
| 8090 | openrouter | free | passthrough | OpenAI |
| 8091 | nvidia | free | passthrough | OpenAI |
| 8092 | gemini | free | **gemini-native** | OpenAI↔Gemini 原生转换 |
| 8093 | opencode-zen | free | passthrough | OpenAI |
| 8094 | open-go | paid | passthrough | OpenAI |

- **配置驱动**：所有 target 由 `targets.json` 定义（端口/供应商/分类/handler/上游/模型映射），无需修改 server.py
- **分类**：`crack`（破解 token）/ `free`（免费透传客户端 key）/ `paid`（收费透传）
- **热重载**：mtime 轮询（2s），`targets.json` / `secrets.json` 修改后自动生效
- **base_url 规范**：crack 类与 gemini-native 统一 `/v1`（代理内部映射下游）；free/paid 透传用 `routePrefix`
- **客户端接入**：`base_url = http://<host>:<port>/v1`（或 `/api/v1`），`api_key = "dummy"`（free/paid 用真实 key）

📖 完整架构、targets.json schema、gemini-native 协议转换、路径重写规则 → [docs/architecture.md](docs/architecture.md)

## Dashboard 🖥️

管理界面 `http://127.0.0.1:8081/dashboard`（任意 target 端口 `/dashboard` 也可访问）：

- 分类栏：聚合网关（8081）/ 破解网关 / 直连网关
- 卡片：请求数、流量统计（成功率/时长/进度条）、可粘贴 `base_url`
- 模型白名单编辑弹框：iOS 滑动开关 + **总开关**（全开/全关/部分开）+ 搜索框 + **下拉下游真实模型**
- token 编辑（含破解按钮，带环境检测）
- 8081 自身请求统计

设计契约 → [DESIGN.md](DESIGN.md)

## Crack Tools 🔓

`crack_copilot.py` / `crack_codebuddy.py` / `crack_qclaw.py` / `crack_traework.py` 从本地客户端提取 token 写入 `secrets.json`。

> **OS 支持**：仅 `crack_copilot.py` 跨平台（需 gh CLI）；`codebuddy`/`qclaw` 仅 Windows 本地破解，其他 OS 下 dashboard 按钮置灰提示。QClaw 可通过 `QCLAW_API_KEY` 环境变量或 dashboard 手动填 key 直连上游。

📖 详见 [docs/crack-tools.md](docs/crack-tools.md) 与 [QCLAW_19000_GATEWAY_REVERSE.md](QCLAW_19000_GATEWAY_REVERSE.md)

## Windows Deployment 🪤

Windows Server 计划任务 + VBS + BAT 三层自启架构，启动脚本在 [`scripts/windows/`](scripts/windows/)。

> 8 个踩坑实录（GBK 崩溃 / VBS 重定向 / `cmd /c` 禁用 / watchdog COM 失败 / pwsh 闪框等）→ [docs/windows-deployment.md](docs/windows-deployment.md)

## How It Works 🧩

1. **8081 (Anthropic)** 接收 `/v1/messages` → 翻译为 OpenAI 格式 → 内部请求 8082 → 译回 Anthropic
2. **target 端口 (OpenAI)** 共享统一转发引擎：HTTP 解析 / 认证注入 / 路径重写 / 429 翻译 / 重试
3. **gemini-native (8092)** 接受 OpenAI 请求，内部转换为 Google 原生 `generateContent` API
4. **模型统计** 每个 target 按模型记录 请求/成功率/错误/429，dashboard 可视化

## Contributing 🤝

Contributions are welcome! Please feel free to submit a Pull Request. 🎁

## 相关文档

- [docs/architecture.md](docs/architecture.md) — 多端口架构详解
- [docs/crack-tools.md](docs/crack-tools.md) — 破解工具与 OS 支持
- [docs/windows-deployment.md](docs/windows-deployment.md) — Windows 部署指南
- [AGENTS.md](AGENTS.md) — AI Agent 项目上下文速查
- [DESIGN.md](DESIGN.md) — Dashboard 设计契约
- [CHANGELOG.md](CHANGELOG.md) — 变更日志
- [QCLAW_19000_GATEWAY_REVERSE.md](QCLAW_19000_GATEWAY_REVERSE.md) — 19000 网关逆向调研报告
