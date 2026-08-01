# AGENT.md

> 本文件是给 AI Agent（Claude Code / Cursor / Trae 等）的项目上下文速查。
> 在动手前先读一遍，避免重复踩坑。

---

## 1. 项目简介

**claude-code-proxy** 是一个 FastAPI 代理服务，让 Anthropic 客户端（如 Claude Code）能用多种后端（OpenAI / Gemini / Anthropic / Copilot Enterprise / QClaw）。

- **主入口**：`server.py`（单文件，~6000 行）
- **Python 版本**：3.10+（见 `pyproject.toml`）
- **依赖**：fastapi, uvicorn, httpx, litellm, python-dotenv, tiktoken, pydantic
- **配置模块**：`config_store.py`（targets.json 加载/迁移/校验、secrets.json 读写、热重载）
- **虚拟环境**：`.venv/`（Windows 下用 `.venv\Scripts\python.exe`）

---

## 2. 快速启动

### 当前部署环境（Windows Server）

| 路径 | 用途 |
|------|------|
| `C:\Users\Administrator\claude-code-proxy-main\` | 项目根 |
| `.venv\Scripts\python.exe` | 项目专用 Python |
| `.env` | 配置文件（**gitignored**，含密钥） |
| `scripts\windows\start_proxy.vbs` | 开机自启脚本（隐藏窗口，动态定位项目根） |
| `proxy.log` | 运行日志 |
| 计划任务 `\ClaudeCodeProxy` | 登录时触发 VBS |

### 手动启动

```powershell
Set-Location "c:\Users\Administrator\claude-code-proxy-main"
# .env 自动加载，不需要设环境变量
& ".\.venv\Scripts\python.exe" server.py
```

### 开机自启

计划任务 `\ClaudeCodeProxy` 在用户登录时调用 `wscript.exe scripts\windows\start_proxy.vbs`。VBS 是**纯启动器**，不设任何环境变量——所有配置来自 `.env`。脚本用 `ScriptFullName` 动态定位项目根（`scripts/windows/` 上两级），移动项目目录后只需更新计划任务路径。

修改配置：编辑 `.env` → 重启代理（`Stop-Process -Id <PID>` + 重跑 VBS）。

---

## 3. 配置文件

### `.env`（已简化）

多端口架构下，`PREFERRED_PROVIDER` 已废弃——不再由 `.env` 控制端口路由，所有 target 由 [`targets.json`](targets.json) 定义。

`.env` 中仅保留全局配置：`DEBUG`、`LOG_FILE`、`LOG_RETENTION_DAYS`、`LOG_ROTATE_WHEN`、`LOG_ROTATE_INTERVAL`。

### `targets.json`（Target 定义）

每个 target 指定端口、供应商、分类、handler、上游 host、模型映射等。端口-供应商绑定由 targets.json 的 `listenPort` 字段定义，无需修改 server.py。

### `secrets.json`（私密 key/token）

破解工具 (`crack_copilot.py` / `crack_codebuddy.py` / `crack_qclaw.py`) 提取的 API key/token 写入此文件，dashboard 可编辑。不入库（gitignored）。

格式：`{"copilot_token": "...", "codebuddy_token": "...", "qclaw_api_key": "...", "trae_work_token": ""}`

---

## 4. API 端点（多端口架构）

| 端口 | 供应商 | 分类 | handler | 协议 | 用途 |
|------|--------|------|---------|------|------|
| **8081** | anthropic-compatible | — | FastAPI | Anthropic | `/v1/messages`（Anthropic）/ dashboard 管理界面 / `/api/targets` 等 REST API |
| **8082** | copilot | crack | copilot | OpenAI | `/v1/chat/completions`（透传）+ 由 8081 内部转发 |
| **8084** | codebuddy | crack | passthrough | OpenAI | `/v1/chat/completions`（crack 透传） |
| **8085** | qclaw | crack | qclaw | OpenAI | `/v1/chat/completions`（qclaw 透传） |
| **8086** | trae-work (预留) | crack | passthrough | OpenAI | 暂未启用 |
| **8090** | openrouter | free | passthrough | OpenAI | 免费代理（透传客户端 key） |
| **8091** | nvidia | free | passthrough | OpenAI | 免费代理 |
| **8092** | gemini | free | **gemini-native** | OpenAI↔Gemini | 原生 Gemini 协议转换（generateContent） |
| **8093** | opencode-zen | free | passthrough | OpenAI | 免费代理 |
| **8094** | open-go | paid | passthrough | OpenAI | 收费代理 |

- **配置驱动**：所有 target 由 `targets.json` 定义，无需修改 server.py
- **分类**：`crack`（破解获取 token）/ `free`（免费透传）/ `paid`（收费透传）
- **热重载**：mtime 轮询（2s），targets.json / secrets.json 修改后自动生效
- **base_url 规范**：crack 类与 gemini-native 统一 `/v1`（代理内部映射下游）；free/paid 透传用 `routePrefix`（如 `/api/v1`）

### 客户端接入

**OpenAI 协议**：`base_url = http://<局域网IP>:8082/v1`，`api_key = "dummy"`（crack 类代理不校验；free/paid 透传用真实 key）
**Anthropic 协议**：`base_url = http://<局域网IP>:8081`，`api_key = "dummy"`
> dashboard 卡片详情的 `base_url` 属性直接显示可粘贴地址（局域网 IP + 端口 + 后缀）。

