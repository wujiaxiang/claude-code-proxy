# AGENT.md

> 本文件是给 AI Agent（Claude Code / Cursor / Trae 等）的项目上下文速查。
> 在动手前先读一遍，避免重复踩坑。

---

## 1. 项目简介

**claude-code-proxy** 是一个 FastAPI 代理服务，让 Anthropic 客户端（如 Claude Code）能用多种后端（OpenAI / Gemini / Copilot Enterprise / QClaw / CodeBuddy / Trae Work）。

- **主入口**：`server.py`（单文件 ~7000 行，多端口代理引擎 + dashboard 一体化，**配置驱动**——所有端口/供应商/模型由 `targets.json` 定义，改配置不动 server.py）
- **依赖**：Python 3.10+ / fastapi / uvicorn / httpx / litellm / python-dotenv / tiktoken / pydantic（虚拟环境 `.venv/`，Windows 用 `.venv\Scripts\python.exe`）
- **配置模块**：`config_store.py`（targets.json 加载/迁移/校验、secrets.json 读写、热重载）

---

## 2. 快速启动

### 当前部署环境（Linux LXC + Windows Server 双环境）

| 环境 | 路径 | 用途 |
|------|------|------|
| Linux LXC（本机） | `/root/shared-workspace/claude-code-proxy/` | 开发/测试主环境 |
| Windows Server | `C:\Users\Administrator\claude-code-proxy-main\` | 生产部署（计划任务 `\ClaudeCodeProxy` 登录时触发 VBS） |
| `.env` / `secrets.json` | 项目根 | 全局配置 / 私密 token（**gitignored**，dashboard 可热编辑） |
| `proxy.log` | 项目根 | 运行日志 |

### 手动启动

```bash
# Linux：启动 8081 FastAPI + 所有 targets.json 端口
.venv/bin/python server.py
# Windows PowerShell：
# Set-Location "c:\Users\Administrator\claude-code-proxy-main"
# & ".\.venv\Scripts\python.exe" server.py

