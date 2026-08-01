# AGENT.md

> 本文件是给 AI Agent（Claude Code / Cursor / Trae 等）的项目上下文速查。
> 在动手前先读一遍，避免重复踩坑。

---

## 1. 项目简介

**claude-code-proxy** 是一个 FastAPI 代理服务，让 Anthropic 客户端（如 Claude Code）能用多种后端（OpenAI / Gemini / Copilot Enterprise / QClaw / CodeBuddy / Trae Work）。

- **主入口**：`server.py`（单文件，~7000 行，多端口代理引擎 + dashboard 一体化）
- **Python 版本**：3.10+（见 `pyproject.toml`）
- **依赖**：fastapi, uvicorn, httpx, litellm, python-dotenv, tiktoken, pydantic
- **配置模块**：`config_store.py`（targets.json 加载/迁移/校验、secrets.json 读写、热重载）
- **虚拟环境**：`.venv/`（Windows 下用 `.venv\Scripts\python.exe`）
- **配置驱动**：所有端口/供应商/模型由 `targets.json` 定义，改配置不动 server.py

---

## 2. 快速启动

### 当前部署环境（Linux LXC + Windows Server 双环境）

| 环境 | 路径 | 用途 |
|------|------|------|
| Linux LXC（本机） | `/root/shared-workspace/claude-code-proxy/` | 开发/测试主环境 |
| Windows Server | `C:\Users\Administrator\claude-code-proxy-main\` | 生产部署 |
| `.env` | 项目根 | 全局配置（**gitignored**，含密钥） |
| `secrets.json` | 项目根 | 私密 key/token（**gitignored**，dashboard 可热编辑） |
| `proxy.log` | 项目根 | 运行日志 |
| 计划任务 `\ClaudeCodeProxy` | Windows | 登录时触发 VBS |

### 手动启动

```bash
# Linux
.venv/bin/python server.py   # 启动 8081 FastAPI + 所有 targets.json 端口

# Windows PowerShell
Set-Location "c:\Users\Administrator\claude-code-proxy-main"
& ".\.venv\Scripts\python.exe" server.py
```

### 重启代理（Linux）

```bash
PID=$(ss -tlnp | grep ':8081 ' | grep -oP 'pid=\K[0-9]+'); [ -n "$PID" ] && kill -9 $PID
nohup .venv/bin/python server.py > /tmp/proxy.log 2>&1 & disown
```

> **注意**：代理进程跑在独立 mount namespace，`/tmp` 与 shell 隔离——跨进程共享状态（如 crack_daily 时间戳）放仓库内 `.cache/`，勿用 `/tmp`。

---

## 3. 配置文件

### `.env`（全局配置）

- `DEBUG` / `LOG_FILE` / `LOG_RETENTION_DAYS` / `LOG_ROTATE_WHEN` / `LOG_ROTATE_INTERVAL` + 各网关 apikeyEnv（`COPILOT_GHE_TOKEN` 等）
- **已废弃**：`PREFERRED_PROVIDER`（多端口架构下不再控制路由，server.py 仍读取但无实际作用）

### `targets.json`（Target 定义，核心配置）

每个 target 指定端口、供应商、分类、handler、上游 host、模型映射。端口-供应商绑定由 `listenPort` 字段定义。

**必填字段**：`label / listenPort / category / handler / targetHost`
**合法枚举**：category ∈ `crack|free|paid`；handler ∈ `passthrough|copilot|qclaw|gemini-native|trae-work`
**完整键**：`label, listenPort, category, handler, targetHost, targetPort, targetProtocol, routePrefix, crackTool, secretRef, apikeyEnv, models, extraHeaders, modelMapping, isFree, enabled, name, reasoning`
**校验规则**：crack 类必须有 `crackTool`；label/端口不能冲突；`enabled=false` 跳过必填校验（预留位）
**secrets 优先级**：`secrets.json > apikeyEnv 环境变量 > 客户端透传`
**热重载**：mtime 轮询 2s，改完即生效（含端口动态增删）

### `secrets.json`（私密 key/token）

破解工具提取的 API key/token 写入此文件，dashboard 可热编辑。不入库（gitignored）。
当前字段：`copilot_token, copilot_personal_token, codebuddy_token, codebuddy_refresh_token, codebuddy_uid, codebuddy_nickname, trae_work_token, trae_work_refresh_token, trae_work_user_id, trae_work_bound_device_id, qclaw_api_key, qclaw_login_key, qclaw_guid, qclaw_user_id, qclaw_nickname, qclaw_openclaw_token, qclaw_device_token`

---

## 4. API 端点（多端口架构）

| 端口 | 供应商 | 分类 | handler | 协议 | 用途 |
|------|--------|------|---------|------|------|
| **8081** | anthropic-compatible | — | FastAPI | Anthropic | `/v1/messages`（Anthropic）/ dashboard 管理界面 / `/api/targets` 等 REST API |
| **8082** | copilot-enterprise | crack | copilot | OpenAI | GHE 企业版 Copilot（收费，上游 copilot-api.bmw.ghe.com，token 用企业 PAT） |
| **8083** | copilot | crack | copilot | OpenAI | 个人版 Copilot（上游 api.githubcopilot.com，token 从本地 `/root/.copilot` 破解，与 8082 账号隔离） |
| **8084** | codebuddy | crack | passthrough | OpenAI | CodeBuddy（上游 copilot.tencent.com，token 用 refresh 自动续期） |
| **8085** | qclaw | crack | qclaw | OpenAI | QClaw（上游 mmgrcalltoken.3g.qq.com，API Key 自动解密） |
| **8086** | trae-work | crack | trae-work | OpenAI | Trae Work（签到/额度/续期，OpenAI↔llm_utils_chat 转换） |
| **8090** | openrouter | free | passthrough | OpenAI | 免费代理（透传客户端 key） |
| **8091** | nvidia | free | passthrough | OpenAI | 免费代理 |
| **8092** | gemini | free | **gemini-native** | OpenAI↔Gemini | 原生 Gemini 协议转换（generateContent） |
| **8093** | opencode-zen | free | passthrough | OpenAI | 免费代理 |
| **8094** | open-go | paid | passthrough | OpenAI | 收费代理 |

- **base_url 规范**：crack 类与 gemini-native 统一 `/v1`（代理内部映射下游）；free/paid 透传用 `routePrefix`（如 `/api/v1`）
- **客户端接入**：OpenAI 协议 `base_url = http://<局域网IP>:8082/v1` 等，`api_key = "dummy"`（crack 类不校验；free/paid 用真实 key）；Anthropic 协议 `base_url = http://<局域网IP>:8081`

