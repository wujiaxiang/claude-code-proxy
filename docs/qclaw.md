# QClaw 破解网关文档

> QClaw 是腾讯的一款 AI 工作台产品。本代理接入 QClaw 的 LLM 上游（`mmgrcalltoken.3g.qq.com`），
> 通过从 QClaw 客户端本地存储解密 API Key 实现"登录过一次即可自动代理"。
> 本文档固化了 API Key 解密链路、三条铁律、直连 / qclaw-local 两种模式、积分额度查询与排查指南。
>
> 本文档从 `README-zh.md` §"QClaw 上游直连方案"迁移并去重补缺，2026-08-02。
> 19000 网关的完整逆向调研见 [QCLAW_19000_GATEWAY_REVERSE.md](../QCLAW_19000_GATEWAY_REVERSE.md)。
> 本文档是 [`crack_qclaw.py`](../crack_qclaw.py)、[`crack_qclaw_q.py`](../crack_qclaw_q.py)
> 与代理 8085 端口（handler=`qclaw`）的实现参考。

---

## 1. 概述

| 项 | 值 |
|----|----|
| 端口 | 8085（targets.json 中 `listenPort=8085`，handler=`qclaw`） |
| 上游 | `https://mmgrcalltoken.3g.qq.com`（OpenAI 兼容接口，routePrefix `/aizone/v1`，解析为 `60.29.254.103` 腾讯 IP） |
| 分类 | `crack`（本地客户端提取 API Key，代理注入认证） |
| 客户端接入 | `base_url = http://<host>:8085/v1`，`api_key = "dummy"`（crack 类代理不校验） |
| secrets 字段 | `qclaw_api_key`（必填，`sk-` 前缀）+ 可选业务字段（见 §5） |
| 模型 | `pool-*` 系列（`pool-deepseek-v4-pro` / `pool-deepseek-v4-flash` / `pool-glm-5.2` 等，targets.json 白名单）；modelMapping：`opus/sonnet → pool-deepseek-v4-pro`、`haiku → pool-deepseek-v4-flash` |

### QClaw 三层架构

1. **Layer 1**：QClaw Electron 应用（`127.0.0.1:19000` auth gateway）
2. **Layer 2**：openclaw runtime（随机端口，如 60227/49613）
3. **Layer 3**：上游 LLM 服务（`mmgrcalltoken.3g.qq.com`）

**为什么放弃 19000 网关**：19000 需要签名认证（早期结论为 Ed25519 设备签名；
2026-07-16 逆向修正为 **HMAC-SHA256 + OS 级 PID 反查**，见
[QCLAW_19000_GATEWAY_REVERSE.md](../QCLAW_19000_GATEWAY_REVERSE.md)），
客户端外进程无法模拟，反复尝试导致 9002 "该功能暂不可用"。**独立签名不可行**，
唯一可行的 19000 方案是寄生（`qclaw_inject.js`，见 §4.2）。

**为什么放弃动态端口**：60227 等动态端口是 agent 级别会话接口，非 LLM 级别，且每次启动端口变化。

**最终方案**：用 GetQClawAPIKey 方法（参考 `github.com/wenjiazhu1980/GetQClawAPIKey`），
从 QClaw 客户端本地存储解密 API Key，直连 Layer 3 上游。

---

## 2. API Key 自动解密（Windows）

### 2.1 解密链路

```
%APPDATA%\QClaw\Local State
  └─ os_crypt.encrypted_key (base64)
       ├─ 前缀 "DPAPI" (5 字节)
       └─ DPAPI blob → CryptUnprotectData() → AES-256 密钥 (32 字节)

%APPDATA%\QClaw\app-store.json
  └─ authGateway.providers.qclaw.apiKey.cipherText (base64)
       ├─ 前缀 "v10" (3 字节)
       ├─ nonce (12 字节)
       ├─ 密文 (变长)
       └─ GCM tag (16 字节)
       → AES-256-GCM 解密 → API Key (sk-...)
```

