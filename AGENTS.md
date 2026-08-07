# AGENT.md

> 本文件是给 AI Agent（Claude Code / Cursor / Trae 等）的项目上下文速查。
> 在动手前先读一遍，避免重复踩坑。

---

## 1. 项目简介

**claude-code-proxy** 是一个 FastAPI 代理服务，让 Anthropic 客户端（如 Claude Code）能用多种后端（OpenAI / Gemini / Copilot Enterprise / QClaw / CodeBuddy / Trae Work）。

- **主入口**：`server.py`（框架层 + 入口，~4200 行，已下沉 HTTP 引擎/翻译层/模型注册表/错误翻译到 `server_http.py` 与 `gateways/*`）+ 网关实现层（`gateways/`）+ 管理面板层（`dashboard/`）。**配置驱动**——所有端口/供应商/模型由 `targets.json` 定义，改配置不动 server.py；改具体网关注辑进 `gateways/<网关>.py`，改 dashboard 进 `dashboard/routes.py`
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
| `proxy.log` | 项目根 | 主运行日志（不含 codebuddy / trae-work 的网关细节，见 §2.1） |
| `codebuddy.log` / `traework.log` | 项目根 | 网关独立日志（`propagate=False`，**不进 proxy.log**） |

### 启动 / 重启（Linux 用 systemd）

> Linux LXC 上代理由 systemd 托管（`claude-code-proxy.service`），**改代码/配置后一律用 systemctl 重启**，不要手动 kill + nohup。

```bash
# 重启（推荐，加载 server.py 改动 + targets.json）：
sudo systemctl restart claude-code-proxy

# 查看状态 / 实时日志：
systemctl status claude-code-proxy
tail -f /root/shared-workspace/claude-code-proxy/proxy.log

# 手动启动（仅调试，前台运行 8081 FastAPI + 所有 targets.json 端口）：
.venv/bin/python server.py
# Windows PowerShell：
# Set-Location "c:\Users\Administrator\claude-code-proxy-main"
# & ".\.venv\Scripts\python.exe" server.py
```

> systemd 细节：`ExecStart=.venv/bin/python3 server.py`，`Restart=always`（kill -9 也会被自动拉起）、`RestartSec=10`，日志追加到 `proxy.log`。**DEBUG 默认关闭**（无 `debug.conf`、`.env` 不设 `DEBUG`），日志级别为 INFO，`logger.debug()` 不输出——网关独立日志（`codebuddy.log` / `traework.log`）的逐请求诊断也随之静默，仅 INFO 及以上（如热重写命中）仍记录。需要排查时临时开：`systemctl edit claude-code-proxy` 加 `Environment=DEBUG=true` → `systemctl restart` → **查完记得恢复**（DEBUG 会把每个请求体/SSE 统计写盘，长期开启徒增磁盘与噪音）。

> **注意**：代理进程跑在独立 mount namespace，`/tmp` 与 shell 隔离——跨进程共享状态（如 crack_daily 时间戳）放仓库内 `.cache/`，勿用 `/tmp`。

### 2.1 日志分流架构（排查前必读）

日志**不是单一文件**。codebuddy / trae-work 两个网关有独立日志，且 `propagate=False`（不冒泡到 root），**内容不会出现在 proxy.log 里**。查错日志文件是最常见的时间浪费。

| 文件 | 写入方 | 内容 |
|------|--------|------|
| `proxy.log` | root logger | 启动诊断、配置热重载、路由、错误码翻译、LiteLLM 翻译层、其他所有端口 |
| `codebuddy.log` | `codebuddy_logger` | 8084 逐请求 model/stream/system 预览/body 摘要、SSE 帧统计、content_filter 拦截、聚合失败 |
| `traework.log` | `traework_logger` | 8086 同类细节 |

代码位置：`_setup_gateway_logger()`（server.py ~219）、`_GATEWAY_LOG_SUFFIX` 映射表。新增网关独立日志只需往该表加一行。

**关键：DEBUG 开关决定能看到什么**（`DEBUG` 默认关闭，级别 INFO）：

| 级别 | codebuddy.log 实际内容 |
|------|----------------------|
| INFO（默认） | 仅 `warning` 以上——content_filter 拦截、聚合失败。**逐请求日志与 SSE 统计全部静默** |
| DEBUG | 追加逐请求入站摘要（`model=... stream=... sys[:200]=... body[:300]=...`）、`SSE 透传完成: data_lines=N finish_reasons=[...] normalized=M` |

