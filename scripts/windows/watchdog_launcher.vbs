' Watchdog launcher: 用 wscript 启动 pwsh.exe，避免计划任务闪黑框
' wscript.exe 本身无窗口，Run(..., 0, ...) 的 0 = vbHide 强制隐藏子进程窗口
' 计划任务应改为: wscript.exe watchdog_launcher.vbs

Set WshShell = CreateObject("WScript.Shell")
WshShell.CurrentDirectory = "c:\Users\Administrator\claude-code-proxy-main"

' 用 Chr(34) 表示引号，避免 VBS 字符串中嵌套引号的转义问题
' 路径含空格时必须用引号包裹，否则 CreateProcess 会把空格解析为参数分隔符
Dim pwshPath, scriptPath, cmd
pwshPath = Chr(34) & "C:\Program Files\PowerShell\7\pwsh.exe" & Chr(34)
scriptPath = Chr(34) & "c:\Users\Administrator\claude-code-proxy-main\watchdog.ps1" & Chr(34)
cmd = pwshPath & " -ExecutionPolicy Bypass -WindowStyle Hidden -File " & scriptPath

WshShell.Run cmd, 0, False
