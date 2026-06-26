param(
    [string]$Python = "python",
    [string]$ProjectRoot = "",
    [string]$AccountsJsonPath = "",
    [string]$AccountName = "ALPACA_US_FULL",
    [int]$CheckSeconds = 60,
    [int]$HeartbeatStaleMinutes = 90,
    [int]$StartupGraceMinutes = 10,
    [int]$ActiveTaskGraceMinutes = 180,
    [int]$TaskLogFreshMinutes = 45,
    [switch]$Foreground,
    [switch]$Status,
    [switch]$Stop,
    [switch]$Force,
    [switch]$Once,
    [string[]]$SchedulerLauncherArgs = @()
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$OutputEncoding = [Console]::OutputEncoding

$watchdogScriptPath = $PSCommandPath
if (-not $watchdogScriptPath) {
    $watchdogScriptPath = $MyInvocation.MyCommand.Path
}

$toolsRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $ProjectRoot) {
    $ProjectRoot = Split-Path -Parent $toolsRoot
}
if (-not $AccountsJsonPath) {
    $AccountsJsonPath = Join-Path $ProjectRoot "configs/alpaca_acounts/alpaca_accounts.local.json"
}

$schedulerLauncherPath = Join-Path $toolsRoot "run_daily_alpaca_scheduler.ps1"
if (-not (Test-Path -LiteralPath $schedulerLauncherPath)) {
    throw "run_daily_alpaca_scheduler.ps1 not found: $schedulerLauncherPath"
}

$schedulerDaemonRoot = Join-Path $ProjectRoot "artifacts\daily_alpaca_scheduler\daemon"
$schedulerPidPath = Join-Path $schedulerDaemonRoot "scheduler.pid"
$schedulerStdoutPath = Join-Path $schedulerDaemonRoot "scheduler.out.log"
$schedulerStderrPath = Join-Path $schedulerDaemonRoot "scheduler.err.log"
$schedulerStatePath = Join-Path $ProjectRoot "artifacts\daily_alpaca_scheduler\state.json"
$schedulerLogsRoot = Join-Path $ProjectRoot "artifacts\daily_alpaca_scheduler\logs"

$watchdogRoot = Join-Path $ProjectRoot "artifacts\daily_alpaca_scheduler\watchdog"
$watchdogPidPath = Join-Path $watchdogRoot "watchdog.pid"
$watchdogStdoutPath = Join-Path $watchdogRoot "watchdog.out.log"
$watchdogStderrPath = Join-Path $watchdogRoot "watchdog.err.log"
$watchdogLogPath = Join-Path $watchdogRoot "watchdog.log"
$watchdogStatePath = Join-Path $watchdogRoot "watchdog_state.json"
$watchdogCommandPath = Join-Path $watchdogRoot "watchdog.command.txt"

function Get-ProcessFromPidFile {
    param(
        [string]$Path,
        [string[]]$CommandContains = @()
    )

    if (-not (Test-Path -LiteralPath $Path)) {
        return $null
    }
    $pidText = (Get-Content -LiteralPath $Path -Raw -ErrorAction SilentlyContinue).Trim()
    if (-not $pidText) {
        return $null
    }
    $processId = 0
    if (-not [int]::TryParse($pidText, [ref]$processId)) {
        return $null
    }
    $process = Get-Process -Id $processId -ErrorAction SilentlyContinue
    if ($null -eq $process) {
        return $null
    }
    if ($CommandContains.Count -eq 0) {
        return $process
    }
    try {
        $cmd = [string](Get-CimInstance Win32_Process -Filter "ProcessId = $processId" -ErrorAction Stop).CommandLine
    } catch {
        return $null
    }
    foreach ($needle in $CommandContains) {
        if (-not $needle) {
            continue
        }
        if ($cmd.IndexOf([string]$needle, [System.StringComparison]::OrdinalIgnoreCase) -lt 0) {
            return $null
        }
    }
    return $process
}