> **踩过的坑**：默认 INFO 下 `codebuddy.log` 可能长时间零增长（甚至文件 mtime 停在几小时前），这是**预期行为，不是日志链路坏了**。曾据此误判。想看逐请求细节必须先开 DEBUG。

**临时开 DEBUG**（查完务必恢复，否则每个请求体/SSE 统计都写盘）：

```bash
systemctl edit claude-code-proxy      # 加 Environment=DEBUG=true
systemctl restart claude-code-proxy
tail -f codebuddy.log                 # 查
# 查完：删掉该行 → systemctl restart（或 rm .service.d/override.conf）
```

轮转：三个文件共用 `LOG_ROTATE_WHEN` / `LOG_ROTATE_INTERVAL` / `LOG_RETENTION_DAYS` 策略（`TimedRotatingFileHandler`）。设了 `LOG_FILE` 时网关日志命名随主日志（`proxy.log` → `proxy-codebuddy.log`），未设时落在 server.py 同目录。

---

## 3. 配置文件

- **`.env`**（全局，gitignored）：**仅运行配置（非私密）**——`DEBUG` / `LOG_FILE` / `LOG_RETENTION_DAYS` / `LOG_ROTATE_WHEN` / `LOG_ROTATE_INTERVAL` / `COPILOT_GHE_HOST` / `COPILOT_INTEGRATION_ID` / `COPILOT_BIG|MEDIUM|SMALL_MODEL`。**已废弃**：`PREFERRED_PROVIDER`（多端口架构下不再控制路由，server.py 仍读取但无实际作用）。**私密凭据一律放 secrets.json**（2026-08-05 收敛）：`COPILOT_GHE_TOKEN` 已并入 secrets.json `copilot_token`，`CODEBUDDY_TOKEN` 冗余已删（target 走 secretRef）
- **`targets.json`**（Target 定义，核心配置）：必填 `label / listenPort / category / handler / targetHost`；category ∈ `crack|free|paid|aggregate`；handler ∈ `passthrough|copilot|qclaw|gemini-native|trae-work|aggregator`；crack 类必须有 `crackTool`；label/端口不能冲突；`enabled=false` 跳过必填校验（预留位）。secrets 优先级：`secrets.json > apikeyEnv 环境变量 > 客户端透传`。**热重载**：mtime 轮询 2s，改完即生效（含端口动态增删）。可选行为开关：`cleanCodebuddyBody` / `cleanQclawBody` / `normalizeSse`（SSE 帧规范化，修不合规上游）/ `normalizeFinishReason`。完整 schema 见 [docs/architecture.md](docs/architecture.md)
- **`secrets.json`**（私密 key/token，gitignored，dashboard 可热编辑）：**私密凭据唯一事实源**。破解工具提取的 key/token 写入此文件。当前字段：`copilot_token, copilot_personal_token, codebuddy_token, codebuddy_refresh_token, codebuddy_uid, codebuddy_nickname, trae_work_token, trae_work_refresh_token, trae_work_user_id, trae_work_bound_device_id, qclaw_api_key, qclaw_login_key, qclaw_guid, qclaw_user_id, qclaw_nickname, qclaw_openclaw_token, qclaw_device_token`。注：`copilot_token` 同时供 8082 企业 GHE target（secretRef）与 server.py 翻译层 `COPILOT_GHE_TOKEN`（同源 token，热重载同步）

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

各网关注册 `daily()`（trae-work 签到+刷新 / codebuddy 成长任务+刷新 / qclaw、copilot 仅校验）；无 key 自动跳过（不算失败）；时间戳写 `.cache/crack_daily_last_run`（dashboard 展示"最后定时刷新"）；日志 `logs/crack_daily.log`；`--only <网关>` 单跑、`--retry-delay 0` 加速调试。**勿新增其他 cron**——这是唯一每日调度入口。

**健壮性设计**（2026-08-05）：handler 签名统一为 `fn(secrets, out, secrets_path)`（调用点无分支，新增网关只加一行注册表）；失败自动重试一次（各 handler 均幂等：签到类先查状态再领取）；任一网关最终失败 → 退出码 1 → crontab 的 `||` 钩子写 `logs/crack_daily.alert`；`timeout 300` 防上游卡死拖垮整个任务。

> **完整约定与扩展方式见 `crack_daily.py` docstring**（离代码最近的单一事实源，勿在多处重复同一段约定）。

### 5.4 凭据管理（dashboard 凭据弹窗）

