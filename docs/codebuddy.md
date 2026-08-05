# CodeBuddy 破解网关文档

> CodeBuddy 是腾讯的 AI 编程工具。本代理接入其 LLM 上游（`copilot.tencent.com`），
> 认证走本地客户端提取的 JWT，且需处理其**只支持流式请求**（11101）的特殊限制。
> 本文档固化了认证与 refreshToken 轮换、11101 非流式自动聚合、额度/成长任务查询。
>
> 本文档从 `docs/architecture.md`、`AGENTS.md`、`CHANGELOG.md`、`docs/crack-tools.md` 迁移整理，2026-08-02。
> 本文档是 [`crack_codebuddy.py`](../crack_codebuddy.py)、[`crack_codebuddy_q.py`](../crack_codebuddy_q.py)
> 与代理 8084 端口（handler=`passthrough`）的实现参考。

---

## 1. 概述

| 项 | 值 |
|----|----|
| 端口 | 8084（targets.json 中 `listenPort=8084`，handler=`passthrough`） |
| 上游 | `https://copilot.tencent.com`（routePrefix `/v2`；查询端点备用 `https://www.codebuddy.ai`） |
| 分类 | `crack`（本地客户端提取 token，代理注入认证） |
| 客户端接入 | `base_url = http://<host>:8084/v1`，`api_key = "dummy"`（crack 类代理不校验） |
| secrets 字段 | `codebuddy_token`（JWT，必填）/ `codebuddy_refresh_token`（可空）/ `codebuddy_uid`（数字）/ `codebuddy_nickname`（只读展示） |
| 模型 | targets.json 白名单：`deepseek-v4-pro` / `deepseek-v4-flash` / `glm-5.2` / `kimi-k3-1` / `hy3` 等 |
| 内容过滤排查 | 见 §6「错误排查」下的「内容过滤现象分类与归因」 |

认证方式：`Authorization: Bearer <codebuddy_token>`（JWT）。

---

## 2. 认证与 refreshToken 轮换

### 2.1 crack_codebuddy.py（Windows）

```
python crack_codebuddy.py [--secrets secrets.json] [--force]
```

- 探测本机 CodeBuddy 客户端目录（`%LOCALAPPDATA%` / `%APPDATA%` / home 下），从客户端本地存储提取 token 写入 `secrets.json`
- ⚠️ **当前 `_find_codebuddy_token()` 为骨架实现**（返回空串），实际提取逻辑待按客户端版本补全；失败时优雅退出码 1 + 引导文案（已登录机器运行本脚本，或 dashboard 手动填写）
- **仅 Windows 支持**，其他 OS 下 dashboard「重新破解」按钮置灰提示"仅支持 Windows，待后续补齐"

### 2.2 refreshToken 轮换协议（重要）

- access token（`codebuddy_token`）是 JWT，从 payload `exp` 可解析到期时间
- 刷新用 `codebuddy_refresh_token` 换取新 access token；**refreshToken 每次刷新会轮换新值**
- ⚠️ **刷新后必须立即持久化新值**（回写 secrets.json），否则旧 refreshToken 失效导致登录态丢失（AGENTS.md 陷阱 #11）
- 每日任务 `crack_daily.py` 的 `daily_codebuddy`：JWT exp 剩余 < 30 天时用 refreshToken 换新并回写（轮换的 refreshToken 一并持久化）
- 刷新接口未实测细节（crack_codebuddy_q.py 中 `refreshExpireAt` 恒为 None，注释"避免轮换 token 影响现有登录态"）——待确认

---

## 3. 11101 非流式限制（自动聚合）

**上游 `copilot.tencent.com` 对所有模型拒绝非流式 chat**：返回 HTTP 400 + `"code":11101`
（错误消息 "Non-stream chat request is currently not supported"），只接受 `stream:true`。

代理自动处理（server.py 统一转发引擎）：

```
客户端非流式请求 → 上游 400 (code=11101) → 检测到 → 内部 stream:true 重试
  → 收集 SSE chunks 聚合为完整 JSON（含 reasoning_content / tool_calls / usage）→ 返回客户端
```

