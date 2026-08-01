' 启动代理服务（隐藏窗口调用 bat）
' 由计划任务 \ClaudeCodeProxy 触发，完全独立于终端会话
' 本文件位于 scripts\windows\，项目根目录 = 本文件所在目录的上级
Option Explicit
Dim fso, WshShell, scriptDir, projectDir
Set fso = CreateObject("Scripting.FileSystemObject")
Set WshShell = CreateObject("WScript.Shell")
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
projectDir = fso.GetParentFolderName(scriptDir)
WshShell.CurrentDirectory = projectDir
WshShell.Run Chr(34) & scriptDir & "\start_proxy.bat" & Chr(34), 0, False