# 重启（Linux）：
PID=$(ss -tlnp | grep ':8081 ' | grep -oP 'pid=\K[0-9]+'); [ -n "$PID" ] && kill -9 $PID
nohup .venv/bin/python server.py > /tmp/proxy.log 2>&1 & disown
```

> **注意**：代理进程跑在独立 mount namespace，`/tmp` 与 shell 隔离——跨进程共享状态（如 crack_daily 时间戳）放仓库内 `.cache/`，勿用 `/tmp`。

---

## 3. 配置文件

- **`.env`**（全局，gitignored）：`DEBUG` / `LOG_FILE` / `LOG_RETENTION_DAYS` / `LOG_ROTATE_WHEN` / `LOG_ROTATE_INTERVAL` + 各网关 apikeyEnv（`COPILOT_GHE_TOKEN` 等）。**已废弃**：`PREFERRED_PROVIDER`（多端口架构下不再控制路由，server.py 仍读取但无实际作用）
- **`targets.json`**（Target 定义，核心配置）：必填 `label / listenPort / category / handler / targetHost`；category ∈ `crack|free|paid|aggregate`；handler ∈ `passthrough|copilot|qclaw|gemini-native|trae-work|aggregator`；crack 类必须有 `crackTool`；label/端口不能冲突；`enabled=false` 跳过必填校验（预留位）。secrets 优先级：`secrets.json > apikeyEnv 环境变量 > 客户端透传`。**热重载**：mtime 轮询 2s，改完即生效（含端口动态增删）。完整 schema 见 [docs/architecture.md](docs/architecture.md)
- **`secrets.json`**（私密 key/token，gitignored，dashboard 可热编辑）：破解工具提取的 key/token 写入此文件。当前字段：`copilot_token, copilot_personal_token, codebuddy_token, codebuddy_refresh_token, codebuddy_uid, codebuddy_nickname, trae_work_token, trae_work_refresh_token, trae_work_user_id, trae_work_bound_device_id, qclaw_api_key, qclaw_login_key, qclaw_guid, qclaw_user_id, qclaw_nickname, qclaw_openclaw_token, qclaw_device_token`

---

## 4. API 端点（多端口架构）

> 权威端口表（targets.json 驱动）见 [docs/architecture.md](docs/architecture.md)；下表为高频速查。

| 端口 | 供应商 | 分类 | handler | 协议 | 用途 |
|------|--------|------|---------|------|------|
| **8080** | aggregator | aggregate | aggregator | OpenAI | 聚合网关（虚拟模型路由 / 会话粘性 / 重试降级 / 配额熔断，路由到本地各真实端口） |
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

## 5. 破解网关模块族

### 5.1 模块清单（索引）

| 模块 | 职责 | 文档 |
|------|------|------|
| `crack_common.py` | 公共层：tc 加解密 + `CREDENTIAL_SCHEMAS` + `CRACK_STATUS_HANDLERS` + `get_crack_status`（dashboard 统一入口） | [crack-tools.md](docs/crack-tools.md) |
| `crack_daily.py` | 统一每日任务调度器（`DAILY_HANDLERS`，**单一 cron 入口**） | [crack-tools.md](docs/crack-tools.md) |
| `crack_copilot.py` + `crack_copilot_q.py` | token 提取（gh CLI，跨平台）+ 额度查询（企业 GHE + 个人版） | [copilot.md](docs/copilot.md) |
| `crack_codebuddy.py` + `crack_codebuddy_q.py` | token 提取（客户端目录探测）+ 额度 / 成长计划任务领取 | [codebuddy.md](docs/codebuddy.md) |
| `crack_qclaw.py` + `crack_qclaw_q.py` | token 提取（Windows DPAPI 解密）+ 积分查询（jprx.m.qq.com 逆向） | [qclaw.md](docs/qclaw.md) |
| `crack_traework.py` | tc 加密认证提取 + 签到 / 额度 / 刷新 CLI | [trae-work.md](docs/trae-work.md) |

### 5.2 状态查询统一结构

各 `*_status()` 返回 `{"quota": [...], "checkin": {...}, "refresh": {...}, "extra": {...}}`，由 `crack_common.get_crack_status` 装配时补充 `displayName / account / capabilities / lastDailyRun`。handler 签名：标准 `handler(token, refresh_token)`；qclaw 多字段 `handler(secrets)`（`HANDLER_TAKES_SECRETS={"qclaw"}` 标记）。详见 [docs/crack-tools.md](docs/crack-tools.md)。

### 5.3 统一每日任务（crack_daily.py）

```bash
0 3 * * * /root/shared-workspace/claude-code-proxy/scripts/cron/crack_daily.sh
```

各网关注册 `daily()`（trae-work 签到+刷新 / codebuddy 成长任务+刷新 / qclaw、copilot 仅校验）；无 key 自动跳过；时间戳写 `.cache/crack_daily_last_run`（dashboard 展示"最后定时刷新"）；日志 `/tmp/crack_daily.log`；`--only <网关>` 单跑。**勿新增其他 cron**——这是唯一每日调度入口。

### 5.4 凭据管理（dashboard 凭据弹窗）

- `GET /api/crack/{label}/schema` → 返回该网关 `CREDENTIAL_SCHEMAS`（前端动态渲染表单 + JSON 双模式）
- `PUT /api/secrets/{label}/bulk` → 按 schema 校验写 secrets.json（字段映射 / pattern / 未知字段报错 / 只读字段忽略）
- **凭证最小原则**：qclaw 只 `qclaw_api_key` 必填（LLM 代理即可用）；其余字段为积分查询增强，缺字段时状态区显示降级提示

### 5.5 模型清理

- `POST /api/targets/{label}/prune-models` → 对照上游最新模型列表删过期模型（配置+内存）；**保护 modelMapping 目标**（映射目标上游不存在时修正为同族可用模型，避免映射断裂）
- 仅 copilot 系（handler=copilot）支持（上游有 /models）；codebuddy/qclaw/trae-work 不显示清理按钮

---

## 6. QClaw 特殊性（重要）

- **API Key 自动解密**：启动时从 `%APPDATA%\QClaw\app-store.json` + `Local State` 解密（DPAPI→AES-256-GCM），`QCLAW_API_KEY` 环境变量优先级最高；客户端登录过一次即可自动拿到 Key
- **三条铁律**：① User-Agent 必须 `OpenAI/JS 6.39.1`；② 所有 httpx 客户端 `trust_env=False`；③ body 按 `_QCLAW_ALLOWED_KEYS` 白名单清理
- 模型必须先 `litellm.register_model`（`_qclaw_all_models`）；网关过滤 usage 字段 → 代理 tiktoken 本地估算注入

🔗 解密链路 / 铁律全文 / 排查指南 → [docs/qclaw.md](docs/qclaw.md)；19000 网关逆向结论（PID 反查，唯一方案是寄生注入）→ [QCLAW_19000_GATEWAY_REVERSE.md](QCLAW_19000_GATEWAY_REVERSE.md)

---

## 7. 代码结构（server.py）

> 单文件 ~7000 行；行号随版本漂移，实际位置用 `grep` 定位，不建议依赖行号。功能模块分组：

```
启动链   日志配置 → tiktoken 本地估算（QClaw usage 注入）→ httpx 客户端管理（trust_env=False）+ QClaw body 清理
         → lifespan（启动诊断 + target 端口启动 + 破解工具调用）→ QClaw API Key DPAPI/AES 解密 → 模型注册 + 全局状态
