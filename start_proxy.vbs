' 启动代理服务（隐藏窗口，日志写入 proxy.log）
' 使用完整路径避免 PATH 问题
' 配置：QClaw provider + DeepSeek V4 Pro（big/medium/small 全部）
Set WshShell = CreateObject("WScript.Shell")
Set env = WshShell.Environment("Process")

' Provider 配置
env("PREFERRED_PROVIDER") = "qclaw"
env("BIG_MODEL") = "pool-deepseek-v4-pro"
env("MEDIUM_MODEL") = "pool-deepseek-v4-pro"
env("SMALL_MODEL") = "pool-deepseek-v4-pro"
env("BIG_REASONING") = "high"
env("MEDIUM_REASONING") = "low"
env("SMALL_REASONING") = "low"

WshShell.CurrentDirectory = "c:\Users\Administrator\claude-code-proxy-main"
WshShell.Run "c:\Users\Administrator\claude-code-proxy-main\.venv\Scripts\python.exe c:\Users\Administrator\claude-code-proxy-main\server.py > c:\Users\Administrator\claude-code-proxy-main\proxy.log 2>&1", 0, False
