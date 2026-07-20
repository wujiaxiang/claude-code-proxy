@echo off
REM 启动代理服务（隐藏窗口由 wscript 调用此 bat 实现）
REM 配置由 .env 文件提供（python-dotenv 自动加载）
cd /d "c:\Users\Administrator\claude-code-proxy-main"
REM 关键：计划任务环境默认 GBK，Python print emoji 会崩
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
".venv\Scripts\python.exe" server.py > proxy.log 2>&1