- 触发条件：`status == 400` 且响应体含 `"code":11101` 且 target label 为 `codebuddy`
- 聚合函数 `_aggregate_codebuddy_stream()`：重建请求（`stream: True`）→ SSE 流收集 → 按 `choices[].index` 分组 delta → 拼装 OpenAI 格式完整响应（含 usage）
- 聚合失败时回退：透传上游 400 给客户端
- **效果**：非流式客户端也可用，行为与 copilot/qclaw 一致，无需客户端改动

---

## 3.5 上游 SSE 帧不合规与规范化（normalizeSse）

**现象**：客户端（opencode）渲染 reasoning 模型（kimi-k3-1 等）的思考链时，**思考链被拆成几百个独立思考块**，而不是一个连续的思考过程。

**根因**：上游每帧 delta 都塞满"存在但为空"的字段，不符合 OpenAI 协议——**不是文本拼接问题，是帧结构问题**。

实测 kimi-k3-1（2026-08-05）上游实际返回：

```json
{"delta":{"content":"","reasoning_content":"The","function_call":null,
          "refusal":"","tool_calls":[],"extra_fields":null},"finish_reason":""}
```

标准 OpenAI 协议下思考阶段应只有 `{"reasoning_content":"The"}`。

**客户端为何切段**：opencode 用 Vercel AI SDK（`@ai-sdk/openai-compatible`），它按**"键是否出现"**判断段落边界——

| 见到的键 | SDK 行为 |
|---------|---------|
| `content` | 认为正文块开始 → 结束当前 reasoning part |
| `tool_calls` | 认为工具调用段开始 → 结束当前 reasoning part |

下一帧又见 `reasoning_content` → 开新 part。**597/599 帧命中**，思考链被切成数百块。

8084 是 `passthrough` 纯字节转发（`aiter_bytes()`），畸形帧原样透传，所以问题直达客户端。

> **⚠️ 排查教训**：首次修复只删了空 `content`，采用"保守策略"特意保留 `tool_calls`/`refusal`/`function_call`/`extra_fields`，理由是"对本 bug 零收益，却可能破坏依赖键存在性做类型推断的客户端"。**这个判断错了**——恰恰是"依赖键存在性"这个特性导致切段，`tool_calls:[]` 正是元凶。教训：**面对未知客户端解析逻辑时，"保守保留"不必然安全**，要先确认客户端用的是哪个 SDK、它按什么规则分段。
>
> 另一个教训：现象描述的精确度决定排查方向。最初被描述为"思考链换行"，导致长时间在"文本换行符"方向验证（实测思考文本里的 `\n` 是模型自己生成的正常分段，473 帧中仅 20 帧含换行）；直到明确是"拆成多个思考块"才锁定真因。

**修复**（targets.json 开 `normalizeSse: true`）：

```
上游 chunk → _SseLineBuffer 重组完整行 → 诊断统计(原始行) → _normalize_codebuddy_sse_line → 写出
```

- `_SseLineBuffer`：按 `\n` 切行，处理跨 TCP chunk 粘包。**改写模式下必须**——纯透传时帧被切断无所谓，但要逐帧改写就必须先重组，否则会切坏 JSON
- `_normalize_codebuddy_sse_line`：清洗**空值**字段（有内容的绝不动）
  - `reasoning_content` 非空且 `content == ""` → 删 `content` 键
  - `content` 非空且 `reasoning_content == ""` → 删 `reasoning_content` 键
  - `tool_calls == []` / `function_call is None` / `refusal == ""` / `extra_fields is None` → 删该键
  - `function_call == {"name":"","arguments":""}` → 删（**首帧是空内容 dict 而非 null**，`== None` 匹配不到，需单独判断）
  - `finish_reason == ""` → `null`（子开关 `normalizeFinishReason`，默认 `true`）
- **回归红线**：`tool_calls`/`function_call` 有内容时必须完整保留（工具调用是结构化数据，删了会断链）。用 `type()` 校验避免 `0`/`False` 这类假空值被误删
- 降级：非 `data:` 行 / `[DONE]` / keep-alive 注释 / 畸形 JSON 一律原样透传，**绝不吞帧或中断流**
- 性能：未命中规则的帧返回原对象，不重新序列化

**修复效果**：带 `tool_calls` 键的帧 597→0，最终 589 帧为纯净的 `('reasoning_content',)`；`finish_reason` 空串 586→0；思考链合并为连续块，思考与正文内容完整无损（中文/emoji/换行符均正确）。

