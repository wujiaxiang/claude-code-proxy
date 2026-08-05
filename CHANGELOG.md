# Changelog

## [Unreleased]

### Added
- **SSE 帧规范化层（`normalizeSse`，2026-08-05）**：passthrough 端口新增可选的上游 SSE 帧清洗能力，用于修正不合规上游。配置驱动（targets.json `normalizeSse` / `normalizeFinishReason`），不硬编码 label；当前对 codebuddy 启用
  - `_SseLineBuffer`：按 `\n` 切行的缓冲器，重组跨 TCP chunk 的半截 SSE 帧。**纯字节透传时无需要，但一旦要逐帧改写就必须先重组**，否则会切坏 JSON（原诊断逻辑按 chunk 边界 `splitlines()` 只影响日志准确性，改写模式下则会污染数据流）
  - `_normalize_codebuddy_sse_line`：逐帧规范化，保守策略——只删空 `content`/`reasoning_content`、`finish_reason:""` 归一为 `null`；**不动** `tool_calls`/`refusal`/`function_call`/`extra_fields`（对目标问题零收益，却可能破坏依赖"键存在性"做类型推断的客户端）
  - 三条工程红线：① 诊断统计基于**改写前**的原始行（否则规范化自身的 bug 会掩盖上游真实异常）；② 解析失败/畸形帧/`[DONE]`/keep-alive 一律原样透传，绝不吞帧或中断流（流断了比渲染难看严重得多）；③ 未改动的帧返回原对象，保住大部分帧的零序列化开销
- **聚合网关 8080 端口**（`aggregator.py` + `config_store.py` aggregate/aggregator 支持）：
  - 虚拟模型 id（agg:xxx）→ 默认池（权重/平等）+ 降级池（可为空）路由，可配置重试次数
  - 会话保持（`(虚拟模型id, session_id)` → 成员，本地缓存 + TTL），防止同会话漂移丢缓存
  - 熔断摘除：下游响应匹配 `quotaErrorPatterns`（额度/积分不足）→ 按端口摘除全部模型，dashboard 状态展示 + 每 300s 最小探测恢复；与 429 限流（`_VENDOR_ERROR_PATTERNS`）严格区分
  - 监控：`/api/aggregate/status`（每成员请求/成功/失败/降级/延迟 + 会话命中率 + 熔断状态），dashboard 聚合卡片 10s 自动刷新
- **8081 转发目标可配置**（`anthropicForward`）：`defaultPort` + 按模型 `modelMap` 映射（可指向聚合模型 agg:xxx 或非聚合），dashboard「转发配置」编辑
- **dashboard 配置编辑**：8080「编辑配置」modal（虚拟模型/池成员/权重/retries 增删改）+ 8081「转发配置」modal；卡片头/详情区与其他卡片统一（监控视角为主）
- **trae-work 模型列表自动同步**（`_trae_fetch_models`）：`/v1/models` 从上游 `get_detail_param` 实时拉取（TTL 5 分钟），过滤 `__dev`/不可用/隐藏，失败回退配置 → 静态列表
- **trae-work 传图支持**：`_openai_to_trae_body` 按标准 OpenAI `image_url:{url}` 格式透传；图片能力仅对内置多模态模型开放（`Doubao_1_6`/`qwen-3.7-plus`/`minimax-m3`/`Doubao-Seed-2.0-Code` 实测成功）
- **破解网关额度/签到状态查询**（dashboard `GET /api/crack/{label}/status`）：`crack_*_q.py` 模块族 + `crack_common.CRACK_STATUS_HANDLERS` 注册表
  - `crack_copilot_q.py`：copilot-enterprise（GHE，`api.bmw.ghe.com/copilot_internal/user`）+ copilot 个人版（`api.github.com`，quota_snapshots）
  - `crack_qclaw_q.py`：QClaw 积分余额（`jprx.m.qq.com/data/4110/forward`）+ 今日 token（4075）+ 流水（4222），认证从 177 提取（userInfo/jwtToken/device-id）
  - `crack_codebuddy_q.py`：CodeBuddy 资源包额度（`billing/meter/get-user-resource`）+ 成长计划任务/连续天数（`/v2/activity/growth/*`）
