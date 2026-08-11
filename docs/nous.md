# Nous Portal 网关（8096）

> 直连 [Nous Portal](https://portal.nousresearch.com)（Hermes 的模型订阅网关），
> 免费 tier 的 `:free` 模型。凭据由 **hermes 容器**（`nousresearch/hermes-agent`）维护，
> 代理只做定时同步，**不自刷 token**。

## 概述

| 项 | 值 |
|---|---|
| 端口 | 8096 |
| 分类 | crack |
| handler | passthrough |
| 上游 | `https://inference-api.nousresearch.com/v1`（OpenAI 兼容） |
| 认证 | `Authorization: Bearer <access_token>`（inference-scoped JWT） |
| 凭据来源 | hermes 容器 `auth.json`（宿主机 `/data/docker/hermes/data/auth.json`） |
| 模型 | 免费 tier `:free` 后缀模型（2026-08-11 实测 5 个） |

## 模型（免费 tier 实测）

- `poolside/laguna-s-2.1:free`
- `poolside/laguna-xs-2.1:free`
- `tencent/hy3:free`
- `stepfun/step-3.7-flash:free`
- `upstage/solar-pro4:free`

付费模型在免费账号下返回 404（"not available on the Free Tier"）。

## 凭据与同步机制（关键设计）

**职责划分**（刻意设计）：
- **hermes 容器**负责 OAuth token 全生命周期：登录（`hermes auth add nous --type oauth`）、
  自动刷新（每次推理调用经 `resolve_nous_runtime_credentials()` 检查过期并用 refresh_token
  换新 JWT，回写 `auth.json`）。**代理绝不自己调 refresh 端点**——避免与 hermes 双写竞争
  refresh_token 轮换（OAuth 2.1 轮换下双写会导致 token 互相作废）。
- **代理**（`gateways/nous.py`）只做**只读同步**（**永不触发刷新**）：
  1. 每 60s 只读 hermes `auth.json` → `providers.nous` 的 `access_token` / `expires_at`
  2. token 剩余寿命 < 10 分钟 / 已过期 / 缺失 → **仅告警**（proxy.log `nous:` warning），
     刷新与重新登录由 hermes 自身生命周期负责，代理不介入
  3. token 变化才写 `secrets.json`（`nous_access_token` / `nous_expires_at`），避免无谓热重载
  4. 请求时由 crack 类通用注入逻辑加 `Authorization: Bearer`（secretRef=`nous_access_token`）

> ⚠️ **2026-08 踩坑记录（勿改回"触发刷新"）**：早期版本曾在 token 快过期时
> `docker exec hermes ... resolve_nous_runtime_credentials(force_refresh=True)` 触发刷新，
> 结果导致 **Nous Portal revoke 整个 session**（auth.json 的 `last_auth_error` =
> "refresh-token reuse... only Hermes may call the refresh endpoint"）。
> 原因：Nous refresh_token **单次使用**，外部触发刷新未持久化旋转后的新 refresh_token，
> 下次复用旧 token 即被判 reuse revoke；且 docker exec 默认以容器 root 回写 auth.json，
> 属主变 root:root 后 hermes 主进程（hermes 用户）失去写权限无法刷 token。教训：
> **auth.json 的唯一写入者必须是 hermes 进程，代理跨容器零写**。

**OAuth 刷新协议**（供参考，代理不直接调用）：
```
POST https://portal.nousresearch.com/api/oauth/token
Header:  x-nous-refresh-token: <refresh_token>
Body:    grant_type=refresh_token&client_id=hermes-cli
→ {access_token(新JWT), refresh_token(轮换), expires_in, ...}
```
- `client_id = "hermes-cli"`，`scope = "inference:invoke"`

## 前置依赖

- **hermes 容器必须活着且已登录**（`docker ps` 有 `hermes`，`/opt/data/auth.json` 的
  `providers.nous` 非空）。容器挂了 → token 停更 → 8096 请求 401。
- 重启 hermes 容器后需确认登录态仍在（登录态在挂载卷 `/data/docker/hermes/data`，容器重建不丢）。
- 代理进程以 root 运行（systemd），可直接读 `auth.json`（owner uid 10000）。

## 日常命令

```bash
# 手动同步凭据（只读拷贝；--force 强制重新同步，无 --refresh 选项——不触发刷新）
.venv/bin/python3 crack_nous.py --force

# 查看 token 状态
python3 -c "import json; s=json.load(open('secrets.json')); print(s.get('nous_expires_at'))"

# 健康检查（官方推荐，勿直接调 refresh 端点）
docker exec hermes /opt/hermes/.venv/bin/hermes auth status --provider nous

# 重新登录 Nous Portal（token 失效/被 revoke 时，OAuth 交互式授权）
docker exec -it hermes /opt/hermes/.venv/bin/hermes auth add nous --type oauth

# 直连测试
curl -sN http://127.0.0.1:8096/v1/chat/completions \
  -H "Content-Type: application/json" -H "Authorization: Bearer dummy" \
  -d '{"model":"tencent/hy3:free","stream":true,"messages":[{"role":"user","content":"hi"}]}'
```

## 已知陷阱

1. **免费 tier 限 :free 模型**：调付费模型返回 404；新 `:free` 模型出现时
   `GET /v1/models` 拉取后加进 targets.json 白名单。
2. **hermes 容器维护**：**不要**手动改 hermes 容器内 auth.json；**不要**用 docker exec /
   脚本另开 refresh 会话触发刷新（Nous refresh_token 单次使用，外部触发不持久化旋转
   token 会被 revoke session，详见上文踩坑记录）。健康检查用 `hermes auth status`，
   重新登录用 `hermes auth add nous --type oauth`。
3. **同步器日志**：token 无变化时不打日志（正常）；异常只在 proxy.log 打 `nous:` 前缀 warning。
4. **重启顺序**：改 server.py / gateways/nous.py 后 `systemctl restart claude-code-proxy`；
   重启后首次同步在 lifespan 内执行（`_nous_sync_once`）。
