# 破解工具说明

本项目通过破解工具从本地客户端提取 API key/token，写入 `secrets.json`（dashboard 可编辑，不入库）。
启动时自动调用；也可作为独立 CLI 运行。

## 工具清单

| 工具 | 目标 | 提取来源 | OS 支持 |
|------|------|---------|---------|
| `crack_copilot.py` | GitHub Copilot Enterprise | `gh auth token`（GitHub CLI） | ✅ 跨平台（需 gh CLI） |
| `crack_codebuddy.py` | CodeBuddy | 本机 CodeBuddy 客户端目录 | ✅ Windows / ❌ 其他 |
| `crack_qclaw.py` | QClaw | `%APPDATA%\QClaw\app-store.json`（DPAPI 解密） | ✅ Windows / ❌ 其他 |
| `crack_traework.py` | Trae Work | 预留（未实现） | ❌ |

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

- **copilot**：`shutil.which("gh")` 检测 gh CLI 是否在 PATH
- **codebuddy**：Windows 下探测 `%LOCALAPPDATA%`/`%APPDATA%`/home 下的 CodeBuddy 客户端目录
- **qclaw**：`QCLAW_API_KEY` 环境变量 或 `%APPDATA%\QClaw\app-store.json` 存在
- **trae-work**：永久不可用（预留）
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