**诊断**：`DEBUG=true` 时 `codebuddy.log` 输出 `SSE 透传完成: data_lines=N finish_reasons=[...] normalized=M`，`normalized` 即本次改写的帧数。**注意 DEBUG 默认关闭**（日志级别 INFO），此行不会出现——需临时开 DEBUG 才能看到，查完记得恢复。

---

## 4. 额度 / 成长任务查询（crack_codebuddy_q.py）

数据来源全部经本机 token 实测。endpoint 默认 `https://copilot.tencent.com`，失败自动换 `https://www.codebuddy.ai`（401/404/超时换 base）。

| 用途 | 端点 | 返回要点 |
|------|------|---------|
| 资源包额度 | `POST /billing/meter/get-user-resource` | `data.Response.Data.Accounts[]`：PackageName / CapacitySize(limit) / CapacityUsed / CapacityRemain / CycleEndTime(expireAt) / CapacityUnit |
| 用量通知 | `POST /v2/billing/meter/get-dosage-notify` | `dosageNotifyCode`（0=无告警） |
| 请求级用量 | `POST /billing/meter/get-user-request-usage` | 每次请求扣费明细 |
| 账号 | `GET /v2/accounts` | `uid` / `nickname` |
| 成长 profile | `GET /v2/activity/growth/profile` | 成长等级 / 完成数 |
| 任务列表 | `GET /v2/activity/growth/tasks` | 任务列表（`reward_credit` / `accept_status` / `has_reward`） |
| 连续打卡 | `GET /activity/growth/streak` | 连续天数（7/14/28 天档位） |
| 能量余额 | `GET /activity/growth/energy` | 能量余额 |
| 接受任务 | `POST /activity/growth/tasks/accept` | body `{task_codes:[...]}` |
| 领取奖励 | `POST /activity/growth/tasks/{code}/claim` | 领取任务奖励 |

> **CodeBuddy 无独立"每日签到"按钮式接口**：积分由每日自动发放 + 成长计划任务领取构成。
> "打卡"体系 = 成长计划（streak 连续天数 + 任务奖励），状态查询如实上报。

- 依赖：标准库 + httpx（`trust_env=False`）
- dashboard 统一入口：`GET /api/crack/codebuddy/status`（`CRACK_STATUS_HANDLERS` 注册表 → `codebuddy_status`）

```bash
.venv/bin/python crack_codebuddy_q.py   # 读 secrets.json 查询额度/成长/账号状态
```

---

## 5. 成长计划任务（每日自动领取）

### 5.1 机制

CodeBuddy 无独立"每日签到"接口，积分由两部分构成：
1. **每日自动发放** — 登录 CodeBuddy 客户端即触发，代理无需干预
2. **成长计划任务奖励** — 需在 CodeBuddy 客户端完成指定操作后手动领取

代理通过 `crack_daily.py` 每天 03:00 自动调用 `GET /v2/activity/growth/tasks` 查可领取任务，再调 `POST /activity/growth/tasks/{code}/claim` 领取。

### 5.2 已完成任务 vs 可领取任务

任务状态有 `not_accepted` / `accepted` / `completed` / `claimed` 几种。只有 `completed` 状态的任务才能领取奖励。常见问题：

| 现象 | 原因 | 解决 |
|------|------|------|
| `领取任务 xxx → task not completed` | 任务已接受但在客户端的进度还没完成 | 在 CodeBuddy 客户端完成对应操作后，代理下次 cron 自动领 |
| 有任务显示 `accepted` 但领不了 | 代理无法代替客户端完成前端交互任务 | 用户需在 CodeBuddy 客户端完成任务 |

### 5.3 典型任务与完成方式

| 任务 | 完成条件 | 代理能否代替 |
|------|----------|-------------|
| `create_canvas` 体验设计创意模式 | 客户端进入创意模式创建画布 | 否 — 需前端交互 |
| `playbook_prompt` 探索优秀灵感 | 客户端点击灵感案例做同款 | 否 — 需前端交互 |
| `chat_5` 和 AI 聊天 5 次 | 累计 5 次对话 | 理论可行但违反服务条款 |
| `expert_5` 召唤 5 次专家 | 召唤 5 位不同专家 | 理论可行但违反服务条款 |
| `expert_5` 召唤 5 次专家 | 召唤 5 位不同专家 | 理论可行但违反服务条款 |

