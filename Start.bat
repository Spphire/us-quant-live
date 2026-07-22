@echo off
setlocal

cd /d "%~dp0"
set "PROJECT_ROOT=%CD%"
set "PYTHON_EXE=%PROJECT_ROOT%\venv\Scripts\python.exe"
set "LOG_DIR=%PROJECT_ROOT%\artifacts\daily_alpaca_scheduler\daemon"
set "STARTUP_LOG=%LOG_DIR%\startup.bat.log"

if not exist "%LOG_DIR%" mkdir "%LOG_DIR%" >nul 2>&1
echo [%date% %time%] Start.bat invoked from %PROJECT_ROOT%>> "%STARTUP_LOG%"

if not exist "%PYTHON_EXE%" (
    echo [ERROR] venv not found at %PYTHON_EXE%>> "%STARTUP_LOG%"
    exit /b 1
)

echo [%date% %time%] restart mode: stopping existing project tray/scheduler/dashboard first>> "%STARTUP_LOG%"
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$root=(Resolve-Path -LiteralPath '%PROJECT_ROOT%').Path; " ^
  "$needles=@('tools\tray_launcher.py','tools\daily_alpaca_scheduler.py','tools\dashboard_server.py','tools\watch_daily_alpaca_scheduler.ps1'); " ^
  "$self=$PID; " ^
  "$activeExecutors=@(Get-CimInstance Win32_Process | Where-Object { if([int]$_.ProcessId -eq [int]$self){return $false}; $cmd=[string]$_.CommandLine; if(-not $cmd){return $false}; ($cmd.IndexOf($root,[StringComparison]::OrdinalIgnoreCase) -ge 0) -and ($cmd.IndexOf('src\alpaca_executor.py',[StringComparison]::OrdinalIgnoreCase) -ge 0) }); " ^
  "if($activeExecutors.Count -gt 0){ Write-Output ('restart aborted: active decision/execution pid(s)='+ (($activeExecutors | ForEach-Object { [string]$_.ProcessId }) -join ',')); exit 41 }; " ^
  "function Get-ProjectTargets { @(Get-CimInstance Win32_Process | Where-Object { if([int]$_.ProcessId -eq [int]$self){return $false}; $cmd=[string]$_.CommandLine; if(-not $cmd){return $false}; ($cmd.IndexOf($root,[StringComparison]::OrdinalIgnoreCase) -ge 0) -and (($needles | Where-Object { $cmd.IndexOf($_,[StringComparison]::OrdinalIgnoreCase) -ge 0 }).Count -gt 0) }) }; " ^
  "for($i=0; $i -lt 5; $i++){ $procs=@(Get-ProjectTargets); if($procs.Count -eq 0){ break }; foreach($p in $procs){ try { Write-Output ('stopping pid='+$p.ProcessId+' '+$p.Name); & taskkill.exe /F /T /PID ([string]$p.ProcessId) | Out-Null } catch { Write-Output ('taskkill failed pid='+$p.ProcessId+' '+$_.Exception.Message) }; try { Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue } catch {} }; Start-Sleep -Milliseconds 800 }; " ^
  "$remaining=@(Get-ProjectTargets); if($remaining.Count -gt 0){ Write-Output ('warning: remaining project processes after restart cleanup: '+(($remaining | ForEach-Object { [string]$_.ProcessId }) -join ',')) }; " ^
  "Remove-Item -LiteralPath (Join-Path $root 'artifacts\daily_alpaca_scheduler\daemon\tray_launcher.pid') -Force -ErrorAction SilentlyContinue; " ^
  "Remove-Item -LiteralPath (Join-Path $root 'artifacts\daily_alpaca_scheduler\daemon\scheduler.pid') -Force -ErrorAction SilentlyContinue; " ^
  "Remove-Item -LiteralPath (Join-Path $root 'artifacts\daily_alpaca_scheduler\watchdog\watchdog.pid') -Force -ErrorAction SilentlyContinue; " ^
  "exit 0" ^
  >> "%STARTUP_LOG%" 2>>&1

if errorlevel 1 (
    echo [%date% %time%] restart aborted because a decision or execution task is active>> "%STARTUP_LOG%"
    exit /b 1
)

if exist "%PROJECT_ROOT%\venv\Scripts\pythonw.exe" (
    echo [%date% %time%] starting tray launcher>> "%STARTUP_LOG%"
    start "" "%PROJECT_ROOT%\venv\Scripts\pythonw.exe" "%PROJECT_ROOT%\tools\tray_launcher.py"
    exit /b 0
)

echo [%date% %time%] pythonw.exe missing, falling back to python.exe>> "%STARTUP_LOG%"
start "" "%PYTHON_EXE%" "%PROJECT_ROOT%\tools\tray_launcher.py"
exit /b 0