- Windows 上 QClaw 用 Electron safeStorage 加密：DPAPI 保护 AES-256-GCM 密钥（Chrome 风格 os_crypt）
- macOS 上为 Keychain 密码派生 AES-128-CBC（参考 GetQClawAPIKey 项目的 `decryptChromiumV10()`，本代理未实现）
- 代码实现：`server.py` 中 `_dpapi_unprotect()`（ctypes 调用 CryptUnprotectData）+ `_decrypt_qclaw_api_key()`；`crack_qclaw.py` 中为同逻辑的独立副本

### 2.2 关键文件

| 文件 | 用途 | 解密依赖 |
|------|------|---------|
| `%APPDATA%\QClaw\app-store.json` | 加密的 API Key + JWT + 用户信息 | 需配合 Local State 解密 |
| `%APPDATA%\QClaw\Local State` | DPAPI 保护的 AES-256 密钥 | 需当前用户 DPAPI |
| `~/.qclaw/qclaw.json` | QClaw 配置（authGatewayBaseUrl, guid） | 明文 |
| `~/.qclaw/openclaw.json` | openclaw runtime 配置（动态端口, auth token） | 明文 |

`app-store.json` 中加密的字段：
- `authGateway.providers.qclaw.apiKey` → API Key（`sk-...`）
- `secure.jwtToken` → JWT 令牌（HS256，30 天有效，积分查询用）
- `secure.userInfo` → 用户信息（JSON：loginKey, guid, userId）

### 2.3 环境变量覆盖

- `QCLAW_API_KEY=sk-xxxx`（`.env`）优先级最高，可手动指定/覆盖
- **非 Windows 也能用**：本地无法破解（DPAPI 仅 Windows），但设置环境变量或 dashboard 手动填 key 后仍可直连上游

> **QClaw 客户端只需登录过一次，代理就能自动拿到 Key，不需要 QClaw 持续运行**（除非用 qclaw-local 模式）。

---

## 3. 三条铁律（关键约束）

| # | 约束 | 原因 | 解决方案 |
|---|------|------|---------|
| 1 | **User-Agent 必须 `OpenAI/JS 6.39.1`** | 上游拒绝 `python-httpx/x.x.x` 默认 UA，返回 400 "invalid request"（对比 Python httpx 400 / Node.js fetch 200 实测发现） | 所有 qclaw 请求头固定注入 `User-Agent: OpenAI/JS 6.39.1`（或 node-fetch 等） |
| 2 | **所有 httpx 客户端 `trust_env=False`** | Python urllib/httpx 受系统代理干扰返回 400，Node.js fetch 不受影响 | 全局连接池 `get_http_client()` 及所有 qclaw 专用客户端均 `trust_env=False` |
| 3 | **body 白名单清理 `_QCLAW_ALLOWED_KEYS`** | 上游只认标准 OpenAI chat completion 字段；`thinking`/`reasoning_effort`/`metadata` 等 Anthropic 专属字段导致 9002 | `_clean_qclaw_body()` 按白名单剔除非标准字段 |

`_QCLAW_ALLOWED_KEYS` 白名单（server.py，共 22 键）：

```
model / messages / max_tokens / max_completion_tokens / stream / temperature /
top_p / stop / tools / tool_choice / frequency_penalty / presence_penalty /
n / user / seed / logprobs / top_logprobs / response_format / logit_bias / cache_control
```

补充约束：
- **自动补 system 消息**：上游要求必须同时有 system + user，只传 user 消息返回 400，代理自动补全
- **模型先注册**：QClaw 模型名不在 LiteLLM 内置映射，必须 `litellm.register_model()`（`_qclaw_all_models`），否则报 "model isn't mapped"
- **usage 本地估算**：QClaw 网关过滤 usage 字段，代理用 tiktoken 本地估算注入（`_estimate_messages_tokens` / `_estimate_text_tokens`，cl100k_base，误差 ±10%，仅客户端展示不影响计费）

---

## 4. 两种接入模式

### 4.1 直连上游（默认，推荐）

```
客户端 → server.py → _clean_qclaw_body() → 补 system 消息 → httpx(trust_env=False)
  → POST https://mmgrcalltoken.3g.qq.com/aizone/v1/chat/completions
  → Headers: Authorization: Bearer sk-xxx, User-Agent: OpenAI/JS 6.39.1
  → 响应原样返回给客户端
```