---

## 5. 破解网关模块族（本次新增核心）

### 5.1 模块清单与职责

| 模块 | 职责 |
|------|------|
| `crack_common.py` | 公共层：tc 加密解密 + `CREDENTIAL_SCHEMAS`（凭据表单 schema）+ `CRACK_STATUS_HANDLERS`（状态查询注册表）+ `get_crack_status`（dashboard 统一入口） |
| `crack_daily.py` | 统一每日任务调度器（插件化 `DAILY_HANDLERS`，**单一 cron 入口**） |
| `crack_traework.py` | Trae Work：tc 加密认证提取 + 签到/额度/刷新 CLI |
| `crack_copilot.py` / `crack_codebuddy.py` / `crack_qclaw.py` | 各网关 token 提取（gh CLI / 客户端目录探测 / Windows DPAPI 解密） |
| `crack_copilot_q.py` | Copilot 额度查询（企业 GHE + 个人版，仅标准库） |
| `crack_codebuddy_q.py` | CodeBuddy 额度 + 成长计划任务领取 |
| `crack_qclaw_q.py` | QClaw 积分查询（jprx.m.qq.com 逆向：4110/4075/4222） |

### 5.2 状态查询统一结构

各 `*_status()` 返回 `{"quota": [...], "checkin": {...}, "refresh": {...}, "extra": {...}}`，quota 条目为 `{"name", "limit", "used", "expireAt"}`。由 `crack_common.get_crack_status` 装配时补充 `displayName / account / capabilities / lastDailyRun`。

**handler 签名约定**：
- 标准：`handler(token, refresh_token)`（trae-work/codebuddy/copilot）
- 多字段：`handler(secrets)`（qclaw 需要 guid/userId/jwt/device 多个 secrets，由 `HANDLER_TAKES_SECRETS={"qclaw"}` 标记）

### 5.3 统一每日任务（crack_daily.py）

```bash
0 3 * * * /root/shared-workspace/claude-code-proxy/scripts/cron/crack_daily.sh
```

