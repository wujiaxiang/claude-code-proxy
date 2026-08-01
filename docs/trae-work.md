# Trae Work 破解与 API 逆向文档

> 本文档固化了字节跳动 Trae Work（TRAE SOLO CN）的 LLM API 认证破解成果：
> 认证数据存储、tc 加密格式、全部 API 接口规范、设备指纹 header、token 生命周期，
> 以及配套工具 `crack_traework.py` / `crack_common.py` 的用法与架构。
> 所有接口与参数均来自实际逆向与实测验证（2026-08），非官方文档。
>
> 本文档是 [`crack_traework.py`](../crack_traework.py)、[`crack_common.py`](../crack_common.py)
> 与代理 8086 端口（handler=`trae-work`）的实现参考。

---

## 1. 产品背景

**Trae Work = TRAE SOLO CN**，是字节跳动的一款独立 AI 工作产品，区别于 **Trae CN（Trae IDE）**。

| 维度 | Trae CN（Trae IDE） | Trae Work（TRAE SOLO CN） |
|------|--------------------|--------------------------|
| 角色 | 集成开发环境 | 独立 AI 工作产品（本代理接入目标） |
| 产品码 | — | `SOLO_Lite` |
| 版本 | — | `0.1.43`（`x-ide-version: 0.1.43`） |
| 安装目录 | `%USERPROFILE%\Trae CN\` | `%APPDATA%\TRAE SOLO CN\` |

> ⚠️ **易踩坑**：两个产品的安装目录非常相似，破解时务必确认操作的是
> `TRAE SOLO CN`（Trae Work），不是 `Trae CN`（Trae IDE）。详见[排查备忘](#10-排查备忘)。

在 `claude-code-proxy` 中，Trae Work 作为 crack 类网关接入：

- **端口**：8086（targets.json 中 `listenPort=8086`，handler=`trae-work`，默认未启用，需在 targets.json 中启用）
- **上游**：`trae-api-cn.mchost.guru`（LLM 网关）与 `api.trae.cn`（业务 API）
- **分类**：`crack`（本地客户端提取 token，代理注入认证）
- **客户端接入**：`base_url = http://<host>:8086/v1`，`api_key = "dummy"`（crack 类代理不校验）

涉及两台机器：

- **CT105（Linux）**：本机，跑代理
- **192.168.2.177（Windows）**：SSH 免密（`Administrator`），装有 Trae Work 客户端，是 token 提取源

---

## 2. 认证数据存储与 tc 解密

### 2.1 存储位置

```
路径: %APPDATA%\TRAE SOLO CN\User\globalStorage\storage.json
key:  iCubeAuthInfo://icube.cloudide
```

- 明文 JSON 字段：`token`（access，即 Cloud-IDE-JWT）、`refreshToken`、`expiredAt`、
  `refreshExpiredAt`、`userId`、`host`、`account`
- 值是 base64 编码的加密 blob，以 `dGMFEAAA...` 开头（`dGMFEAAA` 是 `t\x05\x10\x00\x00` 的 base64）

### 2.2 tc 加密格式

解码后整体布局：

```
[6B header][32B random][AES-128-CBC 密文]
```

| 段 | 长度 | 说明 |
|----|------|------|
| header | 6B | 加密类型标记。`74 63 05 10 00 00`（"tc" + 类型）= AES；另有 AES_PRIVATE 类型 |
| random | 32B | 随机数，参与密钥派生 |
| 密文 | 可变 | AES-128-CBC，padding 后为明文的密文 |

解密结果：

```
[64B SHA-512 hash][明文 JSON]
```

- 前 64 字节是明文 JSON 的 SHA-512 哈希，用于完整性校验
- 校验通过后的剩余部分才是真实明文

### 2.3 密钥派生（SHA-512）

```
salt = SALT_A XOR SALT_B   # AES 类型（每字节异或，共 64B）
       SALT_C XOR SALT_D   # AES_PRIVATE 类型
key  = SHA-512(SHA-512(random) + salt)[0:16]
iv   = SHA-512(SHA-512(random) + salt)[16:32]
```

