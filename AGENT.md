# AGENT.md

> 本文件是给 AI Agent（Claude Code / Cursor / Trae 等）的项目上下文速查。
> 在动手前先读一遍，避免重复踩坑。

---

## 1. 项目简介

**claude-code-proxy** 是一个 FastAPI 代理服务，让 Anthropic 客户端（如 Claude Code）能用多种后端（OpenAI / Gemini / Anthropic / Copilot Enterprise / QClaw）。

- **主入口**：`server.py`（单文件，~3100 行）
- **Python 版本**：3.10+（见 `pyproject.toml`）
- **依赖**：fastapi, uvicorn, httpx, litellm, python-dotenv, tiktoken, pydantic
- **虚拟环境**：`.venv/`（Windows 下用 `.venv\Scripts\python.exe`）

---

## 2. 快速启动

### 当前部署环境（Windows Server）

| 路径 | 用途 |
|------|------|
| `C:\Users\Administrator\claude-code-proxy-main\` | 项目根 |
| `.venv\Scripts\python.exe` | 项目专用 Python |
| `.env` | 配置文件（**gitignored**，含密钥） |
| `start_proxy.vbs` | 开机自启脚本（隐藏窗口） |
| `proxy.log` | 运行日志 |
| 计划任务 `\ClaudeCodeProxy` | 登录时触发 VBS |

### 手动启动

```powershell
Set-Location "c:\Users\Administrator\claude-code-proxy-main"
# .env 自动加载，不需要设环境变量
& ".\.venv\Scripts\python.exe" server.py
```

### 开机自启

计划任务 `\ClaudeCodeProxy` 在用户登录时调用 `wscript.exe start_proxy.vbs`。VBS 是**纯启动器**，不设任何环境变量——所有配置来自 `.env`。

修改配置：编辑 `.env` → 重启代理（`Stop-Process -Id <PID>` + 重跑 VBS）。

---

## 3. 配置文件 `.env`

**所有 provider/model 配置都在 `.env`**，不要硬编码到 VBS 或环境变量。

### 当前配置（DeepSeek V4 Pro）

```ini
PREFERRED_PROVIDER=qclaw
BIG_MODEL=pool-deepseek-v4-pro
MEDIUM_MODEL=pool-deepseek-v4-pro
SMALL_MODEL=pool-deepseek-v4-pro
BIG_REASONING=high
MEDIUM_REASONING=low
SMALL_REASONING=low
```

### 支持的 Provider（7 个）

| Provider | 后端 | `/v1/chat/completions` 行为 |
|----------|------|---------------------------|
| `anthropic` | Anthropic / DeepSeek | LiteLLM 翻译 |
| `gemini` | Gemini 原生 API | LiteLLM 翻译 |
| `gemini-openai` | Gemini OpenAI 兼容端点 | **直接透传** |
| `openai` | OpenAI 官方 | **直接透传** |
| `copilot` | GitHub Copilot Enterprise | **直接透传** |
| `qclaw` | QClaw 直连上游 `mmgrcalltoken.3g.qq.com` | **直接透传** |
| `qclaw-local` | QClaw 19000 本地网关（需寄生注入） | **直接透传** |

### QClaw 可用模型（11 个）

`modelroute`、`pool-hy3-preview`、`pool-deepseek-v4-pro`、`pool-deepseek-v4-flash`、`pool-glm-5.2`、`pool-glm-5.2-night`、`pool-glm-5.1`、`pool-kimi-k2.7-code-highspeed`、`pool-kimi-k2.6`、`pool-minimax-m3`、`pool-minimax-m2.7`

> ⚠️ `hunyuan/hy3` 系列上游不支持，会返回 `model_not_valid`。

---

## 4. API 端点

| 端点 | 协议 | 用途 |
|------|------|------|
| `POST /v1/messages` | Anthropic | Claude Code / Anthropic SDK |
| `POST /v1/chat/completions` | OpenAI | OpenAI SDK / 透传链路 |
| `POST /v1/messages/count_tokens` | Anthropic | Token 计数 |
| `GET /v1/models` | OpenAI | 模型列表 |

### 默认监听端口

- `0.0.0.0:8082`（生产环境，`192.168.2.177:8082`）
- 本地调试用 `8083` 避免冲突

### 客户端接入

**OpenAI 协议**：`base_url = http://192.168.2.177:8082/v1`，`api_key = "dummy"`（代理不校验）
**Anthropic 协议**：`base_url = http://192.168.2.177:8082`，`api_key = "dummy"`

---

## 5. QClaw 特殊性（重要）

### API Key 自动解密

`server.py` 启动时自动从 QClaw 本地存储解密 API Key：
- 读取 `%APPDATA%\QClaw\app-store.json` 的 `authGateway.providers.qclaw.apiKey.cipherText`
- 读取 `%APPDATA%\QClaw\Local State` 的 `os_crypt.encrypted_key`
- DPAPI 解密 AES 密钥 → AES-256-GCM 解密 cipherText → 得到 `sk-...` API Key
- 环境变量 `QCLAW_API_KEY` 优先级最高，可手动覆盖

