@echo off
setlocal EnableExtensions EnableDelayedExpansion
set "ROOT=%~dp0"
set "KILLED=0"

if not exist "%ROOT%start-service.bat" goto missing_start
cd /d "%ROOT%" || goto invalid_root

title Wuyou Dispatch Restart
echo ============================================
echo Restarting Wuyou Dispatch v0802h
echo ============================================

for /f "tokens=5" %%P in ('netstat -ano ^| findstr ":8848" ^| findstr "LISTENING"') do (
  echo Stopping PID %%P
  taskkill /F /PID %%P >nul 2>&1
  set "KILLED=1"
)

if "!KILLED!"=="1" timeout /t 3 /nobreak >nul
start "Wuyou Dispatch" /D "%ROOT%" cmd.exe /d /c start-service.bat
timeout /t 5 /nobreak >nul

echo.
echo Health check. Expected version: v0802h
curl --silent --show-error --max-time 5 http://127.0.0.1:8848/api/health
echo.
pause
exit /b 0

:missing_start
echo Start script not found: %ROOT%start-service.bat
pause
exit /b 1

:invalid_root
echo Cannot open application directory: %ROOT%
pause
exit /b 1
