# AGENTS.md — scripts/windows

> Windows Server 部署自启脚本专项。根 AGENTS.md 已覆盖全局约定，本文件只记录本目录独有的操作规则。

## OVERVIEW

Windows 生产环境的代理自启 + watchdog 脚本（VBS/BAT/PS1 三层），全部**动态路径定位**（脚本自身位置推导项目根），不硬编码绝对路径。

## 文件清单

| 文件 | 作用 | 调用方 |
|------|------|--------|
| `start_proxy.vbs` | 隐藏窗口启动代理（纯启动器，不设环境变量） | 计划任务 `\ClaudeCodeProxy`（登录触发） |
| `start_proxy.bat` | 设 `PYTHONIOENCODING=utf-8` + 运行 `server.py`（日志重定向 `proxy.log`） | `start_proxy.vbs` |
| `watchdog_launcher.vbs` | `wscript` 启动 `pwsh.exe`（避免计划任务闪黑框） | 计划任务 `\ClaudeCodeProxyWatchdog`（每 2 分钟） |
| `watchdog.ps1` | .NET `TcpClient` 检测 8082 端口，挂掉自动重启 | `watchdog_launcher.vbs` |
| `README.md` | 完整启动链路 + 计划任务配置说明 | 人工 |

## 启动链路

```
登录触发 → wscript.exe start_proxy.vbs → start_proxy.bat → python server.py
                                                                    ↑
每2分钟 → wscript.exe watchdog_launcher.vbs → pwsh.exe watchdog.ps1
                                              ├─ 8082 通 → 静默退出
                                              └─ 断 → wscript.exe start_proxy.vbs（重启）
```

## CONVENTIONS

- **改配置只改 `.env`**：VBS/BAT 是纯启动器，不设任何环境变量——所有配置来自项目根 `.env`。
- **计划任务 Action 路径必须指向本子目录**（`scripts/windows/` 移动过，路径会失效）。
- 脚本内用 `ScriptFullName` / `$PSScriptRoot` 动态定位项目根，移动项目目录后只需更新计划任务路径。

## ANTI-PATTERNS（本目录禁止）

1. **VBS/PowerShell 中禁用 `cmd /c`**——Trae 沙盒会拦截（见根文档 docs/windows-deployment.md 坑 3）。
2. **不要假设 PowerShell 路径默认存在**——用 `Test-Path` 验证后再调用。
3. **不要在 VBS 里写中文/特殊字符重定向**——GBK 编码会导致乱码/崩溃（必须 `PYTHONIOENCODING=utf-8`）。
4. **不要硬编码项目绝对路径**——必须动态定位，否则项目移动后自启失效。
5. **勿用 `powershell.exe`（Windows PowerShell 5.x）**——本环境只有 pwsh 7，watchdog 用 `pwsh.exe` 启动。

## NOTES

- Windows 机器无 `powershell.exe`，只有 pwsh 7：`"C:\Program Files\PowerShell\7\pwsh.exe"`。
- 8 个 Windows 部署坑完整记录在根文档 `docs/windows-deployment.md`（GBK 崩溃 / VBS 重定向 / cmd /c 禁用 / watchdog COM 失败 / pwsh 闪框等）。
