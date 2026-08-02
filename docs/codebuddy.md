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

## 5. 错误排查

| 现象 | 原因 | 处理 |
|------|------|------|
| HTTP 400 `"code":11101` | 上游拒绝非流式 chat | 代理已自动聚合；若仍透传 400 = 聚合失败，检查上游连通 |
| 401/403 | token 过期 / 该 base 不认此 token（如 `www.codebuddy.ai` 对 IOA token 直接 401） | 重新提取 token；`_call` 会自动换 base |
| 额度显示 error | token 无权限 / 接口异常 | 检查 `codebuddy_token` 有效性（`GET /v2/accounts` 可验证） |
| 登录态丢失 | 刷新后未持久化轮换的 refreshToken | 回写新 refreshToken 到 secrets.json（陷阱 #11） |

---

## 6. 已知陷阱

1. **上游只支持流式**：codebuddy 上游对非流式请求报 11101，代理已自动转流式聚合；新增错误特征判断时勿绕过该检测（AGENTS.md 陷阱 #10）。
2. **refreshToken 轮换必须立即持久化**：刷新后旧 refreshToken 立即失效，未回写新值会导致登录态丢失（AGENTS.md 陷阱 #11）。
3. **仅 Windows 破解**：`crack_codebuddy.py` 仅 Windows 支持；其他 OS 需手动在 dashboard 填写 `codebuddy_token`。

---

## 附：与本文档相关的代码位置

| 文件 | 说明 |
|------|------|
| `crack_codebuddy.py` | Windows 客户端目录探测提取 token（当前骨架实现） |
| `crack_codebuddy_q.py` | 额度 / 成长任务 / 账号 / 用量通知查询（`codebuddy_status`） |
| `crack_common.py` | `CREDENTIAL_SCHEMAS["codebuddy"]`（凭据 schema）+ 注册表 |
| `crack_daily.py` | `daily_codebuddy`：成长任务领取 + token 剩 <30 天刷新（refreshToken 轮换回写） |
| `server.py` | 11101 检测 + `_aggregate_codebuddy_stream` 非流式聚合 |
| `config_store.py` | `VALID_HANDLERS` 含 `passthrough` |
| `targets.json` | codebuddy target 定义（8084、`copilot.tencent.com`、`routePrefix=/v2`、secretRef） |