**所以 QClaw 客户端只需登录过一次，代理就能自动拿到 Key，不需要 QClaw 持续运行**（除非用 `qclaw-local`）。

### qclaw vs qclaw-local

| 维度 | `qclaw`（推荐） | `qclaw-local`（备用） |
|------|----------------|---------------------|
| QClaw 是否需运行 | 仅初始化时需要取 Key | **必须持续运行** |
| 网络链路 | server.py → 上游 LLM | server.py → 19001 → 19000 → 上游 |
| 额外依赖 | 无 | 需 `qclaw_inject.js` + `--inspect=9229` |
| 稳定性 | 高 | 中（依赖 QClaw 进程） |

`qclaw-local` 的完整部署步骤见 [README.md](README.md) 的 Provider 5b 章节。

### 关键约束（来自 project_memory）

- `QCLAW_BASE_URL = https://mmgrcalltoken.3g.qq.com/aizone/v1`（直连上游）
- `User-Agent` 必须设为 `OpenAI/JS 6.39.1`（否则上游 400）
- 所有 httpx 客户端必须 `trust_env=False`（绕过系统代理）
- 移除 `__QCLAW_AUTH_GATEWAY_MANAGED__`、`x-agent-id`、`Connection: close` header
- QClaw 网关会过滤上游响应的 `usage` 字段 → 代理用 tiktoken 本地估算并注入

### 19000 网关逆向结论

详见 [QCLAW_19000_GATEWAY_REVERSE.md](QCLAW_19000_GATEWAY_REVERSE.md)。核心结论：

- **HMAC-SHA256 签名算法已破解**（密钥 + payload 格式见文档）
- **19000 网关采用 OS 级 PID 反查**（koffi FFI 调用 `GetExtendedTcpTable`）
- **独立签名不可行**：PID 由 Windows 内核管理，用户态无法伪造
- **唯一可行方案是寄生**：在 QClaw 进程内注入 HTTP 服务器（`qclaw_inject.js`）

---

## 6. 代码结构（server.py）

```
Line 1-50      模块导入 + load_dotenv()
Line 44-156    tiktoken 本地 token 估算
Line 157-256   日志配置（彩色 + 滚动）
Line 257-320   httpx 客户端管理 + QClaw body 清理
Line 321-462   QClaw 透传函数 + OpenAI→Anthropic 转换
Line 463-533   FastAPI lifespan（启动诊断）+ 异常处理
Line 534-650   QClaw API Key DPAPI/AES 解密
Line 651-822   Provider 策略注册（开闭原则）
Line 824-870   模型名映射（opus/sonnet/haiku → BIG/MEDIUM/SMALL）
Line 872-1200  Pydantic 模型（Anthropic 协议）
Line 1205-1370 中间件 + 工具函数
Line 1371-1940 Anthropic ↔ LiteLLM 双向转换
Line 1957-2185 /v1/chat/completions（透传 + LiteLLM 分流）
Line 2186-2700 流式响应处理
Line 2705-3020 /v1/messages（Anthropic 端点）
Line 3027-3110 /v1/messages/count_tokens + /v1/models
```

### Provider 策略机制

`_PROVIDER_STRATEGIES` 字典注册了每个 provider 的处理函数（`_qclaw_provider`、`_anthropic_provider` 等）。新增 provider 只需：
1. 在 `valid_providers` 元组中加名字
2. 写一个 `_xxx_provider(req, litellm_req, orig)` 函数
3. 注册到 `_PROVIDER_STRATEGIES`

### 透传 vs 翻译

- **透传**（qclaw/openai/copilot/gemini-openai）：`/v1/chat/completions` 直接 httpx 转发，不经 LiteLLM，保留原始请求体
- **翻译**（anthropic/gemini）：经 LiteLLM 做格式转换和模型映射

---

## 7. 开发工作流

### Windows 工具路径（PATH 经常被污染）

```
git:   C:\Program Files\QClaw\v0.2.33.617\resources\git\cmd\git.exe
node:  C:\Program Files\QClaw\v0.2.33.617\resources\node\node.exe
python: c:\Users\Administrator\claude-code-proxy-main\.venv\Scripts\python.exe
```

**调用 git 前必须清理 PATH**：
```powershell
$env:Path = "C:\Program Files\QClaw\v0.2.33.617\resources\git\cmd;C:\Windows\System32;C:\Windows"
```

### Git 提交规范

使用 Conventional Commits：`feat:` / `fix:` / `docs:` / `chore:` / `refactor:`。

提交前用 `git status` 检查，不要 `git add .`（会带入 `.env` 等敏感文件）。

### 临时调研脚本

