@echo off
cd /d "%~dp0"

rem 如果控制台已经在跑，直接打开网页就行
netstat -ano | findstr ":8765" | findstr "LISTENING" >nul 2>&1
if not errorlevel 1 (
    start "" "http://127.0.0.1:8765/jwm/"
    exit
)

rem 否则先起控制台（无窗口），再打开网页
start "" "%~dp0.venv\Scripts\pythonw.exe" "%~dp0console.py"
timeout /t 3 >nul
start "" "http://127.0.0.1:8765/jwm/"
exit
