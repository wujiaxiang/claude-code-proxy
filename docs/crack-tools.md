# 破解公共层索引

破解网关（crack 类 target）的**公共机制**集中说明：工具模块、凭据字段、状态查询、每日任务、环境检测。
各网关的提取细节 / 接口逆向见对应专属文档。

## 工具清单（索引）

| 模块 | 一行职责 | 网关文档 |
|------|---------|---------|
| `crack_copilot.py` | 提取 Copilot token（企业 PAT / 个人 OAuth，跨平台需 gh CLI） | 🔗 详见 [copilot.md](copilot.md) |
| `crack_codebuddy.py` | 从 CodeBuddy 客户端目录提取 token（仅 Windows） | 🔗 详见 [codebuddy.md](codebuddy.md) |
| `crack_qclaw.py` | 从 `%APPDATA%\QClaw` DPAPI 解密 API Key（仅 Windows） | 🔗 详见 [qclaw.md](qclaw.md) |
| `crack_traework.py` | 从 TRAE SOLO CN `storage.json` 提取认证（tc 加密） | 🔗 详见 [trae-work.md](trae-work.md) |
| `crack_daily.py` | 统一每日任务调度器（见下） | — |
| `crack_*_q.py` | 各网关额度/签到查询（见下） | 🔗 详见各网关文档 |

提取到的 token 写入 `secrets.json`（dashboard 可编辑，不入库）；服务启动时自动调用，也可独立 CLI 运行（成功退出码 0 / 失败 1）。

## secrets.json 字段总表

按 `crack_common.CREDENTIAL_SCHEMAS` 整理（dashboard 凭据弹窗按此渲染，`PUT /api/secrets/{label}/bulk` 校验）：

| label（target） | 依赖字段 | 必填 | 说明 |
|------|---------|------|------|
| copilot-enterprise (8082) | `copilot_token` | ✅ | 企业 PAT（`github_pat_` 前缀，需 Copilot 权限） |
| copilot (8083) | `copilot_personal_token` | ✅ | 个人 OAuth token（`gho_` 前缀） |
| codebuddy (8084) | `codebuddy_token` | ✅ | 可选 `codebuddy_refresh_token` / `codebuddy_uid`；昵称 `codebuddy_nickname` 只读 |
| qclaw (8085) | `qclaw_api_key` | ✅ | 仅此字段即可正常代理；其余为积分查询增强：`qclaw_openclaw_token` / `qclaw_guid` / `qclaw_user_id` / `qclaw_device_token` / `qclaw_login_key`，缺省时状态区降级提示 |
| trae-work (8086) | `trae_work_token` + `trae_work_refresh_token` | ✅✅ | 可选 `trae_work_user_id`；`trae_work_bound_device_id` 等其余字段见各网关文档 |

> **copilot 双模式**（8082 企业 / 8083 个人，token 完全隔离不可混用）→ 🔗 详见 [copilot.md](copilot.md)

## 状态查询统一结构

dashboard 通过 `GET /api/crack/{label}/status` 统一展示额度/签到，由 `crack_common.CRACK_STATUS_HANDLERS` 注册表分发：

```python
{"quota": [...], "checkin": {...}, "refresh": {...}, "extra": {...}}
# quota 条目: {"name", "limit", "used", "expireAt"}；装配时补充 displayName / account / capabilities / lastDailyRun
```

- **handler 签名**：标准 `handler(token, refresh_token)`（trae-work/codebuddy/copilot）；多字段 `handler(secrets)`（qclaw，`HANDLER_TAKES_SECRETS` 标记）
- 各网关额度查询接口细节（quota_snapshots / jprx 4110/4075/4222 / get-user-resource 等）→ 🔗 [copilot.md](copilot.md) / [codebuddy.md](codebuddy.md) / [qclaw.md](qclaw.md) / [trae-work.md](trae-work.md)

## 统一每日任务（单一 cron）

```bash
0 3 * * * /root/shared-workspace/claude-code-proxy/scripts/cron/crack_daily.sh
```

- `DAILY_HANDLERS` 注册表，每网关注册 `daily(secrets, out, secrets_path)`：trae-work 签到+刷新、codebuddy 成长任务领取、qclaw/copilot 仅校验 token
- **无 key 的网关自动跳过**（按 secrets.json 判断，不算失败）；**勿新增其他 cron**——这是唯一每日调度入口
- 日志 `logs/crack_daily.log`（仓库内，非 `/tmp`——容器重启清空且代理 `PrivateTmp=true` 读不到）；执行完写 `.cache/crack_daily_last_run` 时间戳（dashboard 展示"最后定时刷新"）

**失败可观测性**：任一网关最终失败 → 退出码 1 → crontab 的 `||` 钩子追写 `logs/crack_daily.alert`。单网关失败自动重试一次（各 handler 均幂等：签到类先查状态再领取）。外层 `timeout 300` 防上游卡死拖垮整个任务（超时退出码 124，同样触发告警）。

**扩展新网关**：写 `daily_xxx(secrets, out, secrets_path=None) -> dict` → 在 `DAILY_HANDLERS` 加一行 → 完事（签名已统一，`main()` 无分支无需改动）。handler 内部吞异常时把错误写进 result 子 dict（如 `{"error": "..."}`），调度器据此触发重试与非零退出码。

> 完整约定见 `crack_daily.py` docstring（单一事实源）。

```bash
.venv/bin/python crack_daily.py --secrets secrets.json        # 全部
.venv/bin/python crack_daily.py --only trae-work,codebuddy    # 只跑指定网关
.venv/bin/python crack_daily.py --only copilot --retry-delay 0  # 调试：跳过重试等待
```

## 环境检测（crackEnv）

`_crack_env_check(target)` 在 `/api/targets` 返回 `crackEnv: {available, reason}`，dashboard「重新破解」按钮据此置灰/启用：

- **copilot**：`shutil.which("gh")` 检测 gh CLI 是否在 PATH（个人 token 从本机 `/root/.copilot/config.json` 破解）
- **codebuddy**：Windows 下探测 `%LOCALAPPDATA%` / `%APPDATA%` / home 下的客户端目录
- **qclaw**：`QCLAW_API_KEY` 环境变量或 `%APPDATA%\QClaw\app-store.json` 存在（自动解密细节 → 🔗 [qclaw.md](qclaw.md)）
- **trae-work**：检测本机 `storage.json`（可跨机 `--export` / `--import-json` 导入认证，无需本机安装）
- 非 Windows 的 codebuddy/qclaw 返回不可用（"仅支持 Windows"）；不可用时按钮置灰但不阻止手动填写

## 相关文档

- 🔗 [copilot.md](copilot.md) — Copilot 双模式 / token 提取 / 额度查询
- 🔗 [codebuddy.md](codebuddy.md) — CodeBuddy 破解 / 成长任务 / 额度查询
- 🔗 [qclaw.md](qclaw.md) — QClaw 自动解密 / API Key / 积分查询
- 🔗 [trae-work.md](trae-work.md) — Trae Work tc 加密 / 签到 / 额度 / 续期
- [QCLAW_19000_GATEWAY_REVERSE.md](../QCLAW_19000_GATEWAY_REVERSE.md) — 19000 网关逆向调研报告（HMAC 签名 / PID 反查 / 寄生注入）
