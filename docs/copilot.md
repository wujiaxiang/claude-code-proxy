# Copilot 破解网关文档

> GitHub Copilot 以**双端口**模式接入：8082 企业版（GHE）+ 8083 个人版，两者 token 完全隔离不可混用。
> 本文档固化了认证方式（gh CLI / 本地破解）、模型映射、额度查询（quota_snapshots）与模型清理机制。
>
> 本文档从 `docs/crack-tools.md`、`AGENTS.md`、`CHANGELOG.md` 迁移整理，2026-08-02。
> 本文档是 [`crack_copilot.py`](../crack_copilot.py)、[`crack_copilot_q.py`](../crack_copilot_q.py)
> 与代理 8082/8083 端口（handler=`copilot`）的实现参考。

---

## 1. 双端口模式（企业 vs 个人）

| 维度 | 8082 copilot-enterprise | 8083 copilot（个人版） |
|------|------------------------|------------------------|
| 上游（LLM） | `copilot-api.bmw.ghe.com`（GHE 企业版，收费） | `api.githubcopilot.com`（github.com 个人版） |
| 用量端点 | `https://api.bmw.ghe.com/copilot_internal/user`（可用 `COPILOT_GHE_API_HOST` 环境变量覆盖） | `https://api.github.com/copilot_internal/user` |
| secrets 字段 | `copilot_token`（**`github_pat_`** 前缀 GHE fine-grained PAT） | `copilot_personal_token`（**`gho_`** 前缀 OAuth token） |
| apikeyEnv | `COPILOT_GHE_TOKEN` | `COPILOT_PERSONAL_TOKEN` |
| crackTool | `crack_copilot.py` | `crack_copilot.py`（个人 token 从本机 `/root/.copilot/config.json` 破解） |
| 获取方式 | `gh auth token`（GitHub CLI，需已登录 GHE） | 本地 `/root/.copilot` 破解（与 8082 账号隔离） |
| 模型映射 | opus/sonnet → `claude-sonnet-5`，haiku → `claude-haiku-4.5` | opus → `claude-opus-4.8`，sonnet → `claude-sonnet-5`，haiku → `claude-haiku-4.5` |

> **隔离铁律**：两个账号 token 完全隔离，**不可混用**。企业 PAT 打到 api.github.com 会 401/403，个人 OAuth token 打到 GHE 同理。

客户端接入：`base_url = http://<host>:8082/v1`（或 8083），`api_key = "dummy"`（crack 类不校验）。

---

## 2. 认证（token 提取）

### 2.1 crack_copilot.py（跨平台，需 gh CLI）

```
python crack_copilot.py [--secrets secrets.json] [--force]
```

- 优先调用本机 `gh auth token`（需已登录 GitHub Enterprise），成功则写入 `secrets.json` 的 `copilot_token`
- 失败：退出码 1 + 引导文案（在已登录 gh CLI 的机器上运行 `gh auth token`，或 dashboard 手动填写）
- `--force`：即使已有 key 也重新提取；已有 key 时默认跳过
- 个人版（8083）：token 从本机 `/root/.copilot/config.json` 破解写入 `copilot_personal_token`

### 2.2 secrets 字段（凭据 schema）

| 字段 | 格式校验（pattern） | 说明 |
|------|---------------------|------|
| `copilot_token` | `^github_pat_[A-Za-z0-9_]{20,}$` | GHE 企业版 fine-grained PAT，需 Copilot 权限 |
| `copilot_personal_token` | `^gho_[A-Za-z0-9]{20,}$` | 个人版 OAuth token |

dashboard 凭据弹窗按此 schema 动态渲染表单 + 校验（`CREDENTIAL_SCHEMAS`）。

### 2.3 环境检测

- `_crack_env_check`：`shutil.which("gh")` 检测 gh CLI 是否在 PATH；个人版另探测 `/root/.copilot/config.json`
- 不可用时 dashboard「重新破解」按钮置灰，不阻止手动填写

---

## 3. 模型映射与请求头

### 3.1 模型映射（modelMapping）

客户端请求 `opus`/`sonnet`/`haiku` → 按 targets.json 的 `modelMapping` 映射为上游真实模型
（两端口映射见 §1 表格）。映射逻辑由 `_PROVIDER_STRATEGIES` 的 copilot provider 处理。

### 3.2 请求头

```
Authorization: Bearer <token>
Copilot-Integration-Id: copilot-developer-cli   # targets.json extraHeaders，环境变量 COPILOT_INTEGRATION_ID 可覆盖
```

