' 启动代理服务（隐藏窗口调用 bat）
' 由计划任务 \ClaudeCodeProxy 触发，完全独立于终端会话
Set WshShell = CreateObject("WScript.Shell")
WshShell.CurrentDirectory = "c:\Users\Administrator\claude-code-proxy-main"
WshShell.Run "c:\Users\Administrator\claude-code-proxy-main\start_proxy.bat", 0, False