`.gitignore` 已忽略 `_*` 前缀的文件和目录。临时调研脚本用 `_` 开头，不会污染仓库。

### 调试模式

```powershell
$env:DEBUG = "true"
& ".\.venv\Scripts\python.exe" server.py
```

会打印详细的请求/响应日志（包括 LiteLLM 内部字段、QClaw body 清理记录等）。

---

## 8. 常见任务

### 切换模型

编辑 `.env` 的 `BIG_MODEL`/`MEDIUM_MODEL`/`SMALL_MODEL`，重启代理。

### 新增 QClaw 模型

在 `server.py` 第 660-677 行的 `_qclaw_all_models` 字典里加模型名，重启代理。

### 排查 403/9002 错误

1. 检查 QClaw 是否登录过（`%APPDATA%\QClaw\app-store.json` 存在）
2. 检查启动日志 `🔑 QClaw API Key decrypted: sk-xxx...xxxx`
3. 如果 Key 解密失败，设 `QCLAW_API_KEY` 环境变量手动指定
4. 上游 400 → 检查 `User-Agent` 是否为 `OpenAI/JS 6.39.1`
5. `qclaw-local` 403 → 检查 19001 端口是否监听（寄生服务器是否注入）

### 排查代理不通

1. `netstat -ano | findstr :8082` — 端口是否监听
2. `Get-Content proxy.log -Tail 20` — 查最近日志
3. `Get-Process python` — 进程是否存活
4. 日志中 `startup diag: QClaw upstream = 200` 表示上游连通

### 重启代理

```powershell
# 找到 PID
$proc = Get-NetTCPConnection -LocalPort 8082 -State Listen
# 停掉
Stop-Process -Id $proc.OwningProcess -Force
# 启动（用 VBS）
wscript.exe start_proxy.vbs
```

---

## 9. 已知陷阱

1. **PATH 污染**：Trae IDE 的 ripgrep 会污染 PATH，导致 `Get-NetTCPConnection` 等 cmdlet 不可用。调用前先 `$env:Path = "C:\Windows\System32;C:\Windows"`。
2. **`.env` 不入库**：含密钥，`.gitignore` 已忽略。新环境需手动创建。
3. **QClaw 升级**：版本号 `v0.2.33.617` 硬编码在多处路径中，升级后需全局替换。
4. **LiteLLM 模型注册**：QClaw 模型名不在 LiteLLM 内置映射中，必须 `litellm.register_model()` 注册，否则报 "model isn't mapped"。
5. **QClaw body 清理**：客户端可能透传 Anthropic 专属字段（`thinking`、`reasoning_effort`、`output_config`），上游会 400。`_clean_qclaw_body()` 负责清理。
6. **流式响应循环引用**：`qclaw_inject.js` 只复制 axios 请求拦截器，不复制响应拦截器（否则流式响应 JSON.stringify 触发循环引用）。
7. **QClaw 网关过滤 usage**：上游响应没有 `usage` 字段，代理用 tiktoken 本地估算并注入，否则 Claude Code 不显示用量。

---

## 10. 相关文档

- [README.md](README.md) — 用户文档（含所有 provider 配置示例）
- [README-zh.md](README-zh.md) — 中文用户文档
- [CHANGELOG.md](CHANGELOG.md) — 变更日志
- [QCLAW_19000_GATEWAY_REVERSE.md](QCLAW_19000_GATEWAY_REVERSE.md) — 19000 网关逆向调研报告
- [pyproject.toml](pyproject.toml) — 依赖声明
- [qclaw_inject.js](qclaw_inject.js) — qclaw-local 寄生注入脚本
- [start_proxy.vbs](start_proxy.vbs) — 开机自启脚本

---

## 11. Git 状态

- **主分支**：`main`（直接提交，不用 PR）
- **远程**：`https://github.com/wujiaxiang/claude-code-proxy.git`
- **最近提交**：`refactor: read config from .env instead of hardcoded env vars in VBS`（3a9af61）

---

## 12. Agent 行为准则

1. **改配置改 `.env`**，不要改 VBS 或硬编码环境变量。
2. **新增功能先看 Provider 策略机制**（第 814 行 `_PROVIDER_STRATEGIES`），遵循开闭原则。
3. **QClaw 相关改动**注意三个约束：`User-Agent`、`trust_env=False`、body 清理。
4. **临时脚本用 `_` 开头**，会被 gitignore 自动忽略。
5. **提交前** `git status` 检查，不要带入 `.env` / `*.log` / `.venv/`。
6. **调试**用 `$env:DEBUG = "true"`，不要往代码里加 print。
7. **清理 PATH** 再调 Windows 命令，避免 PATH 污染导致 cmdlet 不可用。
8. **遇到 403/9002** 先看 [QCLAW_19000_GATEWAY_REVERSE.md](QCLAW_19000_GATEWAY_REVERSE.md)，不要重复逆向。