- 透传链路（`/v1/chat/completions`）与翻译链路（`/v1/messages` 经 LiteLLM `_qclaw_provider`）均支持
- 两条链路都会：自动补 system 消息、清理非标准字段、伪装 User-Agent
- 模型名去 `qclaw/` 前缀；异常自动重试 3 次
- 启动诊断：`startup diag: QClaw upstream = 200` 表示上游连通

### 4.2 qclaw-local（寄生模式，走 19000）

当直连不可行（如上游风控）时，可通过寄生服务器走 QClaw 本地网关（CHANGELOG 2026-07-16）：

```
架构: client → server.py → 19001(寄生服务器) → 19000(QClaw 网关) → 上游 LLM
```

- `qclaw_inject.js`：通过 Electron inspector 在 QClaw 主进程内注入 HTTP 服务器（**19001 端口**），
  复用 QClaw 自带的 axios 实例（含签名拦截器）转发请求到 19000 网关——绕过无法伪造的 PID 反查签名
- server.py 中 `QCLAW_LOCAL_BASE_URL = http://127.0.0.1:19001`，透传链路和 LiteLLM provider 链路均支持
- 启动诊断自动选择对应 base URL（`PREFERRED_PROVIDER=qclaw-local` 时）

```bash
# 1. QClaw 需以 --inspect=9229 模式启动
# 2. 注入寄生转发服务器
node qclaw_inject.js

# 3. 启动代理
$env:PREFERRED_PROVIDER = "qclaw-local"
python server.py
```

- ⚠️ 该模式需要 QClaw 客户端持续运行，且已实测通过：非流式 ✅、流式（SSE）✅、Anthropic 格式 ✅
- ⚠️ `qclaw_inject.js` 只复制 axios 请求拦截器，不复制响应拦截器（流式响应处理差异）

---

## 5. secrets 字段与凭据 schema

| 字段 | 必填 | 用途 |
|------|------|------|
| `qclaw_api_key` | ✅ | LLM 网关认证（`sk-` 前缀，**仅此字段即可正常代理**） |
| `qclaw_openclaw_token` | — | jprx 业务网关 `X-OpenClaw-Token`（HS256 JWT，30 天有效，积分查询用） |
| `qclaw_guid` | — | 设备 GUID（`X-Guid` 头，积分查询用） |
| `qclaw_user_id` | — | QClaw 账号 ID（`X-Account` 头，积分查询用） |
| `qclaw_device_token` | — | `X-Qclaw-DeviceToken`，设备绑定令牌（积分查询用） |
| `qclaw_login_key` | — | `X-Token` 头，**新版 QClaw 已无此字段，留空即可** |
| `qclaw_nickname` | — | 账号昵称（仅展示） |

> **凭证最小原则**：只填 `qclaw_api_key` 即可正常 LLM 代理；其余字段为积分查询增强，
> 缺字段时 dashboard 状态区显示降级提示（"仅 LLM 代理可用"），不阻塞使用。

---

## 6. 积分额度查询（crack_qclaw_q.py）

逆向自 QClaw v0.2.35.624 客户端与 jprx 业务网关实测。网关：`https://jprx.m.qq.com/data/<cmd>/forward`（POST，JSON）。

| cmd | 功能 | 返回要点 |
|-----|------|---------|
| `data/4110/forward` | 积分余额（getQPointAccount） | `balance` / `total_daily_free_granted` / `balance_detail.items[].expire_time` |
| `data/4075/forward` | 今日剩余 token（getTodayRemainingTokens） | `daily_token_limit` / `daily_token_used`（当日额度） |
| `data/4222/forward` | 积分流水（queryQPointFlow，body 带 `page:1`） | `total` / `flows[0]`（model_name / amount / created_at） |

请求头（对齐客户端 getCommonHeaders）：

```
X-Version: 1
X-Token:        <qclaw_login_key>        （新版已无此字段 → 空串）
X-Guid:         <qclaw_guid>
X-Account:      <qclaw_user_id>
X-OpenClaw-Token: <qclaw_openclaw_token> （JWT）
X-Qclaw-DeviceToken: <qclaw_device_token>
```

- body 需带 `web_version: 1.4.0`（客户端 API_VERSION）与 `web_env: release`（客户端自动追加）
- 响应壳兼容 4110/4075 的 `data.resp.data` 与 4222 的 `resp.data` 两种格式
- 依赖仅标准库 + httpx（`trust_env=False`）
- dashboard 统一入口 `GET /api/crack/qclaw/status`（crack_common 注册表，qclaw 走 `HANDLER_TAKES_SECRETS` 传完整 secrets）