- key/iv 来自 `SHA-512(SHA-512(random) + salt)` 输出，前 16 字节为 key，16~32 字节为 iv
- 4 组盐值硬编码在 `crack_common.py` / `crack_traework.py` 中，来源是开源项目
  [ZedeX/trae-local-api](#9-开源项目参考) 的逆向成果

> **crack_traework.py 中的实现**：`_detect_enc_type()` 识别 header 类型，
> `_decrypt_tc()` 完成派生 + 解密 + SHA-512 校验。

---

## 3. API 端点总表

### 3.1 LLM 与 IDE 业务端点（`trae-api-cn.mchost.guru`）

| 用途 | 端点 | 认证 |
|------|------|------|
| LLM 对话 | `POST https://trae-api-cn.mchost.guru/api/agent/v3/llm_utils_chat` | Cloud-IDE-JWT + 设备指纹 |
| 创建代理任务 | `POST https://trae-api-cn.mchost.guru/api/agent/v3/create_agent_task` | 同上 |
| 模型列表 | `POST https://trae-api-cn.mchost.guru/api/ide/v1/get_detail_param`（body: `function=chat_v3`） | 同上 |

### 3.2 账户业务端点（`api.trae.cn`）

| 用途 | 端点 | 认证 |
|------|------|------|
| 额度查询 | `POST https://api.trae.cn/trae/api/v2/pay/ide_user_ent_usage` | Cloud-IDE-JWT + 设备指纹 |
| 签到状态 | `POST https://api.trae.cn/trae/api/v2/ug/checkin_credits/status` | 同上 |
| 签到领取 | `POST https://api.trae.cn/trae/api/v2/ug/checkin_credits/claim` | 同上 |
| 刷新 token | `POST https://api.trae.cn/cloudide/api/v3/trae/oauth/ExchangeToken` | **无**（裸调即可，设备已 BOUND） |

---

## 4. 认证 header 清单（设备指纹，全部固定值）

所有 LLM / 业务请求（除 ExchangeToken）都需携带：

```
Authorization: Cloud-IDE-JWT <token>
x-app-id: 6eefa01c-1036-4c7e-9ca5-d891f63bfcd8
x-ide-version: 0.1.43
x-ide-version-code: 20260730
x-device-id: 199444637423849
x-machine-id: d2115a713ee587fea5d340ceb8ef1fda3ad808431c24e7fed3085693f52f4428
x-device-type: windows
X-Trae-Client-Type: lite
x-trae-authorized-services: feishu
```

- `Authorization` 的 `token` 即 storage.json 解密后的 access token
- `x-*` 各字段均为设备指纹固定值，实测不会随时间变化，可直接硬编码
- `X-Trae-Client-Type: lite` 对应 SOLO_Lite 产品码

---

## 5. LLM 请求/响应协议

### 5.1 OpenAI → Trae 请求体转换

代理 8086 把 OpenAI `/v1/chat/completions` 请求转换为 Trae `llm_utils_chat` 格式：

```json
POST /api/agent/v3/llm_utils_chat
{
  "messages": [
    {
      "role": "user",
      "content": [{"type": "text", "text": "hi"}],
      "role_type": 0
    }
  ],
  "function": "chat_v3",
  "stream": true,
  "model": "glm-5.2",
  "config_name": "glm-5.2"
}
```

- `content` 是**多模态数组**，`{"type": "text", "text": "..."}` 为文本块
- `role_type: 0` 表示普通用户消息
- `model` 与 `config_name` 相同（如 `glm-5.2`）

### 5.2 SSE 响应事件

响应为 SSE 流，关键事件：

```
event: metadata        # 元数据（模型、会话等）
event: timing_cost     # 耗时统计
event: output          # 正文输出，data 含 response + reasoning_content
```

- `event:output` 的 data 是 JSON，包含 `response`（正文）与 `reasoning_content`（思考内容）
- 代理需要把 SSE 事件还原为 OpenAI 流式分块格式，并把 `reasoning_content` 映射到
  OpenAI 的 `reasoning` 字段（若客户端支持）

---

## 6. token 生命周期

### 6.1 有效期

| token | 有效期 | 说明 |
|-------|--------|------|
| access（Cloud-IDE-JWT） | **14 天**（JWT，`exp - iat = 1210000s`） | 用于全部 API 调用的 Authorization |
| refresh | **半年**（`refreshExpiredAt`） | 每次刷新轮换新值，**可无限续期** |

### 6.2 刷新协议

- 端点：`POST https://api.trae.cn/cloudide/api/v3/trae/oauth/ExchangeToken`
- **当前设备已 BOUND**（`BoundDeviceID=qwy86n6rolp7r5`），裸调 ExchangeToken 即可刷新，**无需任何签名**
- 完整协议（逆向 main.js）实际带 RSA DeviceProof 签名 + DeviceInfo，但设备已绑定时可省略
- ⚠️ **风险**：若换设备，BOUND 失效，则需要完整 RSA 签名协议才能续期

### 6.3 自动续期

- `crack_traework.py --refresh` 用 secrets.json 中的 `refreshToken` 刷新 access token 并回写
- cron 脚本 `trae_work_daily.sh`（`0 3 * * *`）每日执行：签到 + 当 token 剩余有效期 < 3 天才触发刷新

### 6.4 额度结构示例（`ide_user_ent_usage` 返回）

用户"阿软259"（userId=`2328093701182980`）：**积分计费**（`is_credits_billing=true`），权益包：

- 老用户福利 2000×2（到期 2026-08-31）
- 免费额度 / 每月登录赠送 500 / 签到奖励 200×2

---

## 7. crack_traework.py 用法

工具位于仓库根目录，Linux / Windows 均可运行（Windows 端提取 storage.json，Linux 端做解密与刷新）。

```
python crack_traework.py [--secrets secrets.json] [--force]
python crack_traework.py --checkin        # 查询今日签到状态
python crack_traework.py --claim          # 执行每日签到
python crack_traework.py --quota          # 查询剩余额度
python crack_traework.py --refresh        # 用 refreshToken 刷新 access token
python crack_traework.py --export         # 导出私密数据 JSON（供 dashboard 粘贴）
python crack_traework.py --import-json FILE   # 从 JSON 文件/粘贴内容导入私密数据
```

| 参数 | 作用 |
|------|------|
| （无） | 默认：从本机 storage.json 解密并写入 secrets.json |
| `--secrets <path>` | 指定 secrets.json 路径（默认仓库根目录） |
| `--force` | 即使已有 key 也重新提取 |
| `--checkin` | 查询今日签到状态 |
| `--claim` | 执行每日签到领取 |
| `--quota` | 查询剩余额度 |
| `--refresh` | 刷新 access token（**只需 secrets.json 里的 refreshToken，无需本地 storage.json**） |
| `--export` | 导出私密数据 JSON |
| `--import-json <FILE>` | 从 JSON 导入私密数据 |

写入 secrets.json 的字段：`trae_work_token` / `trae_work_refresh_token` / `trae_work_user_id`
（secrets.json 已 gitignore，不入库）。

---

## 8. crack_common.py 架构（CRACK_STATUS_HANDLERS 注册表）

`crack_common.py` 提供破解网关的公共能力：

- **tc 解密**：`_detect_enc_type()` / `_decrypt_tc()`，供 crack_traework 复用
- **Trae 状态查询**：额度、签到、token 有效期
- **`CRACK_STATUS_HANDLERS` 注册表**：破解网关状态查询的统一入口

```python
CRACK_STATUS_HANDLERS = {
    "trae-work": trae_status,
    # "codebuddy": codebuddy_status,   # TODO: 待实现
    # "qclaw":     qclaw_status,       # TODO: 待实现
}
```

`get_crack_status(label, secrets)` 是 dashboard 的统一入口：

1. 按 `label` 查注册表，未注册则返回 `supported: false`
2. 从 secrets 读取 `<label 的 _ 形式>_token` / `<...>_refresh_token`，缺失则返回 `configured: false`
3. 调用对应 handler，返回额度 / 签到 / token 有效期，并附带 `supported: true, configured: true`

**扩展点**：未来 codebuddy / qclaw 的签到 / 额度 / 状态查询，只需实现一个
`xxx_status(token, refresh) -> dict` 函数并在注册表登记即可，dashboard 自动展示。
CodeBuddy Pro 用户每日 +100 credits 的签到可仿照 trae-work 模式接入。

server.py 对应 API：

- `GET /api/crack/{label}/status` — 额度 / 签到 / token 状态（调 `crack_common.get_crack_status`）
- `PUT /api/secrets/{label}/bulk` — 批量导入私密 JSON（body: `{"data": {"token":..., "refreshToken":..., "userId":...}}`）

---

## 9. 开源项目参考

以下开源项目为本任务的破解提供了关键算法与思路（**仅作参考，非官方**）：

| 项目 | 贡献 |
|------|------|
| [ZedeX/trae-local-api](https://github.com/ZedeX/trae-local-api) | **tc 解密算法来源**：AES-128-CBC + SHA-512 盐值派生，本文档 2.3 节的密钥派生即出自此项目逆向 |
| [laojichao/trae-local-api](https://github.com/laojichao/trae-local-api) | 上述项目的 fork，**支持 solo 版**（TRAE SOLO） |
| [Oh-My-Trae/trae-db-decrypt](https://github.com/Oh-My-Trae/trae-db-decrypt) | **SQLCipher 数据库解密**（Trae 本地数据库） |
| [linqiu919/trae2api](https://github.com/linqiu919/trae2api) | Trae API 封装（转 OpenAI 兼容 API） |
| [likecu/trae-minimax-client](https://github.com/likecu/trae-minimax-client) | Trae API 封装（minimax 客户端） |
| [guoq1/check1](https://github.com/guoq1/check1) | 第三方签到脚本（非 Trae 官方） |
| [vibe-coding-labs/trae-reverse-engineering](https://github.com/vibe-coding-labs/trae-reverse-engineering) | Trae 逆向分析 |

---

## 10. 排查备忘

### 10.1 关键路径

| 对象 | 路径 |
|------|------|
| CT105（Linux）工作目录 | `/root/shared-workspace/claude-code-proxy` |
| CT105 Python | `.venv/bin/python`（含 cryptography / httpx） |
| Windows 机器 | SSH 免密：`ssh Administrator@192.168.2.177` |
| Windows pwsh | `"C:\Program Files\PowerShell\7\pwsh.exe" -NoProfile -ExecutionPolicy Bypass -File x.ps1` |
| Windows Python | `C:\Users\Administrator\AppData\Local\hermes\hermes-agent\venv\Scripts\python.exe -X utf8` |
| Trae Work 数据目录 | `C:\Users\Administrator\AppData\Roaming\TRAE SOLO CN\` |

### 10.2 代理日常管理（CT105）

```bash
# 重启代理（8081 冲突时先确认端口）
pkill -9 -f server.py
setsid .venv/bin/python server.py > /tmp/proxy.log 2>&1 < /dev/null &

# 端口对照
# 8081=dashboard/FastAPI  8082=copilot  8084=codebuddy
# 8085=qclaw  8086=trae-work  8090-8094=free/paid
```

### 10.3 CDP 抓包方法（曾用）

- 代理曾通过 CDP（9224 端口）抓包 trae-work renderer 进程
- 请求头里可见 `Authorization: Cloud-IDE-JWT`，是确认认证 header 来源的实证手段

### 10.4 常见坑

1. **trae-work vs trae-ide 混淆**：`C:\Users\Administrator\Trae CN\` 是 **Trae IDE**（不是目标）；
   `C:\Users\Administrator\TRAE SOLO CN\` 才是 **Trae Work**（目标）。目录只差一个词，极易搞混。
2. **token 泄露面**：refreshToken 半年有效，勿外传；secrets.json 已 gitignore，别 `git add .`。
3. **ExchangeToken 设备绑定**：当前裸调成功（BOUND）；换设备后需完整 RSA 签名协议（逆向 main.js 可得）。
4. **dashboard 是 Python f-string**：内嵌 JS 的 `{` / `}` 必须写成 `{{` / `}}`，且不能有反斜杠。

---

## 附：与本文档相关的代码位置

| 文件 | 说明 |
|------|------|
| `crack_traework.py` | tc 解密、提取、签到、额度、刷新、导出/导入 |
| `crack_common.py` | tc 解密公共函数 + `CRACK_STATUS_HANDLERS` 注册表 |
| `server.py` | `_handle_traework` handler（OpenAI ↔ llm_utils_chat 转换）+ `_crack_env_check` trae 分支 + 状态/批量导入 API |
| `config_store.py` | `VALID_HANDLERS` 含 `trae-work` |
| `targets.json` | trae-work target 定义（8086、19 模型 + modelMapping） |
| `trae_work_daily.sh` | cron 每日签到 + token 剩 <3 天刷新 |