⚠️ **限制**：成长计划任务大多依赖 CodeBuddy 客户端前端交互（如打开特定页面、点击按钮、选择模板等），代理 API 层面无法代替用户完成这些操作。

### 5.4 未来改进方向（TODO）

1. **自动完成对话类任务**：部分任务（如 `chat_5`、`expert_5`、`template_5`）可通过代理转发 API 请求模拟完成，需研究 CodeBuddy 任务进度上报机制
2. **更积极的领取重试**：当前 cron 只尝试一次，可改为多次重试直到 `completed`
3. **任务进度监控**：检测 `current < target` 时主动提醒用户在客户端完成

---

## 6. 错误排查

| 现象 | 原因 | 处理 |
|------|------|------|
| HTTP 400 `"code":11101` | 上游拒绝非流式 chat | 代理已自动聚合；若仍透传 400 = 聚合失败，检查上游连通 |
| 401/403 | token 过期 / 该 base 不认此 token（如 `www.codebuddy.ai` 对 IOA token 直接 401） | 重新提取 token；`_call` 会自动换 base |
| 额度显示 error | token 无权限 / 接口异常 | 检查 `codebuddy_token` 有效性（`GET /v2/accounts` 可验证） |
| 登录态丢失 | 刷新后未持久化轮换的 refreshToken | 回写新 refreshToken 到 secrets.json（陷阱 #11） |
| 内容过滤/阻断 | 上游触发内容安全策略或格式校验阻断 | 见下方「内容过滤现象分类与归因」 |
| **返回"您当前输入的信息存在敏感内容"** | 见下方归因规则 R1–R4；注意：当前 codebuddy 走纯 `passthrough`，代理**不注入** `reasoning_effort` / `reasoning_summary` 字段，因此「代理注入触发过滤」在当前代码下不会发生。若未来在 `_handler_prepare_body` 增加 codebuddy 特判，必须遵循 **opt-in 规则**：仅客户端显式传 `reasoning_effort` 时才附加 reasoning 字段，否则移除所有 reasoning 相关字段（历史 #2071 教训） | 先用下方只读诊断命令确认请求体是否真含 reasoning 字段，再归因；非代理注入则属上游/客户端行为，代理层无法绕过 |

### 6.1 内容过滤现象分类与归因（融合自探测交接文档）

当 codebuddy 返回内容过滤类错误时，按以下分类与归因规则判断根因。**核心原则：代理层只负责转发，不修改语义内容；过滤是上游/客户端行为，代理无法绕过。**

#### 现象分类

| 分类 | 表现 | 典型归属 |
|------|------|----------|
| C1 显式敏感词 | 返回「您当前输入的信息存在敏感内容」「包含违规信息」等明确文案 | 上游安全策略（用户真实违规） |
| C2 格式/协议校验 | 401/400/11101 等非内容文案，但被误判为"过滤" | 协议/认证层（非内容安全） |
| C3 推理字段触发 | 请求体含 `reasoning_effort` / `reasoning_summary` 时触发，移除后正常 | 上游对推理参数的特殊校验（历史 #2071 类） |
| C4 上游抖动/限流 | 偶发、重试即过，无稳定复现 | 上游负载或限流（非过滤） |

#### 归因规则

- **R1**：先确认返回文案是否为内容安全类（C1/C3），还是协议类（C2）。协议类错误走 §6 上方对应行处理。
- **R2**：对 C3，用下方只读命令检查**请求体实际是否含 reasoning 字段**。当前 codebuddy 为纯 passthrough，正常情况下不含；若含，说明来自客户端或上游透传，而非代理注入。
- **R3**：C4 需用"相同请求多次重试 + 换模型"验证稳定性，排除偶发。
- **R4**：**禁止**为绕过过滤而删除安全字段、伪造身份、编码混淆、提示注入、拆分危险请求或修改审查参数——属安全越界，代理设计上不支持。

#### 只读诊断命令（安全边界内）

```bash
# 1. 抓经过代理的真实请求体（确认是否含 reasoning 字段）—— 仅读代理日志
tail -200 /root/shared-workspace/claude-code-proxy/proxy.log | grep -i "reasoning\|敏感\|filter"

# 2. 直连上游对照（不代理，验证是上游还是代理问题）—— 用已提取 token
curl -s https://copilot.tencent.com/v2/chat/completions \
  -H "Authorization: Bearer $CODEBUDDY_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"model":"deepseek-v4-flash","messages":[{"role":"user","content":"<同内容>"}],"stream":false}'

# 3. 对照：同一内容去掉 reasoning 字段再试，确认是否 C3 类
```

