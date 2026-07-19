' 启动代理服务（隐藏窗口，日志写入 proxy.log）
' 配置由 .env 文件提供（python-dotenv 自动加载）
Set WshShell = CreateObject("WScript.Shell")
WshShell.CurrentDirectory = "c:\Users\Administrator\claude-code-proxy-main"
WshShell.Run "c:\Users\Administrator\claude-code-proxy-main\.venv\Scripts\python.exe c:\Users\Administrator\claude-code-proxy-main\server.py > c:\Users\Administrator\claude-code-proxy-main\proxy.log 2>&1", 0, False
