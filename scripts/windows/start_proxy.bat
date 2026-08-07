@echo off
REM 启动代理服务（隐藏窗口由 wscript 调用此 bat 实现）
REM 运行配置来自项目根 targets.json 顶层 server 段（私密凭据在 secrets.json）；.env 已废弃删除
REM 本文件位于 scripts\windows\，项目根目录 = 本文件所在目录的上级
cd /d "%~dp0..\.."
REM 关键：计划任务环境默认 GBK，Python print emoji 会崩
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
".venv\Scripts\python.exe" server.py > proxy.log 2>&1
