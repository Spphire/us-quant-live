param(
    [string]$Python = "python",
    [string]$ProjectRoot = "",
    [string]$AccountsJsonPath = "",
    [string]$AccountName = "ALPACA_US_FULL",
    [string]$DecisionTimeCn = "12:30",
    [string]$ExecuteTimeCn = "22:00",
    [string]$TargetNyTime = "10:00",
    [ValidateSet("alpaca_calendar", "weekday", "always")]
    [string]$TradingDaySource = "alpaca_calendar",
    [switch]$DryRun,
    [switch]$Foreground,
    [switch]$Status,
    [switch]$Stop,
    [switch]$Force,
    [ValidateSet("", "decision", "execute", "both", "due")]
    [string]$RunOnce = "",
    [string]$Date = "",
    [string[]]$ExtraArgs = @()
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$OutputEncoding = [Console]::OutputEncoding

$toolsRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $ProjectRoot) {
    $ProjectRoot = Split-Path -Parent $toolsRoot
}
if (-not $AccountsJsonPath) {
    $AccountsJsonPath = Join-Path $ProjectRoot "configs/alpaca_acounts/alpaca_accounts.local.json"
}

$schedulerPath = Join-Path $toolsRoot "daily_alpaca_scheduler.py"
if (-not (Test-Path -LiteralPath $schedulerPath)) {
    throw "daily_alpaca_scheduler.py not found: $schedulerPath"
}

$daemonRoot = Join-Path $ProjectRoot "artifacts\daily_alpaca_scheduler\daemon"
$stdoutPath = Join-Path $daemonRoot "scheduler.out.log"
$stderrPath = Join-Path $daemonRoot "scheduler.err.log"
$pidPath = Join-Path $daemonRoot "scheduler.pid"
$commandPath = Join-Path $daemonRoot "scheduler.command.txt"

function Get-SchedulerProcess {
    if (-not (Test-Path -LiteralPath $pidPath)) {
        return $null
    }
    $pidText = (Get-Content -LiteralPath $pidPath -Raw -ErrorAction SilentlyContinue).Trim()
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
    try {
        $cmd = [string](Get-CimInstance Win32_Process -Filter "ProcessId = $processId" -ErrorAction Stop).CommandLine
    } catch {
        return $null
    }
    $schedulerNeedle = [string](Resolve-Path -LiteralPath $schedulerPath)
    $projectNeedle = [string](Resolve-Path -LiteralPath $ProjectRoot)
    $hasScheduler = $cmd.IndexOf($schedulerNeedle, [System.StringComparison]::OrdinalIgnoreCase) -ge 0
    $hasProject = $cmd.IndexOf($projectNeedle, [System.StringComparison]::OrdinalIgnoreCase) -ge 0
    if (-not ($hasScheduler -and $hasProject)) {
        return $null
    }
    return $process
}

function Show-SchedulerStatus {
    $process = Get-SchedulerProcess
    if ($null -eq $process) {
        Write-Host "[AlpacaScheduler] not running" -ForegroundColor Yellow
        if (Test-Path -LiteralPath $pidPath) {
            Write-Host "[AlpacaScheduler] stale pid file: $pidPath"
        }
        return
    }
    Write-Host "[AlpacaScheduler] running pid=$($process.Id) started=$($process.StartTime)" -ForegroundColor Green
    Write-Host "[AlpacaScheduler] stdout: $stdoutPath"
    Write-Host "[AlpacaScheduler] stderr: $stderrPath"
}

New-Item -ItemType Directory -Force -Path $daemonRoot | Out-Null

if ($Status) {
    Show-SchedulerStatus
    exit 0
}

if ($Stop) {
    $process = Get-SchedulerProcess
    if ($null -eq $process) {
        Write-Host "[AlpacaScheduler] not running" -ForegroundColor Yellow
        Remove-Item -LiteralPath $pidPath -Force -ErrorAction SilentlyContinue
        exit 0
    }
    Write-Host "[AlpacaScheduler] stopping pid=$($process.Id)" -ForegroundColor Yellow
    Stop-Process -Id $process.Id -Force
    Remove-Item -LiteralPath $pidPath -Force -ErrorAction SilentlyContinue
    exit 0
}

$schedulerArgs = @(
    $schedulerPath,
    "--project-root", $ProjectRoot,
    "--accounts-json-path", $AccountsJsonPath,
    "--account-name", $AccountName,
    "--decision-time-cn", $DecisionTimeCn,
    "--execute-time-cn", $ExecuteTimeCn,
    "--target-ny-time", $TargetNyTime,
    "--trading-day-source", $TradingDaySource
)

if ($DryRun) {
    $schedulerArgs += "--dry-run"
}
if ($Force -and $RunOnce) {
    $schedulerArgs += "--force"
}
if ($RunOnce) {
    $schedulerArgs += @("--run-once", $RunOnce)
}
if ($Date) {
    $schedulerArgs += @("--date", $Date)
}
if ($ExtraArgs.Count -gt 0) {
    $schedulerArgs += $ExtraArgs
}

$quotedArgs = $schedulerArgs | ForEach-Object {
    $text = [string]$_
    if ($text -match '[\s"]') {
        '"' + ($text -replace '"', '\"') + '"'
    } else {
        $text
    }
}
$commandText = "$Python " + ($quotedArgs -join " ")
$commandText | Set-Content -LiteralPath $commandPath -Encoding UTF8

if ($Foreground -or $RunOnce) {
    Write-Host "[AlpacaScheduler] starting daily scheduler in foreground" -ForegroundColor Cyan
    Write-Host "[AlpacaScheduler] project root: $ProjectRoot" -ForegroundColor Cyan
    Write-Host "[AlpacaScheduler] decision CN: $DecisionTimeCn, execute CN: $ExecuteTimeCn" -ForegroundColor Cyan
    Push-Location $ProjectRoot
    try {
        & $Python @schedulerArgs
        exit $LASTEXITCODE
    } finally {
        Pop-Location
    }
}

$existing = Get-SchedulerProcess
if ($null -ne $existing) {
    if (-not $Force) {
        Write-Host "[AlpacaScheduler] already running pid=$($existing.Id). Use -Force to restart." -ForegroundColor Yellow
        Show-SchedulerStatus
        exit 0
    }
    Write-Host "[AlpacaScheduler] restarting existing pid=$($existing.Id)" -ForegroundColor Yellow
    Stop-Process -Id $existing.Id -Force
    Remove-Item -LiteralPath $pidPath -Force -ErrorAction SilentlyContinue
}

Write-Host "[AlpacaScheduler] starting daily scheduler in background" -ForegroundColor Cyan
Write-Host "[AlpacaScheduler] project root: $ProjectRoot" -ForegroundColor Cyan
Write-Host "[AlpacaScheduler] decision CN: $DecisionTimeCn, execute CN: $ExecuteTimeCn" -ForegroundColor Cyan
Write-Host "[AlpacaScheduler] stdout: $stdoutPath" -ForegroundColor Cyan
Write-Host "[AlpacaScheduler] stderr: $stderrPath" -ForegroundColor Cyan

$process = Start-Process `
    -FilePath $Python `
    -ArgumentList $schedulerArgs `
    -WorkingDirectory $ProjectRoot `
    -RedirectStandardOutput $stdoutPath `
    -RedirectStandardError $stderrPath `
    -WindowStyle Hidden `
    -PassThru

Set-Content -LiteralPath $pidPath -Value $process.Id -Encoding ASCII
Write-Host "[AlpacaScheduler] started pid=$($process.Id)" -ForegroundColor Green
exit 0
