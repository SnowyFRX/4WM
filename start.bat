@echo off
chcp 65001 >nul
cd /d %~dp0
if not exist .venv (
    echo 首次运行，正在安装依赖...
    python -m venv .venv
    .venv\Scripts\python -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt
)
.venv\Scripts\python -X utf8 run.py %*
pause