- **8083 个人版 copilot 端口**：上游 `api.githubcopilot.com`，token 从本地 `/root/.copilot/config.json` 破解（`copilot_personal_token`），与 8082 企业账号完全隔离
- **统一每日任务 `crack_daily.py`**：插件化调度器（DAILY_HANDLERS 注册表），单一 cron 入口 `scripts/cron/crack_daily.sh`（每日 03:00），无 key 网关自动跳过
- dashboard `isFree` 标签尊重 targets.json 显式配置（crack 类可标记"收费"）
- 横向扩展多端口架构：一端口一供应商（8082 copilot / 8084 codebuddy / 8085 qclaw / 8086 trae-work 预留 / 8090-8094 免费代理）
- targets.json 新 schema（category/isFree/handler/crackTool/secretRef/enabled/modelMapping/reasoning）
- secrets.json 私密 key/token 存储（gitignore，dashboard 热更新）
- 独立破解工具 crack_qclaw / crack_codebuddy / crack_copilot / crack_traework（统一 CLI）
- dashboard 管理界面：REST API（/api/targets、/api/secrets、/api/reload、recrack）+ token/isFree 编辑表单
- 配置热重载：mtime 轮询（2s），端口动态增删，无需重启
- **gemini-native handler**（8092）：OpenAI ↔ Google 原生 generateContent 协议转换（流式/非流式/工具/image inline_data），替代旧 OpenAI 兼容端点
- **dashboard 分类栏**：聚合网关（8081）/ 破解网关 / 直连网关 三组，带数量徽标
- **模型编辑弹框**：modal + iOS 滑动开关 + 总开关（全开/全关/部分开 indeterminate）+ 搜索框 + 编辑态自动拉取下游真实模型列表（失败降级配置）
- **crackEnv 环境检测**：破解按钮按当前 OS/依赖置灰（非 Windows 的 codebuddy/qclaw 提示"仅支持 Windows，待后续补齐"）
- **8081 anthropic-compatible 统计**：卡片改名 + `/v1/messages` 请求数与模型级统计（中间件记录）
- **可粘贴 base_url**：卡片详情显示局域网 IP + 端口 + 后缀（crack/gemini 统一 /v1，free/paid 透传 routePrefix）
- **Windows 启动脚本子目录化**：scripts/windows/（VBS/BAT/PS1 内部动态定位项目根，不再硬编码路径）

### Changed
- 8082 从 PREFERRED_PROVIDER 动态切换改为固定 copilot
- qclaw 配置从 .env 迁入 targets.json 的 8085 条目
- 8081 转发目标由 anthropicForwardPort 配置（默认 8082）
- 8092 gemini-openai → gemini（handler=gemini-native）
- README 精简为多端口架构视角，Windows 部署坑拆至 docs/windows-deployment.md
- **配置架构收敛（2026-08-05）**：私密凭据唯一事实源 = `secrets.json`（dashboard 热编辑 + mtime 热重载 2s 生效）。`.env` 只留非私密运行配置（`DEBUG`/`LOG_*`/`COPILOT_GHE_HOST`/`COPILOT_INTEGRATION_ID`/`COPILOT_*_MODEL`/`PREFERRED_PROVIDER`）。`COPILOT_GHE_TOKEN` 并入 secrets.json `copilot_token`（同源，server.py 翻译层 `_load_vendor_targets`/`_reload_targets`/`_refresh_secrets` 热重载同步）；`.env` 冗余 `CODEBUDDY_TOKEN` 已删（target 走 secretRef）
- `test_qclaw_429.py` → `test_error_code_mapping.py`：覆盖面早已超出 qclaw（多网关多格式错误码映射 + LiteLLM 限流路径），原名有误导性；同步更新 `docs/error-code-mapping.md`
- **每日任务 handler 签名统一**（`crack_daily.py`）：全部改为 `fn(secrets, out, secrets_path=None)`，删除 `main()` 里 `if label in ("trae-work","codebuddy")` 的硬编码分流。此前两类签名并存，新增网关需同步改 `main()` 分支（易漏）；现调用点无分支，扩展只需在 `DAILY_HANDLERS` 加一行
- 调度约定收敛为单一事实源：完整扩展方式与设计理由写入 `crack_daily.py` docstring（含"为什么是单一 cron 而非 systemd timer / APScheduler"及升级触发条件），`AGENTS.md` §5.3 与 `docs/crack-tools.md` 只保留速查与指针，避免同一段约定在多处重复导致改动不同步