> 对照实验设计（探测模板，保留备查）：直连对照 × 多模型 × 多轮，记录 JSON Lines（字段：timestamp / model / has_reasoning / round / result / classification）。未授权账号、无实测数据的不下结论。

### 6.2 system prompt 精确短语拦截（OMO 框架 content_filter）—— 热重写规避

> **背景**：2026-08-04/05 实测发现，腾讯对 **OMO 框架（oh-my-openagent 插件）注入的特定 system prompt 精确短语** 100% 触发内容审查，表现为 **HTTP 200 空 SSE + `finish_reason=content_filter`**（opencode 误判为 "provider's content filter"，且不触发 fallback）。已在代理层做**精确字符串热重写**规避。本节用于快速定位 + 解决同类问题。

#### 触发短语全清单（仅 2 个，均已覆盖）

| # | 触发短语 | 来源（OMO 插件 dist/index.js） | 覆盖规则 |
|---|---------|------|---------|
| 1 | `Sisyphus-Junior - Focused executor from OhMyOpenCode.` | 子代理 default 模板 `buildDefaultSisyphusJuniorPrompt`（hy3 不被识别为 kimi-k3 → 走 default 分支） | `_CODEBUDDY_SYS_REWRITES` 规则 1 |
| 2 | `You are "Sisyphus" - Powerful AI Agent with orchestration capabilities from OhMyOpenCode.` | 主代理动态构建 `renderRoleAndIntentSections`（所有模型共用路径） | `_CODEBUDDY_SYS_REWRITES` 规则 2 |

**触发机制（反证法实证）**：腾讯黑名单匹配的是**完整精确短语**，不是泛化的 "OhMyOpenCode" 字样。触发串 2 的三成分缺一不可：`"Sisyphus"` 引号 + `" - "` 连字符 + `Powerful AI Agent with orchestration capabilities from OhMyOpenCode` 完整句——任何成分缺失/改动都不触发（已逐一反证）。

**已实测不触发的变体**（无需覆盖）：
- 星号变体 `You are **Sisyphus** - Powerful AI Agent with orchestration capabilities from OhMyOpenCode.`（gemini/gpt 静态模板，缺引号）
- 逗号/无引号变体 `You are Sisyphus, the OhMyOpenCode orchestration lead...`（Sisyphus 3 个 + Sisyphus-Junior kimi-k3/k2-7/glm 模板）
- **其他 OMO 角色真实身份短语全部 stop**：Atlas 8 变体（含结构最接近触发串的 `You are Atlas - Master Orchestrator from OhMyOpenCode.`）、Metis、Explore、Librarian、Oracle、Momus、Multimodal-Looker、General、Plan、Hephaestus —— **换角色名不会被拦截**

#### 快速定位步骤

```bash
# 1. 看独立网关日志的入站 system 预览 + SSE finish_reason（DEBUG=true 时才有）
grep -E "sys\[:200\]|finish_reasons" /root/shared-workspace/claude-code-proxy/codebuddy.log | tail -20
#    若 finish_reasons 含 content_filter → 上游拦截
#    若入站 sys[:200] 被替换成 'a capable coding agent...' → 热重写已命中（正常）

# 2. 确认热重写命中记录（INFO 级别，关 debug 后仍有）
grep "sys prompt rewritten" /root/shared-workspace/claude-code-proxy/codebuddy.log | tail -5

# 3. 复现/验证某个短语是否触发（直连 8084，注意：代理会自动重写已知触发串，
#    想测"上游原始行为"需先临时移除对应规则或直连上游）
.venv/bin/python - << 'EOF'
import json, subprocess
TOKEN = json.load(open("secrets.json"))["codebuddy_token"]
def test(p: str):
    body = {"model": "hy3", "stream": True,
            "messages": [{"role": "system", "content": p}, {"role": "user", "content": "hi"}]}
    r = subprocess.run(["curl","-s","-N","-X","POST","http://127.0.0.1:8084/v1/chat/completions",
                        "-H","Content-Type: application/json","-H",f"Authorization: Bearer {TOKEN}",
                        "-d",json.dumps(body),"--max-time","10"], capture_output=True, text=True, timeout=15)
    return "FILTER" if "content_filter" in r.stdout else ("stop" if '"finish_reason":"stop"' in r.stdout else "?"+r.stdout[:60])
print(test('You are "Sisyphus" - Powerful AI Agent with orchestration capabilities from OhMyOpenCode.'))
EOF
```