- `GET /api/crack/{label}/schema` → 返回该网关 `CREDENTIAL_SCHEMAS`（前端动态渲染表单 + JSON 双模式）
- `PUT /api/secrets/{label}/bulk` → 按 schema 校验写 secrets.json（字段映射 / pattern / 未知字段报错 / 只读字段忽略）
- **凭证最小原则**：qclaw 只 `qclaw_api_key` 必填（LLM 代理即可用）；其余字段为积分查询增强，缺字段时状态区显示降级提示

### 5.5 模型清理

- `POST /api/targets/{label}/prune-models` → 对照上游最新模型列表删过期模型（配置+内存）；**保护 models[] 目标**（映射目标上游不存在时修正为同族可用模型，避免映射断裂）
- 仅 copilot 系（handler=copilot）支持（上游有 /models）；codebuddy/qclaw/trae-work 不显示清理按钮

---

## 6. QClaw 特殊性（重要）

- **API Key 自动解密**：启动时从 `%APPDATA%\QClaw\app-store.json` + `Local State` 解密（DPAPI→AES-256-GCM），`QCLAW_API_KEY` 环境变量优先级最高；客户端登录过一次即可自动拿到 Key
- **三条铁律**：① User-Agent 必须 `OpenAI/JS 6.39.1`；② 所有 httpx 客户端 `trust_env=False`；③ body 按 `_QCLAW_ALLOWED_KEYS` 白名单清理
- 模型必须先 `litellm.register_model`（`_qclaw_all_models`）；网关过滤 usage 字段 → 代理 tiktoken 本地估算注入

🔗 解密链路 / 铁律全文 / 排查指南 → [docs/qclaw.md](docs/qclaw.md)；19000 网关逆向结论（PID 反查，唯一方案是寄生注入）→ [QCLAW_19000_GATEWAY_REVERSE.md](QCLAW_19000_GATEWAY_REVERSE.md)

---

## 7. 代码结构（多模块）

> 从单文件（原 server.py 10996 行）逐步拆分。当前为四层架构：框架层（server.py + server_http.py）/ 网关实现层（gateways/）/ 管理面板层（dashboard/routes.py）/ 破解层（crack_*.py）。行号随版本漂移，实际位置用 `grep` 定位，不建议依赖行号。

### 目录结构

- **server.py**（框架层 + 入口，~4200 行）：连接池、日志基础设施、路径重写（`_HANDLER_PATH_MAP`）、Anthropic Pydantic 模型、核心 API 端点（`/v1/chat/completions`、`/v1/messages`）、配置热重载、多端口分发核心（`_handle_target_request`/`_vendor_server`）、catch_all 兜底。已下沉的部分：HTTP 引擎→`server_http.py`、翻译层→`gateways/translate.py`、模型注册表→`gateways/models.py`、错误翻译→`gateways/errors.py`（server.py 顶部 re-export 这些符号，历史调用点零改动）
  - ⚠️ 顶部有主模块别名代码（`if __name__ == "__main__" and "server" not in sys.modules: sys.modules["server"] = sys.modules["__main__"]`），**禁止删除**（防止 gateways/dashboard 的延迟 import 触发 server 双加载）