### Removed
- **`scripts/cron/trae_work_daily.sh`**（死代码）：功能早已被 `crack_daily.py` 的 `daily_traework()` 完全覆盖且实现更优（旧脚本无脑调 `--claim`，新实现先 `checkin_status()` 判断今日是否已签到，幂等）。crontab / systemd / 代码均无引用，唯一作用是让人误以为 trae 签到归它管。同步清理 `docs/trae-work.md` 两处过时引用（§6.3 自动续期、代码位置表）
- `_write_response` 的 `log_sse` 分支从"按 chunk 边界 splitlines 只读诊断"改为"行缓冲逐帧处理"，为规范化提供正确的分帧基础；诊断日志新增 `normalized=N` 计数

### Fixed
- **每日任务调度健壮性缺口（2026-08-05）**：架构评审后修复四处隐患——① **失败完全静默**：`main()` 恒返回 0 且 handler 结果被丢弃，任务挂了无人知晓（可能连续多天错过签到才发现）。现按 handler result 里的 `error` 判定失败并返回退出码 1，crontab 的 `||` 钩子追写 `logs/crack_daily.alert`；② **日志放 `/tmp`**：容器重启即清空，且违反项目自己定的"跨进程状态勿用 /tmp"约定（代理 `PrivateTmp=true` 根本读不到），迁至仓库内 `logs/crack_daily.log`；③ **无超时上限**：某网关上游卡死会拖垮整个 daily，外层加 `timeout 300`（超时码 124 同样触发告警）；④ **无重试**：网络抖动导致当天签到失败即错过，现失败自动重试一次（`--retry-delay` 可调，各 handler 均幂等——签到类先查状态再领取）
- **codebuddy 思考链逐 token 换行（2026-08-05，kimi-k3-1 等 reasoning 模型）**：客户端渲染思考链时每个 token 被切成独立段落。根因**不在拼接逻辑，而在帧结构透传**——上游 `copilot.tencent.com` 返回的 SSE 帧不符合 OpenAI 协议：思考阶段每帧夹带 `"content":""`（实测 465/465 帧命中），正文阶段反过来夹带 `"reasoning_content":""`，且 `finish_reason` 用空串而非 `null`。8084 是 `passthrough` 纯字节转发（`aiter_bytes()`），畸形帧原样透传给客户端；客户端见 `content` 键即认为正文块开始 → 结束当前思考段 → 下一帧再开新段 → 逐字换行。**定位关键佐证**：正文阶段同样逐词发送却无此现象，两者唯一结构差异就是思考帧多了空 `content`，据此排除了"客户端把每个 SSE 帧当一行渲染"的可能。修复见 Added 的 SSE 帧规范化层。实测思考帧空 content 465→0、正文帧空 reasoning 67→0、finish_reason 空串 586→0，思考链与正文内容完整无损
- openrouter 免费池限流文案未被识别 —— `_VENDOR_ERROR_MAPS` 新增 `rate-limited` 关键词（上游返回 `temporarily rate-limited upstream`，2026-08-05 实测 `gemma-4-31b-it:free` 命中），原映射表未覆盖导致该类限流未翻译成 429
- 【模型映射】按钮误扩散到所有 target 卡片 —— 改为仅 8081 转发网关专属
- `config_store.load_targets` 丢弃顶层 `anthropicForward` 字段 —— 保留并校验
- 聚合引擎启动预初始化（首请求前 `/api/aggregate/status` 即可用）
- dashboard 卡头状态位显示请求数而非运行状态（8081）—— 固定显示"运行中"
- 模型编辑弹框保存时总开关行（"全部模型"）被误存为模型 —— 前端跳过无子开关行 + 后端 API 防御性过滤
- qclaw handler 客户端不带 system message 时上游 400 —— asyncio 端口补齐 system 消息（与 FastAPI 路径一致）
- config_store.py 合法 handler 校验遗漏 gemini-native
- **codebuddy 非流式请求自动转流式聚合**：上游（copilot.tencent.com）对所有模型拒绝非流式 chat（11101），代理检测后自动 `stream:true` 重试并聚合 SSE 为完整 JSON（含 reasoning_content/tool_calls/usage），非流式客户端也可用
- **trae-work 工具历史协议（seed-code 空响应修复）**：`_openai_to_trae_body` 将 assistant `tool_calls` 文本化拼入 content（`[Tool Call: name]\nArguments: args`）、`role=tool` 消息转 `user` 加前缀（`[Tool Call Result: name]`）——修复 `Doubao-Seed-Code` 对孤立 tool 消息返回 200+空 SSE 流（0 chunks）问题；响应侧兼容 output 新格式（`type:text/content/reasoning`，2026-05）、SSE `error` 事件不再静默（WARNING+文本透传）、过滤 `Building prompt:` 进度提示（逆向 trae-local-api 对照）
- **trae-work 采样参数透传**：`temperature/top_p/presence_penalty/frequency_penalty/stop/seed/n` 尽力透传，`max_tokens` 截断 128000
- **trae-work 排队处理（简化）**：上游 `request_wait_in_queue` 事件 → 直接返回繁忙提示终止（不做降级重发，曾实现分档降级后撤回）
- **trae-work [Tool Call:] 文本格式解析**：seed-code 模型从历史文本化格式学到 `[Tool Call: name]\nArguments: {...}` 纯文本输出（非 DSML/tool_calls 事件）——扩展 `_parse_dsml_tool_calls` 识别该格式（分片半截检测 + arguments 平衡括号提取），避免原始标记透传客户端导致 IDE 不识别；修复 arguments 混入 reasoning 文本 bug
- **trae-work 流式架构重构：正文纯累积 + 流结束统一解析**（`_resolve_trae_text`，替代原"边收边猜是否为标记"的启发式方案）：曾在流式接收阶段实时判断 chunk 是否为工具调用/reasoning 标记的开头/半截（`_is_potential_toolcall_prefix` 等），陆续暴露三种真实抓包变种 bug——`[Tool Call:` 开头 `"["` 独立成 chunk 被误判丢弃、`{"reasoning_content":"..."}` 子串检测导致缓冲区永久判定为"疑似标记"从而无限期挂起（表现为卡顿数秒）、reasoning JSON 被截断永不闭合同样导致无限期缓冲；参考 `trae-local-api` 官方实现架构（流式阶段只做纯文本累积，上游流结束后对完整文本一次性解析），重写为：`response`/`content` 正文流式阶段只累积不转发，`reasoning_content`/原生 `tool_calls`（结构化字段，无歧义）逐 chunk 立即转发，流结束后统一解析清洗（DSML/`[Tool Call:]`/`<tool_call>` XML 三种格式）。非流式路径同步复用同一套解析逻辑
- **DSML 标记解析修复（`_DSML_PAIR_RE`）**：原 `_DSML_FN_RE` 非贪婪匹配 `<｜function｜>(.*?)</｜function｜>` 会在内层 `<｜function name｜>...</｜function｜>` 的闭合处提前终止（两者共用同一闭合标记字符串），导致 `<｜parameter｜>` 从未被捕获到块内——该函数自引入以来从未真正工作过（补充单元测试时发现），现改为一次性配对正则同时捕获 name+parameter
- **trae-work reasoning 多段提取修复**：`_extract_reasoning_text` 曾用 `.search()`/`.sub(count=1)` 只提取/摘除第一段 `{"reasoning_content":"..."}` JSON 字面量，而 seed-code 会把多段思考拆成多个独立 JSON 拼接输出——后续几段原样以裸露 JSON 字面量泄漏到正文；改用 `.finditer()`/`.sub()`（不限 count）处理全部片段
- **trae-work `<tool_call>` XML 参数提取修复 + 同类问题根治**：`_TOOLCALL_XML_JSON_RE` 用正则 `\{[\s\S]*?\}` 非贪婪匹配 `arguments` JSON 对象，遇到嵌套花括号（如 `edit` 工具 oldString/newString 里的 JS 代码 `{{}}`）或转义引号会在第一个 `}` 处提前截断，`json.loads` 校验失败、`tool_calls` 解析为空，导致整段 `<tool_call>...</tool_call>` 原始文本泄漏到正文（表现为 IDE 界面直接显示裸露的工具调用 JSON，未被解析执行）。教训：**任何"提取 JSON 对象子串"场景一律禁止用正则模拟花括号配对**，统一改用平衡括号扫描 `_extract_balanced_json`（原仅用于 `[Tool Call:]` 文本格式，现 DSML/XML 两条路径同步收编）。这是同一类错误连续第三次出现（DSML 配对正则、reasoning 多段提取、这次的 XML JSON 提取），已做代码级审查确认无残留同类正则
- **trae-work 回归测试套件**：`test_trae_protocol.py`（单元 71 项，无网络，覆盖 reasoning 多段提取 + XML 嵌套花括号/转义引号/未闭合三种真实抓包变种）+ `test_trae_work_e2e.py`（端到端 14 用例矩阵，`scripts/test-cases/trae-work/`），改完相关代码必须完整跑一遍