- 代理透传时注入上述头，并清理空 content 和无效 tool_choice
- **路径重写**（`_HANDLER_PATH_MAP`）：`/v1/chat/completions` → `/chat/completions`、`/v1/models` → `/models`（上游无 `/v1` 前缀）
- Anthropic 协议经 8081 翻译后同样注入 `Copilot-Integration-Id`（LiteLLM 链路 `extra_headers`）

---

## 4. 额度查询（crack_copilot_q.py）

`GET https://{api-host}/copilot_internal/user` → 解析 `quota_snapshots`（chat/completions、premium_interactions 等条目）。

| label | 端点 | 认证 |
|-------|------|------|
| copilot-enterprise（8082） | `GET https://api.{ghe-host}/copilot_internal/user`（默认 `api.bmw.ghe.com`） | `Authorization: token <github_pat_xxx>` |
| copilot（8083） | `GET https://api.github.com/copilot_internal/user` | `Authorization: token <gho_xxx>` |

- 固定请求头：`Accept: application/json`、`X-GitHub-Api-Version: 2022-11-28`、`User-Agent: GitHubCopilot/1.0.0`
- **企业 seat 通常 `unlimited=true`**：chat/completions 无限额度，dashboard 显示 limit 为 ∞，used 取 `credits_used` 兜底；有限额条目按 `entitlement - remaining` 计算 used
- `quota_reset_date` / `quota_reset_date_utc` 作为 expireAt
- GHE 是内网自签名证书 → 用未验证的 HTTPS context 请求（仅标准库 urllib）
- Copilot 无签到机制（checkin 固定 disabled）；token 非 JWT，refresh 解析为 None
- 依赖仅标准库（urllib.request），不引入第三方包

```bash
.venv/bin/python crack_copilot_q.py   # 读 secrets.json 的 copilot_token 查询企业版额度
```

dashboard 统一入口：`GET /api/crack/copilot-enterprise/status` / `GET /api/crack/copilot/status`
（`CRACK_STATUS_HANDLERS` 注册表，`copilot_status` 与 `copilot_personal_status` 按 label 分发）。

---

## 5. 模型清理（prune-models）

- `POST /api/targets/{label}/prune-models`：对照上游最新模型列表删过期模型（配置 + 内存同步更新）
- **保护 modelMapping 目标**：映射目标上游不存在时修正为同族可用模型，避免映射断裂
- 仅 copilot 系（handler=copilot，上游有 `/models` 接口）支持；codebuddy/qclaw/trae-work 不显示清理按钮

---

## 6. 错误排查

| 现象 | 原因 | 处理 |
|------|------|------|
| 403 | token 无 Copilot 权限 / 过期 / 账号未订阅 | 重新 `gh auth token` 或重新登录 GHE，更新 secrets.json |
| 401（混用） | 企业 PAT 打到 api.github.com 或个人 token 打到 GHE | 检查 secrets 字段与端口对应（§1 隔离铁律） |
| `crack_copilot.py` 退出码 1 | gh CLI 未登录 / 未安装 | `gh auth login` 或 dashboard 手动填 PAT |
| quota 查询失败 | token 无 `copilot_internal/user` 访问权限 / 网络不通 | 检查 `Authorization: token` 前缀写法；GHE 用未验证 HTTPS context |
| 模型 404 | 映射目标在上游不存在 | 用 prune-models 清理过期模型并修正映射 |

---

## 7. 已知陷阱

1. **双端口 token 隔离**：8082 用 `copilot_token`（`github_pat_`），8083 用 `copilot_personal_token`（`gho_`），不可混用；改一处勿影响另一处。
2. **PATH 污染**：Windows 上调用 gh CLI 前先清理 PATH（Trae IDE 的 ripgrep 会污染，`$env:Path = "C:\Windows\System32;C:\Windows"`）。
3. **secrets 不入库**：`copilot_token` / `copilot_personal_token` 在 secrets.json（gitignored），勿 `git add .`。

---

## 附：与本文档相关的代码位置

| 文件 | 说明 |
|------|------|
| `crack_copilot.py` | gh CLI 提取 GHE token（`--secrets` / `--force`） |
| `crack_copilot_q.py` | 企业/个人额度查询（`copilot_status` / `copilot_personal_status`） |
| `crack_common.py` | `CREDENTIAL_SCHEMAS`（copilot-enterprise/copilot）+ 注册表 |
| `server.py` | `COPILOT_GHE_HOST` / `COPILOT_INTEGRATION_ID` / copilot provider 透传注入 / prune-models API |
| `targets.json` | 8082/8083 两个 copilot target 定义（modelMapping / extraHeaders / secretRef） |