function Write-WatchdogLog {
    param([string]$Message)

    New-Item -ItemType Directory -Force -Path $watchdogRoot | Out-Null
    $line = "[Watchdog] $(Get-Date -Format o) $Message"
    Add-Content -LiteralPath $watchdogLogPath -Value $line -Encoding UTF8
    if ($Foreground -or $Once -or $Status) {
        Write-Host $line
    }
}

function Get-LatestHeartbeat {
    if (-not (Test-Path -LiteralPath $schedulerStdoutPath)) {
        return $null
    }
    $lines = @(Get-Content -LiteralPath $schedulerStdoutPath -Tail 500 -ErrorAction SilentlyContinue)
    for ($idx = $lines.Count - 1; $idx -ge 0; $idx--) {
        $line = [string]$lines[$idx]
        if ($line -match 'heartbeat\s+([0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:\-+]+)') {
            try {
                return [DateTimeOffset]::Parse($Matches[1])
            } catch {
                return $null
            }
        }
    }
    return $null
}

function Get-LatestTaskLogActivity {
    $files = @()
    foreach ($path in @($schedulerStdoutPath, $schedulerStderrPath)) {
        if (Test-Path -LiteralPath $path) {
            $files += Get-Item -LiteralPath $path
        }
    }
    if (Test-Path -LiteralPath $schedulerLogsRoot) {
        $files += Get-ChildItem -LiteralPath $schedulerLogsRoot -File -Filter "*.log" -ErrorAction SilentlyContinue
    }
    if ($files.Count -eq 0) {
        return $null
    }
    return ($files | Sort-Object LastWriteTime -Descending | Select-Object -First 1).LastWriteTime
}

function Get-ActiveTaskState {
    if (-not (Test-Path -LiteralPath $schedulerStatePath)) {
        return $null
    }
    try {
        $state = Get-Content -LiteralPath $schedulerStatePath -Raw | ConvertFrom-Json
    } catch {
        return $null
    }
    if ($null -eq $state.sessions) {
        return $null
    }

    $active = @()
    foreach ($sessionProp in $state.sessions.PSObject.Properties) {
        $sessionDate = [string]$sessionProp.Name
        $session = $sessionProp.Value
        foreach ($taskName in @("decision", "execute")) {
            if ($session.PSObject.Properties.Name -notcontains $taskName) {
                continue
            }
            $taskState = $session.$taskName
            if ([string]$taskState.status -ne "started") {
                continue
            }
            $startedAt = $null
            try {
                $startedAt = [DateTimeOffset]::Parse([string]$taskState.started_at_cn)
            } catch {
                $startedAt = $null
            }
            $active += [pscustomobject]@{
                session_date = $sessionDate
                task = $taskName
                started_at = $startedAt
                attempts = [int]($taskState.attempts)
            }
        }
    }
    if ($active.Count -eq 0) {
        return $null
    }
    return $active | Sort-Object started_at -Descending | Select-Object -First 1
}

function Get-ExecutorProcesses {
    $needleRoot = [string](Resolve-Path -LiteralPath $ProjectRoot)
    $processes = @()
    try {
        $candidates = Get-CimInstance Win32_Process -ErrorAction Stop | Where-Object {
            $cmd = [string]$_.CommandLine
            if (-not $cmd) {
                return $false
            }
            $hasExecutor = $cmd -match 'alpaca_executor\.py'
            $hasRoot = $cmd.IndexOf($needleRoot, [System.StringComparison]::OrdinalIgnoreCase) -ge 0
            return ($hasExecutor -and $hasRoot)
        }
        foreach ($item in $candidates) {
            $processes += [pscustomobject]@{
                ProcessId = [int]$item.ProcessId
                CreationDate = $item.CreationDate
                CommandLine = [string]$item.CommandLine
            }
        }
    } catch {
        return @()
    }
    return @($processes)
}

