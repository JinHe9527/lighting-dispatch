@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 团播派工服务重启
echo ============================================
echo    团播派工 v0721a 重启
echo ============================================
set KILLED=0
for /f "tokens=5" %%a in ('netstat -ano ^| findstr :8848 ^| findstr LISTENING') do (
  echo 停止旧服务进程 PID %%a
  taskkill /F /PID %%a >nul 2>&1
  set KILLED=1
)
if "%KILLED%"=="1" timeout /t 5 /nobreak >nul
netstat -ano | findstr :8848 | findstr LISTENING >nul 2>&1
if errorlevel 1 (
  echo 服务未运行，正在启动
  start "" "%~dp0..\启动服务.bat"
  timeout /t 5 /nobreak >nul
)
echo.
echo 健康检查（version 应为 v0721a）：
curl -s --max-time 5 http://127.0.0.1:8848/api/health
echo.
echo.
echo 若返回 ok=true、db=ok、version=v0721a，重启成功。
pause
