# 破解工具说明

本项目通过破解工具从本地客户端提取 API key/token，写入 `secrets.json`（dashboard 可编辑，不入库）。
启动时自动调用；也可作为独立 CLI 运行。

## 工具清单

| 工具 | 目标 | 提取来源 | OS 支持 |
|------|------|---------|---------|
| `crack_copilot.py` | GitHub Copilot Enterprise / 个人版 | `gh auth token`（GitHub CLI）或本机 `/root/.copilot/config.json` | ✅ 跨平台（需 gh CLI） |
| `crack_codebuddy.py` | CodeBuddy | 本机 CodeBuddy 客户端目录 | ✅ Windows / ❌ 其他 |
| `crack_qclaw.py` | QClaw | `%APPDATA%\QClaw\app-store.json`（DPAPI 解密） | ✅ Windows / ❌ 其他 |
| `crack_traework.py` | Trae Work | `%APPDATA%\TRAE SOLO CN\...\storage.json`（tc 加密解密） | ✅ Windows（可跨机导入） |

## 额度/签到状态查询（dashboard 展示）

`crack_*_q.py` 系列模块查询各破解网关的剩余额度/签到状态，dashboard 通过 `GET /api/crack/{label}/status` 统一展示（`crack_common.CRACK_STATUS_HANDLERS` 注册表分发）：

| 模块 | label | 查询内容 |
|------|-------|---------|
| `crack_common.py`（内建） | trae-work | 权益包额度（ide_user_ent_usage）+ 每日签到（checkin_credits） |
| `crack_copilot_q.py` | copilot-enterprise / copilot | `GET {api-host}/copilot_internal/user` → quota_snapshots（chat/completions/premium_interactions） |
| `crack_qclaw_q.py` | qclaw | `POST jprx.m.qq.com/data/4110/forward` 积分余额 + `data/4075` 今日 token + `data/4222` 流水 |
| `crack_codebuddy_q.py` | codebuddy | `POST {endpoint}/billing/meter/get-user-resource` 资源包额度 + `/v2/activity/growth/*` 成长任务/连续天数 |

返回统一结构：`{quota: [...], checkin: {...}, refresh: {...}, extra: {...}}`。

> **copilot 双模式**：8082（copilot-enterprise）用企业 PAT（`copilot_token`，github_pat_ 前缀）查 `api.bmw.ghe.com`；8083（copilot）用个人 token（`copilot_personal_token`，gho_ 前缀，从本地 `/root/.copilot` 破解）查 `api.github.com`。两个账号 token 完全隔离，不可混用。

## 统一每日任务（单一 cron）

`crack_daily.py` 是所有破解网关的**统一每日调度器**（签到/领取奖励/刷新 token），插件化注册，单一 cron 入口：

```
0 3 * * * /root/shared-workspace/claude-code-proxy/scripts/cron/crack_daily.sh
```

- **插件注册**：`DAILY_HANDLERS` 字典，每个网关注册一个 `daily(secrets, out)` 函数
- **无 key 跳过**：网关未在 secrets.json 配置 key 时自动跳过，不报错
- **当前任务**：trae-work 签到+刷新、codebuddy 成长任务领取、qclaw/copilot 仅校验 token
- **日志**：`/tmp/crack_daily.log`（-l/--log 可改）

```bash
# 手动运行全部
.venv/bin/python crack_daily.py --secrets secrets.json
# 只跑指定网关
.venv/bin/python crack_daily.py --only trae-work,codebuddy
```

## 运行方式

```bash
# 独立 CLI
python crack_copilot.py          # 提取并写入 secrets.json
python crack_codebuddy.py --force
python crack_qclaw.py --secrets secrets.json

# 服务启动时自动调用（crack 类 target 缺 key 时）
python server.py
```

成功退出码 0；失败退出码 1 + 引导文案。

## 环境检测（dashboard「重新破解」按钮）

`_crack_env_check(target)` 在 `/api/targets` 返回 `crackEnv: {available, reason}`，前端据此决定按钮状态：

- **copilot**：`shutil.which("gh")` 检测 gh CLI 是否在 PATH（企业/个人双模式：个人 token 从 `/root/.copilot/config.json` 破解）
- **codebuddy**：Windows 下探测 `%LOCALAPPDATA%`/`%APPDATA%`/home 下的 CodeBuddy 客户端目录
- **qclaw**：`QCLAW_API_KEY` 环境变量 或 `%APPDATA%\QClaw\app-store.json` 存在
- **trae-work**：检测本机 `storage.json` 是否存在（可跨机通过 `--export`/`--import-json` 导入认证，无需本机安装）
- **非 Windows 的 codebuddy/qclaw**：返回不可用，提示"仅支持 Windows，待后续补齐"

**不可用时按钮置灰**（`disabled` + `title` 提示原因），不阻止手动填写。

## QClaw 特殊性（自动解密）

`server.py` 启动时自动从 QClaw 本地存储解密 API Key：
- 读取 `%APPDATA%\QClaw\app-store.json` 的 `authGateway.providers.qclaw.apiKey.cipherText`
- 读取 `%APPDATA%\QClaw\Local State` 的 `os_crypt.encrypted_key`
- DPAPI 解密 AES 密钥 → AES-256-GCM 解密 cipherText → 得到 `sk-...` API Key

**QClaw 客户端只需登录过一次，代理就能自动拿到 Key，不需要 QClaw 持续运行**（除非用 `qclaw-local`）。
环境变量 `QCLAW_API_KEY` 优先级最高，可手动覆盖。

> **注意**：QClaw 即使无法本地破解（如非 Windows），只要通过环境变量或 dashboard 手动填写 key，仍可直连上游使用。

## 相关文档

- [QCLAW_19000_GATEWAY_REVERSE.md](../QCLAW_19000_GATEWAY_REVERSE.md) — 19000 网关逆向调研报告（HMAC 签名 / PID 反查 / 寄生注入）