---

## 2026-07-16 — qclaw-local provider 方案落地 + 19000 网关机制完整破解

### 新增

#### `qclaw-local` provider（方案2：寄生代理）

- 新增 `qclaw_inject.js`：通过 Electron inspector 在 QClaw 主进程内注入 HTTP 服务器（19001 端口），使用 QClaw 自带的 axios 实例（含签名拦截器）转发请求到 19000 网关
- `server.py` 新增 `qclaw-local` provider 支持：
  - `QCLAW_LOCAL_BASE_URL = http://127.0.0.1:19001`
  - 透传链路和 LiteLLM provider 链路均支持 qclaw-local
  - 启动诊断自动选择对应 base URL
- 架构：`client → server.py(8083) → 19001(寄生服务器) → 19000(QClaw 网关) → 上游 LLM`
- 测试通过：非流式 ✅、流式（SSE）✅、Anthropic 格式 ✅

#### 19000 网关进程来源检查机制完整破解

- **HMAC-SHA256 签名算法完全破解**（密钥 + payload 格式 + 算法验证通过）
- **确认网关采用 OS 级 PID 反查机制**，通过 koffi FFI 直接调用 IPHLPAPI.DLL 的 `GetExtendedTcpTable`
- **结论：独立签名不可行**，PID 由 Windows 内核管理，用户态无法伪造
- 详见 `QCLAW_19000_GATEWAY_REVERSE.md`（完整逆向调研报告）

