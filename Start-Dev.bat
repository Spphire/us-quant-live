@echo off
setlocal

cd /d "%~dp0"
set "PROJECT_ROOT=%CD%"
set "OUTPUT_ROOT=%PROJECT_ROOT%\artifacts\dev_candidate_scheduler"
set "LOG_DIR=%OUTPUT_ROOT%\daemon"
set "STARTUP_LOG=%LOG_DIR%\startup.bat.log"

if not exist "%LOG_DIR%" mkdir "%LOG_DIR%" >nul 2>&1
echo [%date% %time%] Start-Dev.bat invoked from %PROJECT_ROOT%>> "%STARTUP_LOG%"

if defined US_QUANT_LIVE_SHARED_PYTHON (
    set "PYTHON_EXE=%US_QUANT_LIVE_SHARED_PYTHON%"
) else if exist "%PROJECT_ROOT%\venv\Scripts\python.exe" (
    set "PYTHON_EXE=%PROJECT_ROOT%\venv\Scripts\python.exe"
) else (
    set "PYTHON_EXE=W:\Quat\us-quant-live\venv\Scripts\python.exe"
)

if not exist "%PYTHON_EXE%" (
    echo [ERROR] Python runtime not found at %PYTHON_EXE%>> "%STARTUP_LOG%"
    exit /b 1
)

for %%I in ("%PYTHON_EXE%") do set "PYTHONW_EXE=%%~dpIpythonw.exe"

echo [%date% %time%] restart mode: stopping existing Dev tray/scheduler/dashboard first>> "%STARTUP_LOG%"
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$root=(Resolve-Path -LiteralPath '%PROJECT_ROOT%').Path; " ^
  "$needles=@((Join-Path $root 'tools\tray_launcher.py'),(Join-Path $root 'tools\daily_alpaca_scheduler.py'),(Join-Path $root 'tools\dashboard_server.py'),(Join-Path $root 'tools\watch_daily_alpaca_scheduler.ps1')); " ^
  "$executorNeedle=Join-Path $root 'src\alpaca_executor.py'; " ^
  "$self=$PID; " ^
  "$activeExecutors=@(Get-CimInstance Win32_Process | Where-Object { if([int]$_.ProcessId -eq [int]$self){return $false}; $cmd=[string]$_.CommandLine; if(-not $cmd){return $false}; $cmd.IndexOf($executorNeedle,[StringComparison]::OrdinalIgnoreCase) -ge 0 }); " ^
  "if($activeExecutors.Count -gt 0){ Write-Output ('restart aborted: active Dev decision/execution pid(s)='+ (($activeExecutors | ForEach-Object { [string]$_.ProcessId }) -join ',')); exit 41 }; " ^
  "function Get-ProjectTargets { @(Get-CimInstance Win32_Process | Where-Object { if([int]$_.ProcessId -eq [int]$self){return $false}; $cmd=[string]$_.CommandLine; if(-not $cmd){return $false}; (($needles | Where-Object { $cmd.IndexOf($_,[StringComparison]::OrdinalIgnoreCase) -ge 0 }).Count -gt 0) }) }; " ^
  "for($i=0; $i -lt 5; $i++){ $procs=@(Get-ProjectTargets); if($procs.Count -eq 0){ break }; foreach($p in $procs){ try { Write-Output ('stopping pid='+$p.ProcessId+' '+$p.Name); & taskkill.exe /F /T /PID ([string]$p.ProcessId) | Out-Null } catch { Write-Output ('taskkill failed pid='+$p.ProcessId+' '+$_.Exception.Message) }; try { Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue } catch {} }; Start-Sleep -Milliseconds 800 }; " ^
  "$remaining=@(Get-ProjectTargets); if($remaining.Count -gt 0){ Write-Output ('warning: remaining Dev processes after restart cleanup: '+(($remaining | ForEach-Object { [string]$_.ProcessId }) -join ',')) }; " ^
  "Remove-Item -LiteralPath (Join-Path '%OUTPUT_ROOT%' 'daemon\tray_launcher.pid') -Force -ErrorAction SilentlyContinue; " ^
  "Remove-Item -LiteralPath (Join-Path '%OUTPUT_ROOT%' 'daemon\scheduler.pid') -Force -ErrorAction SilentlyContinue; " ^
  "Remove-Item -LiteralPath (Join-Path '%OUTPUT_ROOT%' 'watchdog\watchdog.pid') -Force -ErrorAction SilentlyContinue; " ^
  "exit 0" ^
  >> "%STARTUP_LOG%" 2>>&1

if errorlevel 1 (
    echo [%date% %time%] restart aborted because a Dev decision or execution task is active>> "%STARTUP_LOG%"
    exit /b 1
)

set "US_QUANT_LIVE_APP_NAME=US Quant Live Dev"
set "US_QUANT_LIVE_APP_ID=us-quant-live-dev"
set "US_QUANT_LIVE_MUTEX_NAME=Global\us-quant-live-tray-launcher-dev-candidate"
set "US_QUANT_LIVE_ICON_PATH=%PROJECT_ROOT%\tools\tray_icon_dev.ico"
set "US_QUANT_LIVE_PYTHON_EXE=%PYTHON_EXE%"
set "US_QUANT_LIVE_ARTIFACTS_ROOT=%OUTPUT_ROOT%"
set "US_QUANT_LIVE_DASHBOARD_PORT=18077"
set "US_QUANT_LIVE_DASHBOARD_URL=http://127.0.0.1:18077"
set "US_QUANT_LIVE_ACCOUNTS_JSON_PATH=%PROJECT_ROOT%\configs\alpaca_acounts\alpaca_accounts.local.json"
set "US_QUANT_LIVE_ACCOUNT_NAME=ALPACA_DEV_CANDIDATE"
set "US_QUANT_LIVE_LONG_BRIDGE_CONFIG_PATH=%PROJECT_ROOT%\configs\longbridge.local.json"
set "US_QUANT_LIVE_EXECUTION_INTRADAY_BAR_PROVIDER=longbridge"
set "US_QUANT_LIVE_OUTPUT_ROOT=%OUTPUT_ROOT%"
set "US_QUANT_LIVE_STATE_PATH=%OUTPUT_ROOT%\state.json"
set "US_QUANT_LIVE_PREPARE_TIME_CN=12:30"
set "US_QUANT_LIVE_DECISION_TIME_CN=21:00"
set "US_QUANT_LIVE_EXECUTE_TIME_CN=22:00"
set "US_QUANT_LIVE_TARGET_NY_TIME=10:00"
set "US_QUANT_LIVE_TRADING_DAY_SOURCE=alpaca_calendar"
set "US_QUANT_LIVE_AUTOSTART_TASK_NAME=US Quant Live Dev Tray"
set "US_QUANT_LIVE_START_SCRIPT_NAME=Start-Dev.bat"

if exist "%PYTHONW_EXE%" (
    echo [%date% %time%] starting Dev tray launcher>> "%STARTUP_LOG%"
    start "" "%PYTHONW_EXE%" "%PROJECT_ROOT%\tools\tray_launcher.py"
    exit /b 0
)

echo [%date% %time%] pythonw.exe missing, falling back to python.exe>> "%STARTUP_LOG%"
start "" "%PYTHON_EXE%" "%PROJECT_ROOT%\tools\tray_launcher.py"
exit /b 0
