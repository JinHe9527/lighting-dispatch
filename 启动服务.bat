@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 团播派工服务（请勿关闭）
echo ============================================
echo    团播派工服务启动中
echo    本机地址：http://127.0.0.1:8848
echo    请保持此窗口运行
echo ============================================
:loop
"%~dp0python\python.exe" "%~dp0app\server.py"
echo.
echo [服务已停止，3 秒后自动重启。关闭本窗口可彻底停止。]
timeout /t 3 /nobreak >nul
goto loop