function Invoke-SchedulerStart {
    param([string]$Reason)

    Write-WatchdogLog "starting scheduler: reason=$Reason"
    $launcherArgs = @(
        "-ExecutionPolicy", "Bypass",
        "-File", $schedulerLauncherPath,
        "-Python", $Python,
        "-ProjectRoot", $ProjectRoot,
        "-AccountsJsonPath", $AccountsJsonPath,
        "-AccountName", $AccountName,
        "-Force"
    )
    if ($SchedulerLauncherArgs.Count -gt 0) {
        $launcherArgs += $SchedulerLauncherArgs
    }
    & powershell @launcherArgs | ForEach-Object {
        Write-WatchdogLog "scheduler-launcher: $_"
    }
}

function Save-WatchdogState {
    param([hashtable]$Payload)

    New-Item -ItemType Directory -Force -Path $watchdogRoot | Out-Null
    $tmpPath = "$watchdogStatePath.tmp"
    $Payload | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $tmpPath -Encoding UTF8
    Move-Item -LiteralPath $tmpPath -Destination $watchdogStatePath -Force
}

function Test-SchedulerHealth {
    $now = [DateTimeOffset]::Now
    $schedulerProcess = Get-ProcessFromPidFile -Path $schedulerPidPath -CommandContains @(
        [string](Resolve-Path -LiteralPath (Join-Path $toolsRoot "daily_alpaca_scheduler.py")),
        [string](Resolve-Path -LiteralPath $ProjectRoot)
    )
    $executorProcesses = @(Get-ExecutorProcesses)
    $activeTask = Get-ActiveTaskState
    $heartbeatAt = Get-LatestHeartbeat
    $latestTaskActivity = Get-LatestTaskLogActivity

    $heartbeatAgeMinutes = $null
    if ($null -ne $heartbeatAt) {
        $heartbeatAgeMinutes = [math]::Round(($now - $heartbeatAt).TotalMinutes, 3)
    }

    $taskActivityAgeMinutes = $null
    if ($null -ne $latestTaskActivity) {
        $taskActivityAgeMinutes = [math]::Round(((Get-Date) - $latestTaskActivity).TotalMinutes, 3)
    }

    $activeTaskAgeMinutes = $null
    if ($null -ne $activeTask -and $null -ne $activeTask.started_at) {
        $activeTaskAgeMinutes = [math]::Round(($now - $activeTask.started_at).TotalMinutes, 3)
    }

    $schedulerPid = $null
    $schedulerStartAgeMinutes = $null
    if ($null -ne $schedulerProcess) {
        $schedulerPid = [int]$schedulerProcess.Id
        $schedulerStartAgeMinutes = [math]::Round(((Get-Date) - $schedulerProcess.StartTime).TotalMinutes, 3)
    }

    $reason = "ok"
    $restartNeeded = $false

    if ($null -eq $schedulerProcess) {
        if ($executorProcesses.Count -gt 0) {
            $reason = "scheduler_missing_but_executor_active"
        } else {
            $reason = "scheduler_not_running"
            $restartNeeded = $true
        }
    } elseif ($null -eq $heartbeatAt) {
        if ($schedulerStartAgeMinutes -ne $null -and $schedulerStartAgeMinutes -gt $StartupGraceMinutes -and $executorProcesses.Count -eq 0) {
            $reason = "heartbeat_missing_after_startup_grace"
            $restartNeeded = $true
        } else {
            $reason = "waiting_for_first_heartbeat"
        }
    } elseif ($heartbeatAgeMinutes -gt $HeartbeatStaleMinutes) {
        if ($executorProcesses.Count -gt 0) {
            $reason = "heartbeat_stale_but_executor_active"
        } elseif ($null -ne $activeTask -and $activeTaskAgeMinutes -ne $null -and $activeTaskAgeMinutes -lt $ActiveTaskGraceMinutes) {
            $reason = "heartbeat_stale_but_state_has_active_task"
        } elseif ($taskActivityAgeMinutes -ne $null -and $taskActivityAgeMinutes -lt $TaskLogFreshMinutes) {
            $reason = "heartbeat_stale_but_task_log_fresh"
        } else {
            $reason = "heartbeat_stale"
            $restartNeeded = $true
        }
    }

    return [ordered]@{
        checked_at_cn = (Get-Date -Format o)
        scheduler_pid = $schedulerPid
        scheduler_start_age_minutes = $schedulerStartAgeMinutes
        executor_process_count = [int]$executorProcesses.Count
        heartbeat_at = if ($null -ne $heartbeatAt) { $heartbeatAt.ToString("o") } else { $null }
        heartbeat_age_minutes = $heartbeatAgeMinutes
        latest_task_log_activity = if ($null -ne $latestTaskActivity) { $latestTaskActivity.ToString("o") } else { $null }
        task_log_activity_age_minutes = $taskActivityAgeMinutes
        active_task = if ($null -ne $activeTask) {
            [ordered]@{
                session_date = $activeTask.session_date
                task = $activeTask.task
                started_at = if ($null -ne $activeTask.started_at) { $activeTask.started_at.ToString("o") } else { $null }
                age_minutes = $activeTaskAgeMinutes
                attempts = $activeTask.attempts
            }
        } else {
            $null
        }
        restart_needed = [bool]$restartNeeded
        reason = $reason
    }
}

