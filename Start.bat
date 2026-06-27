@echo off
REM US Quant Live - English alias for the launcher

cd /d "%~dp0"

if not exist "venv\Scripts\python.exe" (
    echo [ERROR] venv not found at %~dp0venv
    pause
    exit /b 1
)

start "" "venv\Scripts\pythonw.exe" "tools\tray_launcher.py"
exit
