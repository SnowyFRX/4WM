@echo off
chcp 65001 >nul
setlocal
set "JWM_EMULATOR=%LOCALAPPDATA%\Android\Sdk\emulator\emulator.exe"
set "JWM_EMULATOR_LOG=%~dp0logs\emulator.log"
if not exist "%JWM_EMULATOR%" (
    echo 找不到 Android 模拟器，请检查 Android SDK 是否已安装。
    pause
    exit /b 1
)
if not exist "%~dp0logs" mkdir "%~dp0logs"
echo 正在打开安卓模拟器 jwm。首次看到 Android 桌面前请等待约半分钟。
echo 启动记录保存在 "%JWM_EMULATOR_LOG%"。
"%JWM_EMULATOR%" -avd jwm -gpu host -no-snapshot-load -no-boot-anim -no-audio -netdelay none -netspeed full >"%JWM_EMULATOR_LOG%" 2>&1
if errorlevel 1 (
    echo 模拟器启动失败，请查看上面的启动记录。
    pause
    exit /b 1
)