转发链   路径重写（_rewrite_upstream_path + _HANDLER_PATH_MAP）+ targets 加载 → 配置热重载（mtime 轮询 2s）
         → 统一 target 转发引擎（_handle_target_request，多端口核心）→ Provider 策略（_PROVIDER_STRATEGIES）
         → Anthropic↔LiteLLM 双向转换（翻译层核心）→ /v1/chat/completions → 流式响应（handle_streaming）+ /v1/messages
网关专属  gemini-native 协议转换 · trae-work 协议代理（_handle_traework）· codebuddy 非流式聚合 · 模型级统计（_bump_model_stats）
Dashboard  HTML/CSS/JS（DASHBOARD_STYLE + _build_card_html）→ REST API（/api/targets、/api/secret 系列、/api/crack/*、/api/prune-models）
         → 页面渲染主函数 → catch_all 兜底 + 彩色请求日志 + 主入口
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

### 其他约定

- **Git 提交**：Conventional Commits（`feat:`/`fix:`/`docs:`/`chore:`/`refactor:`/`test:`，可带 scope）。提交前 `git status` 检查，不要 `git add .`（会带入 `.env` 等敏感文件）
- **临时调研脚本**：`.gitignore` 已忽略 `_*` 前缀的文件和目录，临时脚本用 `_` 开头
- **调试**：`DEBUG=true .venv/bin/python server.py`（Windows `$env:DEBUG = "true"`），打印详细请求/响应日志。**禁止往代码里加 print 调试**
- **测试**：本项目**无 pytest**（纯脚本式）：`test_targets_schema.py`（纯单元）/ `test_crack_tools.py`（crack 子进程）/ `test_suite.py`（集成，需 8082）/ `test_dashboard.py`（dashboard 验收，需 8081/8082）

---

## 9. 常见任务

- **排查 403/9002 错误**（QClaw）：登录态/解密 Key/UA/19000 寄生四步 → [docs/qclaw.md](docs/qclaw.md) §7 排查指南
- **排查代理不通**：`ss -tlnp | grep :8081` 主端口是否监听 → `tail -20 proxy.log` 查最近日志 → 日志中 `startup diag: QClaw upstream = 200` 表示上游连通
- **查看网关额度/签到状态**：dashboard（8081/dashboard）→ 各 crack 卡片状态区；`GET /api/crack/{label}/status` 返回额度/签到/token 到期/最后定时刷新
- **清理过期模型**：dashboard → 模型区 → "🧹 清理过期模型"按钮（仅 copilot 系显示）；或 `POST /api/targets/{label}/prune-models`

---

## 10. 已知陷阱

**跨网关/代码级规则（保留全文）**：

1. **PATH 污染**：Trae IDE 的 ripgrep 会污染 PATH，导致 `Get-NetTCPConnection` 等 cmdlet 不可用。调用前先 `$env:Path = "C:\Windows\System32;C:\Windows"`。
2. **`.env` / `secrets.json` 不入库**：含密钥，`.gitignore` 已忽略。新环境需手动创建。
3. **f-string 内嵌 JS**：花括号必须 `{{}}`，CSS 抽常量，JS 不用模板字符串（见 §7）。
4. **代理 mount namespace 隔离**：跨进程共享状态放仓库 `.cache/`，勿用 `/tmp`（代理读不到）。
5. **LXC 跑 Docker AppArmor 冲突**：手动 `docker run` 要加 `--security-opt apparmor=unconfined`（部署环境约束，见全局 CLAUDE.md）。
6. **聚合网关（8080）**：熔断配额模式（`quotaErrorPatterns`，额度/积分不足 → 摘除端口）与 429 限流模式（`_VENDOR_ERROR_PATTERNS`，只翻译不熔断）必须严格区分，新增错误特征时先判断归哪一类；聚合层**不透传 secretRef/apikeyEnv**，只透传客户端 `Authorization`（凭据归各下游端口自己处理）；会话粘性 key = `(虚拟模型id, session_id)`，改粘性逻辑勿动这个隔离约定。

**网关级陷阱（详见各网关文档）**：

- **qclaw**（升级路径/模型注册/body 清理/注入循环/usage 过滤）→ [docs/qclaw.md](docs/qclaw.md) §8 已知陷阱
- **codebuddy**（仅流式 11101/refreshToken 轮换）→ [docs/codebuddy.md](docs/codebuddy.md) §6 已知陷阱
- **trae-work**（模型列表同步/传图白名单）→ [docs/trae-work.md](docs/trae-work.md) §5.3

---

## 11. 相关文档

- [README.md](README.md) / [README-zh.md](README-zh.md) — 用户文档（provider 配置示例）
- [CHANGELOG.md](CHANGELOG.md) — 变更日志
- [DESIGN.md](DESIGN.md) — Dashboard 设计契约（色彩/圆角/组件/禁止项）
- [QCLAW_19000_GATEWAY_REVERSE.md](QCLAW_19000_GATEWAY_REVERSE.md) — 19000 网关逆向调研报告
- [docs/architecture.md](docs/architecture.md) — 多端口架构详解（targets.json schema + **权威端口表**）
- [docs/qclaw.md](docs/qclaw.md) — QClaw：解密链路 / 三条铁律 / 积分查询 / 排查 / 已知陷阱
- [docs/copilot.md](docs/copilot.md) — Copilot 双模式（企业/个人）/ token 提取 / 额度 / 模型清理
- [docs/codebuddy.md](docs/codebuddy.md) — CodeBuddy：refreshToken 轮换 / 11101 聚合 / 成长任务 / 已知陷阱
- [docs/trae-work.md](docs/trae-work.md) — Trae Work 逆向：tc 加密 / 接口规范 / 签到 / 额度 / 续期 / 传图
- [docs/crack-tools.md](docs/crack-tools.md) — 破解公共层索引（模块 / secrets 字段 / 状态查询 / 每日任务）
- [docs/windows-deployment.md](docs/windows-deployment.md) — Windows 部署指南（8 个坑）
- [scripts/windows/README.md](scripts/windows/README.md) — Windows 启动脚本目录说明（开发约定）

---

## 12. Git 状态

- **主分支**：`main`（直接提交，不用 PR）；远程：`https://github.com/wujiaxiang/claude-code-proxy.git`

---

## 13. Agent 行为准则

1. **改配置改 `.env` / `targets.json`**，不要改 VBS 或硬编码环境变量。
2. **新增功能先看 Provider 策略机制**（`_PROVIDER_STRATEGIES`）或 `CRACK_STATUS_HANDLERS` / `DAILY_HANDLERS` 注册表，遵循开闭原则。
3. **QClaw 相关改动**注意三个约束：`User-Agent`、`trust_env=False`、body 清理。
4. **改 dashboard** 注意 f-string 花括号转义三条规则（§7），改完 `python -c "import ast; ast.parse(open('server.py').read())"` 验证 + 重启代理截图确认。
