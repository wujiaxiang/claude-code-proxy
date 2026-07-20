' Watchdog launcher: 用 wscript 启动 pwsh.exe，避免计划任务闪黑框
' wscript.exe 本身无窗口，Run(..., 0, ...) 的 0 = vbHide 强制隐藏子进程窗口
' 计划任务应改为: wscript.exe watchdog_launcher.vbs

Set WshShell = CreateObject("WScript.Shell")
WshShell.CurrentDirectory = "c:\Users\Administrator\claude-code-proxy-main"
WshShell.Run "C:\Program Files\PowerShell\7\pwsh.exe -ExecutionPolicy Bypass -WindowStyle Hidden -File ""c:\Users\Administrator\claude-code-proxy-main\watchdog.ps1""", 0, False
