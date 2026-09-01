@echo off
chcp 65001 >nul
pushd "%~dp0"
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" evaluation.py
) else (
    py -3.12 evaluation.py
)
if errorlevel 1 (
    echo.
    echo 启动失败，请查看上方信息及 EVALUATION_README.md。
    pause
)
popd