- `DAILY_HANDLERS` 注册表：trae-work（签到+刷新）/ codebuddy（成长任务+刷新）/ qclaw（仅校验 jwt）/ copilot（仅校验 token）
- **无 key 的网关自动跳过**；执行完写 `.cache/crack_daily_last_run` 时间戳（dashboard 状态区展示"最后定时刷新"）
- 日志 `/tmp/crack_daily.log`；`--only trae-work` 可只跑指定网关
- **勿新增其他 cron**——这是唯一每日调度入口

### 5.4 凭据管理（dashboard 凭据弹窗）

- `GET /api/crack/{label}/schema` → 返回该网关 `CREDENTIAL_SCHEMAS`（前端动态渲染表单 + JSON 双模式）
- `PUT /api/secrets/{label}/bulk` → 按 schema 校验（字段映射/pattern 校验/未知字段报错/只读字段忽略）
- **凭证最小原则**：qclaw 只 `qclaw_api_key` 必填（LLM 代理即可用）；其余字段为积分查询增强，缺字段时状态区显示降级提示

### 5.5 模型清理

- `POST /api/targets/{label}/prune-models` → 对照上游最新模型列表删过期模型（配置+内存）
- **保护 modelMapping 目标**：映射目标上游不存在时修正为同族可用模型，避免映射断裂
- 仅 copilot 系（handler=copilot）支持（上游有 /models）；codebuddy/qclaw/trae-work 不显示清理按钮

---

## 6. QClaw 特殊性（重要）

### API Key 自动解密

`server.py` 启动时自动从 QClaw 本地存储解密 API Key：
- 读取 `%APPDATA%\QClaw\app-store.json` 的 `authGateway.providers.qclaw.apiKey.cipherText`
- 读取 `%APPDATA%\QClaw\Local State` 的 `os_crypt.encrypted_key`
- DPAPI 解密 AES 密钥 → AES-256-GCM 解密 cipherText → 得到 `sk-...` API Key
- 环境变量 `QCLAW_API_KEY` 优先级最高

**QClaw 客户端只需登录过一次，代理就能自动拿到 Key**（除非用 `qclaw-local`）。

### 关键约束（三条铁律）

1. **User-Agent 必须 `OpenAI/JS 6.39.1`**（否则上游 400）
2. **所有 httpx 客户端 `trust_env=False`**（绕过系统代理）
3. **body 清理**：`_clean_qclaw_body()` 按 `_QCLAW_ALLOWED_KEYS` 白名单剔除非标准字段（否则 9002）

另：QClaw 模型必须先 `litellm.register_model`（`_qclaw_all_models`），否则报 "model isn't mapped"；QClaw 网关会过滤 usage 字段 → 代理用 tiktoken 本地估算注入。

### 19000 网关逆向结论

详见 [QCLAW_19000_GATEWAY_REVERSE.md](QCLAW_19000_GATEWAY_REVERSE.md)。核心结论：
- HMAC-SHA256 签名算法已破解（密钥 + payload 格式见文档）
- 19000 网关采用 OS 级 PID 反查（koffi FFI 调用 `GetExtendedTcpTable`）
- **独立签名不可行**：PID 由 Windows 内核管理，用户态无法伪造
- **唯一可行方案是寄生**：在 QClaw 进程内注入 HTTP 服务器（`qclaw_inject.js`）

---

## 7. 代码结构（server.py）

> 行号随版本漂移，以下按**功能模块**描述（实际位置用 `grep` 定位，不建议依赖行号）：

```
L1-60       模块导入 + load_dotenv + 日志配置
L60-165     tiktoken 本地 token 估算（QClaw 网关过滤 usage 时注入）
L265-469    httpx 客户端管理（trust_env=False）+ QClaw body 清理 + QClaw 直连透传
L473-566    FastAPI lifespan（启动诊断 + target 端口启动 + 破解工具自动调用）
L569-710    QClaw API Key DPAPI/AES 解密 + 环境变量
L713-770    _qclaw_all_models / _copilot_models + BIG/MEDIUM/SMALL + 全局状态变量
L772-882    模型级统计（_bump_model_stats）+ codebuddy 非流式聚合
L1092-1333  gemini-native：OpenAI↔generateContent 协议转换
L1339-1623  Trae Work 协议代理（_handle_traework）+ handler 认证注入
L1631-1680  _HANDLER_PATH_MAP + 路径重写（_rewrite_upstream_path）+ targets 加载
L1684-1854  配置热重载（mtime 轮询 2s）+ 破解工具调用
L2119-2334  统一 target 转发引擎（_handle_target_request）——多端口核心
L2339-2526  Provider 策略注册（_PROVIDER_STRATEGIES）+ 模型名映射
L3016-3585  Anthropic↔LiteLLM 双向转换（翻译层核心）
L3603-3828  /v1/chat/completions（透传 + LiteLLM 分流）
L3831-4829  流式响应处理（handle_streaming）+ /v1/messages + count_tokens
L5029-5774  dashboard HTML/CSS/JS（DASHBOARD_STYLE + _build_card_html）
L5782-6125  dashboard REST API（/api/targets、/api/secret 系列、/api/crack/*、/api/prune-models）
L6131-6957  dashboard 页面渲染主函数（826 行，含 KPI 卡/状态灯/凭据弹窗）
L6963-7060  catch_all 兜底 + 彩色请求日志 + 主入口
```

