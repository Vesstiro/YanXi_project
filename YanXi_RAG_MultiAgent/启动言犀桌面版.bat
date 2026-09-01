@echo off
chcp 65001 >nul
pushd "%~dp0"
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" desktop.py
) else (
    py -3.12 desktop.py
)
if errorlevel 1 (
    echo.
    echo 启动失败，请查看上方信息及 DESKTOP_README.md。
    pause
)
popd
