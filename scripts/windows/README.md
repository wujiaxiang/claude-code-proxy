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

## 已知坑

8 个 Windows 部署坑实录（GBK 崩溃 / VBS 重定向 / `cmd /c` 禁用 / watchdog COM 失败 / pwsh 闪框等）→ [docs/windows-deployment.md](../docs/windows-deployment.md)

## 开发约定（本目录规则）

- **改配置改项目根 `targets.json`（运行配置在顶层 `server` 段）或 `secrets.json`（私密凭据）**：VBS/BAT 是纯启动器，不设任何环境变量——`.env` 已废弃删除（备份 `.env.bak`），所有配置来自这两个文件。
- **计划任务 Action 路径必须指向本子目录**（`scripts/windows/` 移动过，路径会失效）。
- **动态路径定位**：脚本内用 `ScriptFullName` / `$PSScriptRoot` 推导项目根，移动项目目录后只需更新计划任务路径，脚本本身不改。

### ANTI-PATTERNS（本目录禁止）

1. **禁用 `cmd /c`**——VBS/PowerShell 里 Trae 沙盒会拦截（见 docs/windows-deployment.md 坑 3），直接调用 bat 而非 `cmd /c bat`。
2. **不要假设 PowerShell 路径默认存在**——调用前用 `Test-Path` 验证。
3. **不要在 VBS 里写中文/特殊字符重定向**——GBK 编码导致乱码/崩溃（必须 `set PYTHONIOENCODING=utf-8`）。
4. **不要硬编码项目绝对路径**——必须动态定位，否则项目移动后自启失效。
5. **勿用 `powershell.exe`（Windows PowerShell 5.x）**——本环境只有 pwsh 7（`"C:\Program Files\PowerShell\7\pwsh.exe"`），watchdog 用 `pwsh.exe` 启动。