function Show-WatchdogStatus {
    $watchdogProcess = Get-ProcessFromPidFile -Path $watchdogPidPath -CommandContains @(
        [string](Resolve-Path -LiteralPath $watchdogScriptPath),
        [string](Resolve-Path -LiteralPath $ProjectRoot)
    )
    if ($null -eq $watchdogProcess) {
        Write-Host "[AlpacaWatchdog] not running" -ForegroundColor Yellow
        if (Test-Path -LiteralPath $watchdogPidPath) {
            Write-Host "[AlpacaWatchdog] stale pid file: $watchdogPidPath"
        }
    } else {
        Write-Host "[AlpacaWatchdog] running pid=$($watchdogProcess.Id) started=$($watchdogProcess.StartTime)" -ForegroundColor Green
    }

    $schedulerProcess = Get-ProcessFromPidFile -Path $schedulerPidPath -CommandContains @(
        [string](Resolve-Path -LiteralPath (Join-Path $toolsRoot "daily_alpaca_scheduler.py")),
        [string](Resolve-Path -LiteralPath $ProjectRoot)
    )
    if ($null -eq $schedulerProcess) {
        Write-Host "[AlpacaScheduler] not running" -ForegroundColor Yellow
    } else {
        Write-Host "[AlpacaScheduler] running pid=$($schedulerProcess.Id) started=$($schedulerProcess.StartTime)" -ForegroundColor Green
    }
    Write-Host "[AlpacaWatchdog] log: $watchdogLogPath"
    Write-Host "[AlpacaWatchdog] state: $watchdogStatePath"

    if (Test-Path -LiteralPath $watchdogStatePath) {
        Write-Host "[AlpacaWatchdog] last check:"
        Get-Content -LiteralPath $watchdogStatePath -Raw | Write-Host
    }
}

New-Item -ItemType Directory -Force -Path $watchdogRoot | Out-Null

if ($Status) {
    Show-WatchdogStatus
    exit 0
}

if ($Stop) {
    $watchdogProcess = Get-ProcessFromPidFile -Path $watchdogPidPath -CommandContains @(
        [string](Resolve-Path -LiteralPath $watchdogScriptPath),
        [string](Resolve-Path -LiteralPath $ProjectRoot)
    )
    if ($null -eq $watchdogProcess) {
        Write-Host "[AlpacaWatchdog] not running" -ForegroundColor Yellow
        Remove-Item -LiteralPath $watchdogPidPath -Force -ErrorAction SilentlyContinue
        exit 0
    }
    Write-Host "[AlpacaWatchdog] stopping pid=$($watchdogProcess.Id)" -ForegroundColor Yellow
    Stop-Process -Id $watchdogProcess.Id -Force
    Remove-Item -LiteralPath $watchdogPidPath -Force -ErrorAction SilentlyContinue
    exit 0
}