### Provider 策略机制（开闭原则）

`_PROVIDER_STRATEGIES` 字典注册了每个 provider 的处理函数（`_qclaw_provider`、`_copilot_provider` 等）。新增 provider 只需：
1. 在 `valid_providers` 元组中加名字
2. 写一个 `_xxx_provider(req, litellm_req, orig)` 函数
3. 注册到 `_PROVIDER_STRATEGIES`

### 透传 vs 翻译

- **透传**（qclaw/openai/copilot）：`/v1/chat/completions` 直接 httpx 转发，不经 LiteLLM，保留原始请求体
- **翻译**（anthropic/gemini）：经 LiteLLM 做格式转换和模型映射
- **gemini-native**（8092）：接受 OpenAI 请求，代理内部转换为 Google 原生 `generateContent` API
- **trae-work**（8086）：接受 OpenAI 请求，代理内部转换为 Trae `llm_utils_chat` API

### f-string 内嵌 JS 的花括号转义（重要防御）

dashboard 的 HTML/JS 是 Python f-string 内嵌，三条规定：
1. **JS 内的 `{` `}` 必须写 `{{` `}}`**（如 `(function() {{ ... }})`）
2. **CSS 抽成独立常量 `DASHBOARD_STYLE`**（普通字符串，HTML 用 `<style>{DASHBOARD_STYLE}</style>` 插值）——避免 CSS 花括号全部转义
3. **JS 内不用模板字符串 `${}`**，用 `+` 字符串拼接；SSE 事件用 `json.dumps()` 嵌入而非手写花括号

---

## 8. 开发工作流

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

使用 Conventional Commits：`feat:` / `fix:` / `docs:` / `chore:` / `refactor:` / `test:`（可带 scope，如 `fix(codebuddy):`）。
提交前用 `git status` 检查，不要 `git add .`（会带入 `.env` 等敏感文件）。

### 临时调研脚本

`.gitignore` 已忽略 `_*` 前缀的文件和目录。临时调研脚本用 `_` 开头，不会污染仓库。

### 调试模式

```bash
DEBUG=true .venv/bin/python server.py   # Linux
$env:DEBUG = "true"                     # Windows
```

会打印详细的请求/响应日志（包括 LiteLLM 内部字段、QClaw body 清理记录等）。**禁止往代码里加 print 调试**。

### 测试

本项目**无 pytest**（纯脚本式测试）：

```bash
.venv/bin/python test_targets_schema.py   # 纯单元测试（不依赖服务）
.venv/bin/python test_crack_tools.py      # crack 工具子进程测试
.venv/bin/python test_suite.py            # 集成测试（需 8082 运行中）
.venv/bin/python test_dashboard.py        # dashboard 验收（需 8081/8082 运行中）
```

---

## 9. 常见任务

### 排查 403/9002 错误

1. 检查 QClaw 是否登录过（`%APPDATA%\QClaw\app-store.json` 存在）
2. 检查启动日志 `🔑 QClaw API Key decrypted: sk-xxx...xxxx`
3. 如果 Key 解密失败，设 `QCLAW_API_KEY` 环境变量手动指定
4. 上游 400 → 检查 `User-Agent` 是否为 `OpenAI/JS 6.39.1`
5. `qclaw-local` 403 → 检查 19001 端口是否监听（寄生服务器是否注入）

### 排查代理不通

1. `ss -tlnp | grep :8081` — 主端口是否监听
2. `tail -20 proxy.log` — 查最近日志
3. 日志中 `startup diag: QClaw upstream = 200` 表示上游连通

### 查看网关额度/签到状态

dashboard（8081/dashboard）→ 各 crack 卡片状态区：`GET /api/crack/{label}/status` 返回额度/签到/token 到期/最后定时刷新。

### 清理过期模型