- **server_http.py**（~370 行）：HTTP 转发引擎核心工具（`_parse_http_request`/`_write_response`/`_SseLineBuffer`/`_write_error_response`/`_write_response_with_status_override`）。server.py 通过 re-export 保持 `from server import _write_response` 对 gateways/* 继续有效
- **gateways/translate.py**（~370 行）：翻译层——`_PROVIDER_STRATEGIES` 全族 + OAI↔Anthropic 转换（`_convert_oai_to_anthropic`）+ token 估算族 + `_close_json_fragment`/`clean_gemini_schema`。provider 策略字典用 PEP 562 惰性引用避免循环导入
- **gateways/models.py**（~680 行）：模型注册表全族（`_get_target_models`/`_build_models_list`/`_fetch_downstream_models`/`_fetch_live_models`/`_scan_dangling_refs`/`ModelRegistry` 等）。跨模块依赖：函数内 `from server import X` 延迟导入 + `import server as _srv` 访问热重载全局（`_TARGETS`/`_SECRETS`/`_MODELS_CFG`）；`_DOWNSTREAM_MODELS_CACHE` 归本模块所有，server.py 经 `import gateways.models as _gmodels` 实时读取
- **gateways/errors.py**（~100 行）：错误翻译族（`_map_upstream_error`/`_vendor_body_retryable`/`_is_rate_limit_error`/`_is_auth_expired_error` + `_VENDOR_ERROR_MAPS`/`_VENDOR_ERROR_PATTERNS`/`_VENDOR_RETRY_AFTER`）
- **gateways/qclaw.py**：QClaw 解密 + body 清洗 + 透传（`_qclaw_provider`）；改 QClaw 逻辑进这里
- **gateways/codebuddy.py**：CodeBuddy body 清洗 + 流聚合 + SSE 规范化；改 CodeBuddy 逻辑进这里
- **gateways/trae_work.py**：Trae Work 协议转换 + DSML 解析器族（含 5 套工具调用文本标记解析器，最大网关）；改 Trae Work 逻辑进这里
- **gateways/gemini_native.py**：Gemini 原生协议转换
- **gateways/copilot.py**：Copilot Responses API 转换 + GHE 配置
- **gateways/aggregator/engine.py**：聚合网关纯引擎逻辑（`AggregatorEngine`，路由 / 会话粘性 / 熔断 / 降级）
- **gateways/aggregator/http_adapter.py**：聚合网关 HTTP 适配（`_handle_aggregate_request`/`_aggregator_prober`）
  - ⚠️ `_AGGREGATOR_ENGINE` 全局通过 `import server as _srv` + `_srv._AGGREGATOR_ENGINE` 模块属性共享
- **dashboard/routes.py**（~3200 行）：管理面板全套（`DASHBOARD_STYLE` + HTML 渲染 + 18 个 `/api/*` 路由，FastAPI `APIRouter`，server.py 里 `app.include_router(dashboard_router)` 挂载）；改 dashboard 进这里
  - ⚠️ **当前最大单一文件**，下一步瘦身头号目标（HTML 渲染 / 各 `/api/*` 路由可按卡片拆分到 `dashboard/` 子模块）
- **config_store.py**（~360 行）：`targets.json` 加载/迁移/校验、secrets.json 读写、热重载（独立模块）

### 跨模块约定（新增，拆分后必须遵守）

1. 网关模块 / dashboard 对 server 的共享依赖（logger、`get_http_client`、`_cfg` 等）用**函数内延迟导入** `from server import X`——server.py 顶部主模块别名保证不双加载
2. 热重载可变全局（`_TARGETS`/`_SECRETS`/`_AGGREGATOR_ENGINE` 等）跨模块访问必须用 `import server as _srv` + `_srv.X` 模块属性方式，**禁止 `from server import X`**（值拷贝会在热重载后读到旧快照）
3. 新增网关 = 在 `gateways/` 建一个模块 + server.py 注册（开闭原则）

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
- **排查 codebuddy / trae-work 网关行为**（请求被拦、SSE 异常、工具调用不识别）：**先看对应的独立日志**（`codebuddy.log` / `traework.log`），不是 proxy.log——两者 `propagate=False` 不互通（见 §2.1）。默认 INFO 下逐请求细节不写盘，需临时开 DEBUG
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
7. **上游 SSE 帧不可假设合规**：codebuddy 上游思考帧夹带空 `content`、正文帧夹带空 `reasoning_content`、`finish_reason` 用空串而非 `null`（已由 `normalizeSse` 修正，见 docs/codebuddy.md §3.5）。**改写 SSE 的三条硬约束**：① 必须先用 `_SseLineBuffer` 重组跨 chunk 的半截帧再改写（纯透传时帧被切断无所谓，改写时不重组会切坏 JSON）；② 诊断统计必须基于改写**前**的原始行，否则规范化自身的 bug 会掩盖上游真实异常；③ 解析失败一律原样透传，绝不吞帧或中断流（流断了比渲染难看严重得多）。清洗范围以**值是否为空**为准（`content`/`reasoning_content`/`tool_calls`/`function_call`/`refusal`/`extra_fields` 的空值都要删），有内容的结构字段绝不动。
   > **别想当然"保留字段更安全"**：首版修复特意保留 `tool_calls:[]` 等空字段（怕破坏依赖键存在性的客户端），结果恰恰是它导致 opencode（Vercel AI SDK）把思考链切成数百块——SDK 见 `tool_calls` 键即认为工具调用段开始。改 SSE 前先确认客户端用哪个 SDK、按什么规则分段。
8. **排查协议类问题先抓原始字节**：本次思考链换行 bug 曾误判为"流式追加拼接错误"，实际是帧结构问题。有效方法是 `iter_bytes()` 打印原始 SSE 的 `repr()` 逐字段对比标准协议，并用**同一响应内正常阶段作对照**（正文同样逐词发送却无问题 → 锁定唯一结构差异）。

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