### 变更

- `server.py` 中 qclaw-local 的注释更新为"寄生转发服务器"，引用 `qclaw_inject.js`
- `.gitignore` 新增对临时调研脚本（`_*` 前缀）和调研目录的忽略规则

### 清理

- 删除 89 个临时调查脚本（`_*.js`/`_*.py`/`_*.txt`）和 2 个调研目录（`_app_asar_extracted/`、`_asar_regions/`）
- 将方案2 核心脚本从 `_reinject_v3.js` 重命名为 `qclaw_inject.js` 并加生产级注释

### 使用方法

```bash
# 1. QClaw 需以 --inspect=9229 模式启动
# 2. 注入寄生转发服务器
node qclaw_inject.js

# 3. 启动代理（本地调试用 8083 端口避免与 8082 冲突）
$env:PREFERRED_PROVIDER = "qclaw-local"
$env:PORT = "8083"
python server.py
```

---

## 2026-07-16 — QClaw 19000 端口签名机制逆向分析（调查记录）

### 概述

为推进 `qclaw-local` provider（走 19000 端口本地网关）方案，对 QClaw v0.2.33.617 的 19000 端口 HTTP API 签名认证机制进行了系统逆向分析。本次为**调查记录**，尚未产出代码变更；签名算法的具体实现仍待破解（V8 字节码保护）。

### 关键发现

#### 1. 19000 端口端点认证差异

| 端点 | 方法 | 认证 | 返回 |
|------|------|------|------|
| `/proxy/llm/models` | GET | **无需认证** | 200 + 模型列表 |
| `/proxy/llm/chat/completions` | POST | **需要签名头部** | 403 / 9002（缺签名时） |

- `/proxy/llm/models` 即使不带任何认证头部也返回 200，列出所有可用模型（modelroute, pool-hy3-preview, pool-deepseek-v4-pro/flash, pool-glm-5.2/5.1, pool-kimi-k2.7-code-highspeed, pool-kimi-k2.6, pool-minimax-m3/m2.7 等）
- `/proxy/llm/chat/completions` 必须携带完整的签名头部集合，否则网关返回 403 + 错误码 9002

#### 2. 签名算法：HMAC-SHA256（非 Ed25519）

**重要纠正**：早期文档（含 2026-07-09 条目）记载"19000 端口使用 Ed25519 设备签名认证，无法绕过"。本次逆向证实该结论不准确：

- **Ed25519** 仅用于 WebSocket 握手时的设备身份认证（`noble-ed25519` 库），不是 HTTP API 调用的签名机制
- **HTTP API 签名** 使用 **HMAC-SHA256**（在字节码中定位到 `createHmac` 调用，位置 1423170）
- HMAC-SHA256 是对称签名，只要拿到密钥即可伪造合法签名，比 Ed25519 容易突破

