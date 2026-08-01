# Windows 启动脚本

本目录包含 Windows Server 部署用的代理启动 + watchdog 脚本，全部使用**动态路径定位**（基于脚本自身位置推导项目根），不依赖硬编码绝对路径。

## 文件说明

| 文件 | 作用 | 由谁调用 |
|------|------|---------|
| `start_proxy.vbs` | 隐藏窗口启动代理（纯启动器，不设环境变量） | 计划任务 `\ClaudeCodeProxy`（登录触发） |
| `start_proxy.bat` | 设 `PYTHONIOENCODING=utf-8` 并运行 `server.py`（日志重定向 `proxy.log`） | `start_proxy.vbs` |
| `watchdog_launcher.vbs` | 用 `wscript` 启动 `pwsh.exe`（避免计划任务闪黑框） | 计划任务 `\ClaudeCodeProxyWatchdog`（每 2 分钟） |
| `watchdog.ps1` | .NET `TcpClient` 检测 8082 端口，挂掉自动重启 | `watchdog_launcher.vbs` |

## 启动链路

```
登录触发 → wscript.exe start_proxy.vbs → start_proxy.bat → python server.py
                                                                    ↑
每2分钟 → wscript.exe watchdog_launcher.vbs → pwsh.exe watchdog.ps1
                                              │
                                         检测 8082 端口
                                         ├─ 通 → 静默退出
                                         └─ 断 → wscript.exe start_proxy.vbs（重启）
```

## 计划任务配置（必须指向本子目录）

脚本已移动到 `scripts/windows/`，**计划任务 Action 的路径必须同步更新**：

```
\ClaudeCodeProxy            → wscript.exe "<项目根>\scripts\windows\start_proxy.vbs"
\ClaudeCodeProxyWatchdog    → wscript.exe "<项目根>\scripts\windows\watchdog_launcher.vbs"
```

> 脚本内部用 `ScriptFullName` / `$PSScriptRoot` 动态定位项目根（`scripts/windows/` 上两级），
> 因此移动整个项目目录后**只需更新计划任务路径**，脚本本身无需修改。

## 手动启动

```powershell
Set-Location "<项目根>"
wscript.exe .\scripts\windows\start_proxy.vbs
```

## 已知坑（详见 docs/windows-deployment.md）

1. **GBK 崩溃**：计划任务环境 codepage 是 GBK，必须 `set PYTHONIOENCODING=utf-8`（已在 bat 处理）
2. **`cmd /c` 被禁**：Trae IDE 沙盒拦截 `cmd /c`，直接调用 bat 而非 `cmd /c bat`
3. **PowerShell 7**：系统只有 `pwsh.exe`，没有 `powershell.exe`
4. **闪黑框**：pwsh 由 `wscript.exe` + `Run(..., 0, ...)` 在 CreateProcess 阶段隐藏窗口
5. **watchdog 用 VBS 会静默失败**：COM 对象检测端口不可靠，必须用 PowerShell `.NET TcpClient`
