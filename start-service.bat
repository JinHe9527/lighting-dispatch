@echo off
setlocal EnableExtensions
set "ROOT=%~dp0"
set "LOG=%ROOT%app\service-console.log"

if not exist "%ROOT%python\python.exe" goto missing_python
if not exist "%ROOT%app\server.py" goto missing_server
cd /d "%ROOT%" || goto invalid_root

title Wuyou Dispatch Service
echo ============================================
echo Wuyou Dispatch is starting
echo Local address: http://127.0.0.1:8848
echo Keep this window open
echo ============================================

:loop
echo [%date% %time%] Starting service >> "%LOG%"
"%ROOT%python\python.exe" -u "%ROOT%app\server.py" >> "%LOG%" 2>&1
set "SERVICE_EXIT=%ERRORLEVEL%"
echo.
echo Service stopped with exit code %SERVICE_EXIT%.
echo Diagnostic log: %LOG%
type "%LOG%"
echo.
echo Restarting in 5 seconds.
timeout /t 5 /nobreak >nul
goto loop

:missing_python
echo Python runtime not found: %ROOT%python\python.exe
pause
exit /b 1

:missing_server
echo Server program not found: %ROOT%app\server.py
pause
exit /b 1

:invalid_root
echo Cannot open application directory: %ROOT%
pause
exit /b 1