#### 3. 签名相关 HTTP 头部

通过字符串扫描 `out/main/index.cjsc` 定位到以下签名头部集合：

| 头部 | 用途推测 |
|------|----------|
| `x-signature` | 主签名值（HMAC 输出） |
| `x-sign-signature` | 备用/二级签名 |
| `x-server-timestamp` | 服务端时间戳（时间同步用） |
| `x-client-timestamp` | 客户端时间戳（防重放） |
| `x-nonce` | 一次性随机数（防重放） |
| `x-qclaw-version` | 客户端版本号 |
| `x-auth-version` | 认证协议版本 |
| `x-token` | 网关 token（gateway.auth.token） |
| `x-conversation-message-id` | 会话消息 ID |
| `x-media-attachment` | 媒体附件标识 |

#### 4. 签名状态机

在字节码中识别出 `llmSignature` 相关的状态字符串：

- `llm_signature_ok` — 签名验证通过
- `llm_signature_time_sync_failed` — 客户端/服务端时间同步失败
- `llm_signature_missing_header` — 缺少必要签名头部
- `llm_signature_inject_failed` — 签名注入失败

#### 5. 关键函数名（位于 `out/main/index.cjsc`）

| 函数名 | 字节码内偏移 | 推测职责 |
|--------|-------------|----------|
| `signRequestBody` | 202554 | 对请求体执行签名 |
| `injectLlmSignature` | 212961 | 将签名头部注入 HTTP 请求 |
| `buildRequestSign` | 208290 | 构造待签名 payload |
| `buildUpstreamHeaders` | 208586 | 构造发往上游的完整头部集合 |

#### 6. 签名代码保护机制

- 签名实现位于 `out/main/index.cjsc`（6,924,424 bytes），是 **V8 字节码文件**（非明文 JS）
- 字符串常量可读（函数名、头部名、状态字符串），但函数体为 V8 bytecode，无法直接还原源码
- asar 包内其他 `.js` 文件均不包含签名逻辑（已全量扫描确认）
- asar 包总大小 143,256,891 bytes，包含 7555 个文件

### 调查方法

1. **asar 解包**：用 Python 手动解析 asar 格式（pickle header + JSON header + content），提取文件清单和偏移
2. **字符串归属定位**：编写 `_find_string_owner.py` 扫描所有 asar 内文件，确认签名字符串仅出现在 `out/main/index.cjsc`
3. **上下文提取**：对每个关键字符串提取前后 1KB 上下文，确认相邻符号关系
4. **端点探测**：用 QClaw 自带的 `node.exe`（v22.22.3）直接请求 19000 端口，验证认证差异
5. **openclaw CLI 探查**：运行 `openclaw doctor --generate-gateway-token` 生成独立 token，但证实该 token 与 QClaw 运行时使用的 token 不同（QClaw 通过 `OPENCLAW_GATEWAY_TOKEN` 环境变量注入运行时 token）

### 待解决问题

1. **HMAC 密钥来源**：HMAC-SHA256 的密钥是固定值、设备派生值、还是从 gateway.auth.token 派生？需逆向 V8 字节码或运行时 hook 才能确认
2. **签名 payload 构造**：待签名字符串的具体拼接格式（哪些头部参与签名、顺序如何、是否包含请求体 hash）
3. **时间同步协议**：`x-server-timestamp` 与 `x-client-timestamp` 的校验逻辑（是否要求服务端先返回时间戳才能签名）
4. **V8 字节码反编译**：需用 `v8-decompiler` 或运行时 `--print-bytecode` 才能还原 `signRequestBody` 等函数的实现

### 替代突破方案（来自 Google 调研）

- **方案 A**：获取 `gateway.auth.token` + Ed25519 签名（适用于 WebSocket 握手，非 HTTP API）
- **方案 B**：MITM 抓包截获 QClaw 客户端发往 19000 的合法请求，复制 `device` 结构体在 5 分钟时间窗内重放
- **方案 C**：修改 `~/.openclaw/openclaw.json` 将 `gateway.auth.mode` 从 `ed25519`/`strict` 降级为 `none`/`token`（需文件写权限且重启网关）
- **方案 D**（本次新增）：直接逆向 HMAC-SHA256 签名算法，在代理端自行构造合法签名头部