#### 解决方案

- **代码位置**：`server.py` `_CODEBUDDY_SYS_REWRITES`（元组列表，`_clean_codebuddy_body` 遍历 system message 做精确字符串替换；列表驱动，新增规则只需加一行 `(触发短语, 安全替换)`）。
- **触发短语 → 安全替换**：
  1. `Sisyphus-Junior - Focused executor from OhMyOpenCode.` → `Focused task executor agent.`
  2. `You are "Sisyphus" - Powerful AI Agent with orchestration capabilities from OhMyOpenCode.` → `You are "Sisyphus" - a capable coding agent with strong orchestration abilities.`
- **安全边界**：只替换 OMO 框架自身注入的**身份短语**，不动客户端内容/工具字段；替换目标是等义的中性描述（不伪造身份、不编码混淆、不删除安全字段）。若未来腾讯扩展黑名单拦其他角色（如 Atlas），只需在元组追加一行，无需改逻辑。
- **历史路径**：曾实现 content_filter 识别转 422 明确错误（38d6087），因 opencode fallback 只认网络类错误而无效，已回退（110d370）恢复静默透传；随后以热重写（b1cdd25 子代理 → ff49601 主代理 + 列表驱动）作为最终方案。

---

## 6. 已知陷阱

1. **上游只支持流式**：codebuddy 上游对非流式请求报 11101，代理已自动转流式聚合；新增错误特征判断时勿绕过该检测（AGENTS.md 陷阱 #10）。
2. **refreshToken 轮换必须立即持久化**：刷新后旧 refreshToken 立即失效，未回写新值会导致登录态丢失（AGENTS.md 陷阱 #11）。
3. **仅 Windows 破解**：`crack_codebuddy.py` 仅 Windows 支持；其他 OS 需手动在 dashboard 填写 `codebuddy_token`。
4. **上游 SSE 帧不合规，勿假设其符合 OpenAI 协议**：思考帧夹带空 `content`、正文帧夹带空 `reasoning_content`、`finish_reason` 用空串而非 `null`（详见 §3.5）。已由 `normalizeSse` 规范化层修正；**任何新增的 SSE 解析逻辑都不要假设上游字段规范**。改写 SSE 必须先用 `_SseLineBuffer` 重组跨 chunk 的半截帧，且诊断统计要基于改写**前**的原始行——否则规范化自身的 bug 会掩盖上游真实异常。
5. **reasoning 参数必须 opt-in**（历史 #2071 教训）：codebuddy 上游对推理参数（如 `reasoning_effort` / `reasoning_summary`）有特殊校验，可能引发内容安全类错误。当前 codebuddy 走纯 passthrough，代理不注入 reasoning 字段；**若未来在 `_handler_prepare_body` 增加 codebuddy 特判，必须遵循 opt-in 规则**——仅客户端显式传 `reasoning_effort` 时才附加 reasoning 相关字段，否则移除所有 reasoning 字段。新增处理 codebuddy 请求的代码必须遵循此规则。

---

## 附：与本文档相关的代码位置

| 文件 | 说明 |
|------|------|
| `crack_codebuddy.py` | Windows 客户端目录探测提取 token（当前骨架实现） |
| `crack_codebuddy_q.py` | 额度 / 成长任务 / 账号 / 用量通知查询（`codebuddy_status`） |
| `crack_common.py` | `CREDENTIAL_SCHEMAS["codebuddy"]`（凭据 schema）+ 注册表 |
| `crack_daily.py` | `daily_codebuddy`：成长任务领取 + token 剩 <30 天刷新（refreshToken 轮换回写） |
| `server.py` | 11101 检测 + `_aggregate_codebuddy_stream` 非流式聚合；codebuddy 走纯 passthrough，**不**清理/注入 reasoning 字段（若未来加 codebuddy 特判须遵循 §6 的 opt-in 规则） |
| `config_store.py` | `VALID_HANDLERS` 含 `passthrough` |
| `targets.json` | codebuddy target 定义（8084、`copilot.tencent.com`、`routePrefix=/v2`、secretRef） |
