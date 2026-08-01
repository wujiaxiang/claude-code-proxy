# Windows 部署指南（启动脚本 + 踩坑实录）

> 本文档从主 README 拆出，记录 Windows Server 上通过计划任务 + VBS + BAT 三层架构自启代理的完整方案与踩坑实录。
> 启动脚本位于 [`scripts/windows/`](../scripts/windows/)，内部使用动态路径定位（不硬编码项目目录）。

## 目录结构

```
项目根/
├── server.py              ← 代理主程序
├── .venv/                 ← 项目虚拟环境
├── .env                   ← 全局配置（DEBUG/LOG_*，不含密钥）
├── targets.json           ← 多端口架构配置（端口/供应商/分类/handler/模型）
├── secrets.json           ← 私密 token（dashboard 可编辑，不入库）
└── scripts/windows/       ← Windows 启动脚本
    ├── start_proxy.vbs          ← 主启动器（计划任务登录触发）
    ├── start_proxy.bat          ← 设编码 + 跑 python（日志重定向）
    ├── watchdog_launcher.vbs    ← watchdog 无窗口启动器（防闪黑框）
    ├── watchdog.ps1             ← 端口检测 + 自动重启
    └── README.md                ← 脚本使用说明
```

> **脚本定位原理**：所有脚本通过自身位置（`WScript.ScriptFullName` / `$PSScriptRoot`）推导项目根（`scripts/windows/` 上两级），
> 因此项目目录迁移后**只需更新计划任务路径**，脚本本身无需修改。

## 启动架构

```
计划任务 \ClaudeCodeProxy          计划任务 \ClaudeCodeProxyWatchdog
触发：用户登录                       触发：每 2 分钟
        │                                    │
        ▼                                    ▼
wscript.exe scripts\windows\        wscript.exe scripts\windows\
           start_proxy.vbs                 watchdog_launcher.vbs
        │                                    │  (vbHide 包装，避免 pwsh 闪框)
        ▼                                    ▼
start_proxy.bat                    pwsh.exe -File watchdog.ps1
  set PYTHONIOENCODING=utf-8               │
  set PYTHONUTF8=1                  .NET TcpClient 检测 8082 端口
  python server.py > proxy.log             │
                                  ┌───────┴───────┐
                                  端口通         端口不通
                                  退出           ↓
                                            wscript.exe start_proxy.vbs
                                            （触发重启）
```

**关键文件**：
- `scripts/windows/start_proxy.vbs` — VBS 启动器，隐藏窗口调用 bat（动态定位项目根）
- `scripts/windows/start_proxy.bat` — 设 `PYTHONIOENCODING=utf-8`，启动 python 并重定向日志
- `scripts/windows/watchdog.ps1` — PowerShell watchdog，端口检测 + 自动重启
- `scripts/windows/watchdog_launcher.vbs` — watchdog 的无窗口启动器，避免 pwsh 闪黑框

**计划任务创建命令**（参考，XML 文件部署时生成，不入库）：
```powershell
# 主任务（登录触发）—— Action 指向 scripts\windows\start_proxy.vbs
schtasks /create /xml proxy_task.xml /tn \ClaudeCodeProxy /f

# Watchdog 任务（每 2 分钟触发）—— Action 指向 scripts\windows\watchdog_launcher.vbs
schtasks /create /xml wd_task.xml /tn \ClaudeCodeProxyWatchdog /f

# 手动触发 watchdog 测试
schtasks /run /tn \ClaudeCodeProxyWatchdog
```

---

## 坑 1：GBK 编码导致 Python 启动崩溃（最严重，根因）

**症状**：代理在 Trae IDE 终端能跑，但计划任务触发后秒挂，`proxy.log` 完全为空。

**根因**：`server.py` 启动时 `print(f"\U0001f511 QClaw API Key decrypted: ...")` 输出 emoji 🔑。Windows 计划任务环境的 codepage 是 GBK（936），Python 默认按 stdout 编码 print，遇到 emoji 立即抛 `UnicodeEncodeError: 'gbk' codec can't encode character '\U0001f511'`，进程在绑定 8082 端口之前就死了。

**修复**：在 `start_proxy.bat` 中显式设：
```bat
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
".venv\Scripts\python.exe" server.py > proxy.log 2>&1
```

**排查教训**：日志为空不代表没启动过——是 Python 在写日志之前就崩了。先 `python server.py` 同步跑一次看 stderr 才能定位。

---

## 坑 2：VBS 的 `WshShell.Run` 不支持 shell 重定向

**症状**：`WshShell.Run "python.exe server.py > proxy.log 2>&1", 0, False` 在 VBS 中不生效——`>` 不会被解释为重定向，proxy.log 永远是空的。

**根因**：`WshShell.Run` 不是 `cmd.exe`，不解析 `>` / `>>` / `2>&1` 等 shell 操作符。

**修复**：VBS 只负责隐藏窗口启动，重定向交给 `.bat` 文件处理。VBS → BAT → Python 三层架构。

---

## 坑 3：`cmd /c` 在 Trae IDE 沙盒中被禁用

**症状**：在 VBS 中用 `WshShell.Run "cmd /c ..."` 启动会失败，Trae IDE 的安全沙盒拦截了 `cmd /c` 调用。