```bash
.venv/bin/python crack_qclaw_q.py                # 从 secrets.json 读 qclaw_* 字段查询
.venv/bin/python crack_qclaw_q.py --secrets x.json
```

---

## 7. 排查指南

### 7.1 API Key 解密失败

```
⚠️ QClaw API Key not available
```

1. 确认 QClaw 客户端已安装并登录过
2. 检查 `%APPDATA%\QClaw\app-store.json` 是否存在 `authGateway.providers.qclaw.apiKey`
3. 检查 `%APPDATA%\QClaw\Local State` 是否存在 `os_crypt.encrypted_key`
4. 手动指定：`.env` 中设置 `QCLAW_API_KEY=sk-xxxx`

### 7.2 启动诊断返回非 200

```
startup diag: QClaw upstream = 400
```

- **400** = 请求格式问题（检查 User-Agent / 是否自动补 system 消息 / body 是否有非白名单字段）
- **401/403** = API Key 过期，重新登录 QClaw 客户端刷新 Key

### 7.3 重新解密 API Key

Key 过期时：打开 QClaw 客户端重新登录 → 重启 proxy 即可自动重新解密。也可手动验证：

```python
from server import _decrypt_qclaw_api_key
key = _decrypt_qclaw_api_key()
print(key)
```

### 7.4 9002 错误

- 直连上游模式下不会出现 9002（该错误属 19000 网关签名认证失败）
- 若出现：检查 body 是否被 `_clean_qclaw_body()` 清理（日志 `🧹 QClaw body cleaned: removed keys=...`）、
  User-Agent 是否为 `OpenAI/JS 6.39.1`
- qclaw-local 模式下出现 9002 → 19000 签名链路问题，排查 `qclaw_inject.js` 注入是否成功（19001 端口是否监听）

---

## 8. 已知陷阱

1. **QClaw 升级**：版本号硬编码在多处路径中（如 `v0.2.33.617`），升级后需全局替换。
2. **LiteLLM 模型注册**：QClaw 模型名不在 LiteLLM 内置映射中，必须 `litellm.register_model()` 注册，否则报 "model isn't mapped"。
3. **body 清理**：客户端可能透传 Anthropic 专属字段（`thinking`、`reasoning_effort`、`output_config`），上游会 400/9002，必须走 `_clean_qclaw_body()` 白名单。
4. **流式响应循环引用**：`qclaw_inject.js` 只复制 axios 请求拦截器，不复制响应拦截器。
5. **QClaw 网关过滤 usage**：上游响应没有 `usage` 字段，代理用 tiktoken 本地估算并注入。
6. **跨进程状态放 `.cache/`**：代理 mount namespace 隔离，`/tmp` 与 shell 不通（crack_daily 时间戳等）。

---

## 附：与本文档相关的代码位置

| 文件 | 说明 |
|------|------|
| `crack_qclaw.py` | Windows DPAPI 提取 API Key（独立 CLI，`--secrets` / `--force`） |
| `crack_qclaw_q.py` | jprx 积分/今日 token/流水查询（4110/4075/4222） |
| `crack_common.py` | `CREDENTIAL_SCHEMAS["qclaw"]`（凭据 schema）+ `CRACK_STATUS_HANDLERS` 注册表 |
| `server.py` | `_dpapi_unprotect` / `_decrypt_qclaw_api_key` / `_clean_qclaw_body` / `_qclaw_provider` / `_passthrough_to_qclaw` / `get_http_client` / `_qclaw_all_models` |
| `qclaw_inject.js` | 19001 寄生转发服务器（qclaw-local 模式） |
| `config_store.py` | `VALID_HANDLERS` 含 `qclaw` |
| `targets.json` | qclaw target 定义（8085、`mmgrcalltoken.3g.qq.com`、`routePrefix=/aizone/v1`、modelMapping） |
| `QCLAW_19000_GATEWAY_REVERSE.md` | 19000 网关逆向调研报告（HMAC-SHA256 签名 / PID 反查 / 寄生注入） |
