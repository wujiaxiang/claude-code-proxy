' Watchdog launcher: 用 wscript 启动 pwsh.exe，避免计划任务闪黑框
' wscript.exe 本身无窗口，Run(..., 0, ...) 的 0 = vbHide 强制隐藏子进程窗口
' 计划任务应改为: wscript.exe scripts\windows\watchdog_launcher.vbs
' 本文件位于 scripts\windows\，项目根目录 = 本文件所在目录的上级
Option Explicit
Dim fso, WshShell, scriptDir, projectDir, pwshPath, scriptPath, cmd
Set fso = CreateObject("Scripting.FileSystemObject")
Set WshShell = CreateObject("WScript.Shell")
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
projectDir = fso.GetParentFolderName(scriptDir)
WshShell.CurrentDirectory = projectDir

' 用 Chr(34) 表示引号，避免 VBS 字符串中嵌套引号的转义问题
' 路径含空格时必须用引号包裹，否则 CreateProcess 会把空格解析为参数分隔符
pwshPath = Chr(34) & "C:\Program Files\PowerShell\7\pwsh.exe" & Chr(34)
scriptPath = Chr(34) & scriptDir & "\watchdog.ps1" & Chr(34)
cmd = pwshPath & " -ExecutionPolicy Bypass -WindowStyle Hidden -File " & scriptPath

WshShell.Run cmd, 0, False