**报错**：`invalid command: The use of 'cmd /c' (or 'cmd.exe /c') is blocked on Windows for safety`

**修复**：不要在 VBS / PowerShell 中用 `cmd /c`，直接 `WshShell.Run "path\to.bat", 0, False` 调用 bat 文件即可。

---

## 坑 4：watchdog.vbs 的 COM 对象端口检测全部静默失败

**症状**：用 `MSWinsock.Winsock` / `MSXML2.XMLHTTP` / `System.Net.Sockets.TcpClient` 三种 COM 对象在 VBS 中检测端口，全部静默失败——`watchdog.log` 不写入、代理不重启、`WScript.Quit` 异常。

**根因**：
- `MSWinsock.Winsock` 在新版 Windows 默认未注册
- `System.Net.Sockets.TcpClient` 是 .NET 类，不是 COM 组件，VBS 通过 COM 调用行为不稳定
- `On Error Resume Next` 吞掉了所有错误，看不出哪一步挂了

**修复**：彻底放弃 VBS 做 watchdog，改用 PowerShell 脚本 `watchdog.ps1`。PowerShell 原生支持 .NET，`New-Object System.Net.Sockets.TcpClient` + `BeginConnect` + `WaitOne(2000)` 异步超时检测，稳定可靠。

---

## 坑 5：系统装的是 PowerShell 7（`pwsh.exe`），没有 `powershell.exe`

**症状**：计划任务调用 `powershell.exe -File watchdog.ps1` 失败，报 "not recognized"。手动 `Get-Command powershell` 也找不到。

**根因**：Windows Server 安装了 PowerShell 7+ 作为默认 PowerShell，传统 `C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe`（Windows PowerShell 5.1）反而不存在。

**修复**：计划任务的 Action 必须用：
```
Command:    C:\Program Files\PowerShell\7\pwsh.exe
Arguments:  -ExecutionPolicy Bypass -WindowStyle Hidden -File "...\scripts\windows\watchdog.ps1"
```

**排查技巧**：用 `Test-Path` 检查两个路径都存在与否，不要假设哪个是默认。

---

## 坑 6：`schtasks /create` 引号转义地狱，改用 XML

**症状**：`schtasks /create /tr "wscript.exe ..."` 在 PowerShell 中引号嵌套转义失败，要么 bat 路径被截断，要么参数丢失。

**修复**：放弃命令行参数方式，改用 `schtasks /create /xml task.xml /tn \TaskName /f`。XML 文件可以精确控制 `UserId`、`LogonType`、`Triggers`、`Actions`，且不依赖 shell 转义规则。

**坑中坑**：XML 中 `LogonType` 的合法值是 `InteractiveToken`，不是 `Interactive`（后者会报 "incorrectly formatted or out of range"）。`UserId` 必须填当前用户的完整 SID，可用 `[System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value` 获取。

---

## 坑 7：PATH 污染导致 `Get-NetTCPConnection` 等 cmdlet 不可用

**症状**：在 PowerShell 中执行 `Get-NetTCPConnection -LocalPort 8082` 报 "not recognized"。

**根因**：Trae IDE 的 ripgrep、QClaw 的工具链等会把 `...\@vscode\ripgrep\bin` 或其他路径前缀加到 PATH，覆盖了 `C:\Windows\System32\WindowsPowerShell\Modules`，导致 PowerShell 模块加载失败。

**修复**：调用 Windows 系统命令前先清理 PATH：
```powershell
$env:Path = "C:\Windows\System32;C:\Windows"
```

或者直接用 `netstat -ano | findstr :8082`，不依赖任何 PowerShell 模块。

---

## 坑 8：PowerShell 7 (`pwsh.exe`) 在计划任务中闪黑框

**症状**：每 2 分钟触发 watchdog 时，桌面会闪过一个黑色 console 窗口，瞬间消失，用户能感知到。即使 `-WindowStyle Hidden` 参数也无效。

**根因**：PowerShell 7 (`pwsh.exe`) 启动时会创建真实的 console 窗口（与 PowerShell 5.1 不同），即使加了 `-WindowStyle Hidden`，console 已经被分配出来再隐藏，肉眼可见闪现。计划任务的 `wscript.exe` 同样如此——只要 Action 的 `Command` 指向带 console 的进程，都会闪。

**修复**：用一个无窗口的 wscript.exe 作为外层包装，再通过 `WshShell.Run(cmd, 0, False)` 的 `0`（vbHide）启动 pwsh.exe。`WshShell.Run` 的第二参数 `0` 在 CreateProcess 时直接传 `STARTUPINFO.wShowWindow = SW_HIDE`，从根源抑制 console 窗口创建。当前实现即 `watchdog_launcher.vbs`（内部用 `Chr(34)` 处理引号、动态定位项目根）。

**关键点**：
- `Chr(34)` 在 VBS 中表示双引号，比字符串内嵌套转义引号更可靠
- 路径含空格时必须用引号包裹，否则 CreateProcess 会把空格解析为参数分隔符
- `-WindowStyle Hidden` 只是 pwsh 内部隐藏窗口，console 已经被分配；必须由父进程通过 `WshShell.Run(..., 0, ...)` 在 CreateProcess 阶段就传 `SW_HIDE`，才能彻底避免闪框