### 关键路径

- QClaw 安装目录：`C:\Program Files\QClaw\v0.2.33.617\`
- asar 包：`C:\Program Files\QClaw\v0.2.33.617\resources\app.asar`
- 签名代码：`out/main/index.cjsc`（asar 内偏移 51,313,148，绝对偏移 53,091,540）
- 配置存储：`%APPDATA%\QClaw\app-store.json`（含加密的 `authGateway.providers.qclaw.apiKey`）
- openclaw 配置：`~/.openclaw/openclaw.json`（独立实例，与 QClaw 运行时 token 不同）
- QClaw 自带 Node：`C:\Program Files\QClaw\v0.2.33.617\resources\node\node.exe`（v22.22.3）
- QClaw 自带 openclaw：`C:\Program Files\QClaw\v0.2.33.617\resources\openclaw\node_modules\openclaw\openclaw.mjs`

### 文件变更

本次为纯调查记录，**无代码变更**。以下为调查过程中产生的临时分析脚本（待清理）：

| 文件 | 用途 |
|------|------|
| `_parse_asar.py` | 解析 asar 格式 |
| `_find_auth_gateway_v2.py` | 搜索 auth gateway 模式 |
| `_find_string_owner.py` | 定位签名字符串归属文件 |
| `_extract_cjsc.py` | 提取并分析 index.cjsc |
| `_extract_key_regions.py` | 提取关键区域到独立文件 |
| `_find_llm_signature.py` | 搜索 llmSignature 函数 |
| `_find_sign_impl.py` | 搜索签名实现 |
| `_extract_sign_impl.py` | 提取 signRequestBody / injectLlmSignature |
| `_search_signing_all.py` | 全 asar 搜索签名模式 |
| `_test_19000_node.js` | 用 node.exe 测试 19000 端口 |
| `_test_sign_with_qclaw.js` | 尝试加载 QClaw 内部模块 |
| `_app_asar_extracted/` | 提取的 cjsc 和上下文文件 |

---

## 2026-07-15 — 本地 token 估算（tiktoken）

### 概述

QClaw 上游网关不返回 `usage` 字段，导致 Claude Code 等客户端无法显示用量。引入 `tiktoken` 在代理端本地估算 input/output tokens，替换原本硬编码的 0 值。

### 变更

- **新增依赖** `tiktoken>=0.7.0`（[pyproject.toml](file:///c:/Users/Administrator/claude-code-proxy-main/pyproject.toml)）
- **新增工具函数**（[server.py L44-L155](file:///c:/Users/Administrator/claude-code-proxy-main/server.py#L44-L155)）：
  - `_get_tokenizer(model)` — 缓存 cl100k_base tokenizer 实例
  - `_extract_text_from_content(content)` — 从 str / list[dict] 抽取纯文本（兼容 Anthropic content blocks 与 OpenAI message content）
  - `_estimate_messages_tokens(messages, model, system, tools)` — 估算输入 tokens（参考 OpenAI 公式：每条 msg 4 + role + text，加 3 priming）
  - `_estimate_text_tokens(text, model)` — 估算输出 tokens
- **替换硬编码 0 值**（5 个位置）：
  - `_convert_oai_to_anthropic` — fallback 路径的 `Usage(input_tokens=0, output_tokens=0)`
  - `convert_litellm_to_anthropic` — QClaw 网关不返回 usage 时估算 prompt/completion tokens
  - `handle_streaming` — `message_start.input_tokens` 用请求 messages 估算；`message_delta.output_tokens` 在 early-exit 和 final 两个路径都用累积响应文本估算
  - `/v1/chat/completions` 透传响应 — 缺失 usage 时注入估算的 `prompt_tokens` / `completion_tokens` / `total_tokens`
  - `/v1/messages/count_tokens` — ImportError fallback 从硬编码 1000 改为 tiktoken 估算
- **`handle_qclaw_streaming`** — output_tokens 估算从 `len(accumulated.split())`（按空格分词）改为 tiktoken

### 估算精度

- DeepSeek/GLM/Kimi/MiniMax/Claude 均使用 `cl100k_base`（经验上误差 ±10%）
- 仅做客户端展示用途，不影响上游计费

### 文件变更

| 文件 | 变更 |
|------|------|
| `pyproject.toml` | 新增 tiktoken 依赖 |
| `server.py` | 新增估算工具函数 + 替换 5 处硬编码 0 值 |

---

## 2026-07-09 (v2) — 测试套件整合

### 概述

将根目录三个历史测试文件整合为一个统一测试套件 `test_suite.py`，消除功能重叠，统一 QClaw 模型名适配。

### 变更

- **新增 `test_suite.py`**（869 行）：整合 `test_claude_api.py` / `test_messages_endpoint.py` / `tests.py`
  - 15 大类测试场景，38 个测试点
  - 合并 `test_messages_endpoint.py` 的 thinking 场景（adaptive/enabled/budget/历史 422 bug/工具组合）和 SSE 事件序列验证
  - 合并 `tests.py` 的 argparse 支持（`--simple` / `--tools` / `--oai` / `--no-streaming`）
  - 统一根据 `PREFERRED_PROVIDER` 动态选择模型名
- **删除 `test_claude_api.py`**（528 行）
- **删除 `test_messages_endpoint.py`**（262 行）
- **删除 `tests.py`**（691 行）
- **`README-zh.md`** 更新测试章节，反映整合后的使用方式

### 文件变更

| 文件 | 变更 |
|------|------|
| `test_suite.py` | 新增，整合三个历史测试文件 |
| `test_claude_api.py` | 删除 |
| `test_messages_endpoint.py` | 删除 |
| `tests.py` | 删除 |
| `README-zh.md` | 更新测试章节 |

---

## 2026-07-09 — QClaw 上游直连 + API Key 自动解密

### 概述

彻底放弃 19000 本地网关方案（Ed25519 设备签名认证无法绕过），改用 GetQClawAPIKey 方法：从 QClaw 客户端本地存储解密 API Key，直连上游 `mmgrcalltoken.3g.qq.com` OpenAI 兼容接口。支持指定具体模型（如 `pool-deepseek-v4-flash`），流式和非流式均正常。

### 修复

- **`QCLAW_BASE_URL`** 默认值从 `http://127.0.0.1:19000/proxy/llm` 改为 `https://mmgrcalltoken.3g.qq.com/aizone/v1`
- **新增 `_decrypt_qclaw_api_key()`**：从 `%APPDATA%\QClaw\Local State` 读取 DPAPI 保护的 AES-256 密钥，用 AES-256-GCM 解密 `app-store.json` 中的 v10 密文，得到 `sk-...` API Key
- **新增 `_dpapi_unprotect()`**：Windows DPAPI 解密（ctypes 调用 CryptUnprotectData）
- **移除所有 `__QCLAW_AUTH_GATEWAY_MANAGED__`** 引用，改用解密的真实 API Key
- **移除 `x-agent-id` 请求头**（上游不需要）
- **移除 `Connection: close`**（不再需要避免网关缓存，恢复 keepalive）
- **`max_keepalive_connections`** 从 0 改为 10（恢复连接复用）
- **所有 httpx 客户端添加 `trust_env=False`**（绕过系统代理，解决 Python urllib/httpx 因系统代理导致请求失败的问题）
- **移除 403/9002 专属重试逻辑**（直连上游不会有 9002）
- **保留 `User-Agent: OpenAI/JS 6.39.1`**（上游拒绝 python-httpx 默认 UA，返回 400 "invalid request"）
- **`.env`** 移除旧 19000 URL，新增可选 `QCLAW_API_KEY` 环境变量覆盖
- **`test_claude_api.py`** 适配 QClaw 模式：透传链路用 pool-* 模型名，根据 PREFERRED_PROVIDER 动态选择

### 文件变更

| 文件 | 变更 |
|------|------|
| `server.py` | API Key 解密 + 上游直连 + trust_env + User-Agent + 清理 9002 逻辑 |
| `.env` | 移除旧网关 URL，新增可选 API Key |
| `test_claude_api.py` | 适配 QClaw 模式，动态模型名 |
| `README-zh.md` | 新增 QClaw 上游直连设计文档 + 解密链路 + 排查指南 |

### 背景

- QClaw 19000 端口使用 Ed25519 设备签名认证，客户端外无法模拟
- 60227 等动态端口是 agent 级别会话接口，非 LLM 级别
- 上游 `mmgrcalltoken.3g.qq.com` 是标准 OpenAI 兼容接口，用解密的 API Key 即可调用
- Python urllib 受系统代理影响返回 400，Node.js fetch 和 httpx(trust_env=False) 正常

---

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