---

## 5. QClaw 特殊性（重要）

### API Key 自动解密

`server.py` 启动时自动从 QClaw 本地存储解密 API Key：
- 读取 `%APPDATA%\QClaw\app-store.json` 的 `authGateway.providers.qclaw.apiKey.cipherText`
- 读取 `%APPDATA%\QClaw\Local State` 的 `os_crypt.encrypted_key`
- DPAPI 解密 AES 密钥 → AES-256-GCM 解密 cipherText → 得到 `sk-...` API Key
- Key 提取后写入 `secrets.json` 的 `qclaw_api_key` 字段，dashboard 可编辑
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

> 行号随版本漂移，以下按**功能模块**描述（实际位置用 `grep` 定位，不建议依赖行号）：

```
模块导入 + load_dotenv() + 常量
tiktoken 本地 token 估算（QClaw 网关过滤 usage 时注入）
日志配置（彩色 + 滚动）
httpx 客户端管理（trust_env=False）+ QClaw body 清理
QClaw 透传函数 + OpenAI→Anthropic 转换
FastAPI lifespan（启动诊断 + target 端口启动 + 破解工具自动调用）
QClaw API Key DPAPI/AES 解密（Windows；其他 OS 提示未实现）
targets.json 配置加载 + 热重载（config_store.py 负责 schema 校验）
Provider 策略注册（_PROVIDER_STRATEGIES，开闭原则）
模型名映射（opus/sonnet/haiku → BIG/MEDIUM/SMALL）
asyncio target 端口统一转发引擎（_handle_target_request）
  - /api/*、/dashboard → 代理回 8081 FastAPI
  - 路径重写（_rewrite_upstream_path：handler 映射 > routePrefix > 原样）
  - 认证注入（_handler_prepare_headers：crack 注入 secrets / free-paid 透传客户端 key）
  - 模型级统计（_bump_model_stats）
  - gemini-native handler（_handle_gemini_native：OpenAI ↔ generateContent 转换）
中间件（8081 /v1/messages 统计 + 日志）
Anthropic ↔ LiteLLM 双向转换
/v1/chat/completions（透传 + LiteLLM 分流）
流式响应处理
/v1/messages（Anthropic 端点）+ dashboard API
dashboard（HTML/CSS/JS 内嵌：分类栏 / 卡片 / 模型编辑弹框 / 总开关 / 搜索）
/v1/messages/count_tokens + /v1/models
```

### Provider 策略机制

`_PROVIDER_STRATEGIES` 字典注册了每个 provider 的处理函数（`_qclaw_provider`、`_anthropic_provider` 等）。新增 provider 只需：
1. 在 `valid_providers` 元组中加名字
2. 写一个 `_xxx_provider(req, litellm_req, orig)` 函数
3. 注册到 `_PROVIDER_STRATEGIES`

### 透传 vs 翻译

- **透传**（qclaw/openai/copilot）：`/v1/chat/completions` 直接 httpx 转发，不经 LiteLLM，保留原始请求体
- **翻译**（anthropic/gemini）：经 LiteLLM 做格式转换和模型映射
- **gemini-native**（8092）：接受 OpenAI 请求，代理内部转换为 Google 原生 `generateContent` API（见 docs/architecture.md）

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

1. `netstat -ano | findstr :8081` — 主端口是否监听
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