if (-not $Foreground -and -not $Once) {
    $existing = Get-ProcessFromPidFile -Path $watchdogPidPath -CommandContains @(
        [string](Resolve-Path -LiteralPath $watchdogScriptPath),
        [string](Resolve-Path -LiteralPath $ProjectRoot)
    )
    if ($null -ne $existing) {
        if (-not $Force) {
            Write-Host "[AlpacaWatchdog] already running pid=$($existing.Id). Use -Force to restart." -ForegroundColor Yellow
            Show-WatchdogStatus
            exit 0
        }
        Write-Host "[AlpacaWatchdog] restarting existing pid=$($existing.Id)" -ForegroundColor Yellow
        Stop-Process -Id $existing.Id -Force
        Remove-Item -LiteralPath $watchdogPidPath -Force -ErrorAction SilentlyContinue
    }

    $backgroundArgs = @(
        "-NoProfile",
        "-ExecutionPolicy", "Bypass",
        "-File", $watchdogScriptPath,
        "-Python", $Python,
        "-ProjectRoot", $ProjectRoot,
        "-AccountsJsonPath", $AccountsJsonPath,
        "-AccountName", $AccountName,
        "-CheckSeconds", [string]$CheckSeconds,
        "-HeartbeatStaleMinutes", [string]$HeartbeatStaleMinutes,
        "-StartupGraceMinutes", [string]$StartupGraceMinutes,
        "-ActiveTaskGraceMinutes", [string]$ActiveTaskGraceMinutes,
        "-TaskLogFreshMinutes", [string]$TaskLogFreshMinutes,
        "-Foreground"
    )
    if ($SchedulerLauncherArgs.Count -gt 0) {
        $backgroundArgs += "-SchedulerLauncherArgs"
        $backgroundArgs += $SchedulerLauncherArgs
    }

    $quotedArgs = $backgroundArgs | ForEach-Object {
        $text = [string]$_
        if ($text -match '[\s"]') {
            '"' + ($text -replace '"', '\"') + '"'
        } else {
            $text
        }
    }
    "powershell $($quotedArgs -join ' ')" | Set-Content -LiteralPath $watchdogCommandPath -Encoding UTF8

    Write-Host "[AlpacaWatchdog] starting watchdog in background" -ForegroundColor Cyan
    Write-Host "[AlpacaWatchdog] project root: $ProjectRoot" -ForegroundColor Cyan
    Write-Host "[AlpacaWatchdog] stdout: $watchdogStdoutPath" -ForegroundColor Cyan
    Write-Host "[AlpacaWatchdog] stderr: $watchdogStderrPath" -ForegroundColor Cyan
    $process = Start-Process `
        -FilePath "powershell" `
        -ArgumentList $backgroundArgs `
        -WorkingDirectory $ProjectRoot `
        -RedirectStandardOutput $watchdogStdoutPath `
        -RedirectStandardError $watchdogStderrPath `
        -WindowStyle Hidden `
        -PassThru

    Set-Content -LiteralPath $watchdogPidPath -Value $process.Id -Encoding ASCII
    Write-Host "[AlpacaWatchdog] started pid=$($process.Id)" -ForegroundColor Green
    exit 0
}

Write-WatchdogLog "online. check_seconds=$CheckSeconds heartbeat_stale_minutes=$HeartbeatStaleMinutes"
$restartCount = 0
while ($true) {
    try {
        $health = Test-SchedulerHealth
        if ([bool]$health.restart_needed) {
            $restartCount += 1
            Invoke-SchedulerStart -Reason ([string]$health.reason)
            $health["action"] = "restart_scheduler"
        } else {
            $health["action"] = "none"
        }
        $health["restart_count"] = $restartCount
        Save-WatchdogState -Payload $health
        Write-WatchdogLog "check reason=$($health.reason) action=$($health.action) scheduler_pid=$($health.scheduler_pid) executor_count=$($health.executor_process_count) heartbeat_age_minutes=$($health.heartbeat_age_minutes)"
    } catch {
        Write-WatchdogLog "warning: check failed: $($_.Exception.Message)"
    }

    if ($Once) {
        break
    }
    Start-Sleep -Seconds ([math]::Max($CheckSeconds, 5))
}