dashboard → 模型区 → "🧹 清理过期模型"按钮（仅 copilot 系显示）；或 `POST /api/targets/{label}/prune-models`。

---

## 10. 已知陷阱

1. **PATH 污染**：Trae IDE 的 ripgrep 会污染 PATH，导致 `Get-NetTCPConnection` 等 cmdlet 不可用。调用前先 `$env:Path = "C:\Windows\System32;C:\Windows"`。
2. **`.env` / `secrets.json` 不入库**：含密钥，`.gitignore` 已忽略。新环境需手动创建。
3. **QClaw 升级**：版本号硬编码在多处路径中，升级后需全局替换。
4. **LiteLLM 模型注册**：QClaw 模型名不在 LiteLLM 内置映射中，必须 `litellm.register_model()` 注册。
5. **QClaw body 清理**：客户端可能透传 Anthropic 专属字段（`thinking`、`reasoning_effort`、`output_config`），上游会 400。
6. **流式响应循环引用**：`qclaw_inject.js` 只复制 axios 请求拦截器，不复制响应拦截器。
7. **QClaw 网关过滤 usage**：上游响应没有 `usage` 字段，代理用 tiktoken 本地估算并注入。
8. **f-string 内嵌 JS**：花括号必须 `{{}}`，CSS 抽常量，JS 不用模板字符串（见 §7）。
9. **代理 mount namespace 隔离**：跨进程共享状态放仓库 `.cache/`，勿用 `/tmp`（代理读不到）。
10. **codebuddy 上游只支持流式**：非流式请求会报 `11101`，代理已自动聚合为流式 JSON。
11. **codebuddy refreshToken 轮换**：刷新后必须立即持久化新值，否则旧值失效导致登录态丢失。
12. **LXC 跑 Docker AppArmor 冲突**：手动 `docker run` 要加 `--security-opt apparmor=unconfined`（部署环境约束，见全局 CLAUDE.md）。

---

## 11. 相关文档

- [README.md](README.md) / [README-zh.md](README-zh.md) — 用户文档（provider 配置示例）
- [CHANGELOG.md](CHANGELOG.md) — 变更日志
- [DESIGN.md](DESIGN.md) — Dashboard 设计契约（色彩/圆角/组件/禁止项）
- [QCLAW_19000_GATEWAY_REVERSE.md](QCLAW_19000_GATEWAY_REVERSE.md) — 19000 网关逆向调研报告
- [docs/architecture.md](docs/architecture.md) — 多端口架构详解（targets.json schema）
- [docs/crack-tools.md](docs/crack-tools.md) — 破解工具与 OS 支持
- [docs/trae-work.md](docs/trae-work.md) — Trae Work 逆向文档（tc 加密/接口规范/签到/额度/续期）
- [docs/windows-deployment.md](docs/windows-deployment.md) — Windows 部署指南（8 个坑）
- [scripts/windows/AGENTS.md](scripts/windows/AGENTS.md) — Windows 自启脚本专项说明

---

## 12. Git 状态

- **主分支**：`main`（直接提交，不用 PR）
- **远程**：`https://github.com/wujiaxiang/claude-code-proxy.git`

---

## 13. Agent 行为准则

1. **改配置改 `.env` / `targets.json`**，不要改 VBS 或硬编码环境变量。
2. **新增功能先看 Provider 策略机制**（`_PROVIDER_STRATEGIES`）或 `CRACK_STATUS_HANDLERS` / `DAILY_HANDLERS` 注册表，遵循开闭原则。
3. **QClaw 相关改动**注意三个约束：`User-Agent`、`trust_env=False`、body 清理。
4. **临时脚本用 `_` 开头**，会被 gitignore 自动忽略。
5. **提交前** `git status` 检查，不要带入 `.env` / `*.log` / `.venv/` / `secrets.json`。
6. **调试**用 `DEBUG=true`，不要往代码里加 print。
7. **清理 PATH** 再调 Windows 命令，避免 PATH 污染导致 cmdlet 不可用。
8. **遇到 403/9002** 先看 [QCLAW_19000_GATEWAY_REVERSE.md](QCLAW_19000_GATEWAY_REVERSE.md)，不要重复逆向。
9. **改 dashboard** 注意 f-string 花括号转义三条规则（§7），改完 `python -c "import ast; ast.parse(open('server.py').read())"` 验证 + 重启代理截图确认。
10. **行号会漂移**：引用 server.py 位置时用功能模块描述 + grep，不写死行号。
