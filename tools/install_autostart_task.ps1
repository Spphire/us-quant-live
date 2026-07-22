param(
    [string]$ProjectRoot = "",
    [string]$Python = "",
    [string]$AccountsJsonPath = "",
    [string]$TaskName = "US Quant Live Tray",
    [string]$TaskPath = "\USQuant\",
    [int]$DelaySeconds = 60,
    [switch]$Status,
    [switch]$Unregister,
    [switch]$RunNow
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$OutputEncoding = [Console]::OutputEncoding

$toolsRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $ProjectRoot) {
    $ProjectRoot = Split-Path -Parent $toolsRoot
}
$ProjectRoot = (Resolve-Path -LiteralPath $ProjectRoot).Path

if (-not $Python) {
    $venvPython = Join-Path $ProjectRoot "venv\Scripts\python.exe"
    if (Test-Path -LiteralPath $venvPython) {
        $Python = $venvPython
    } else {
        $Python = "python"
    }
}
if ($Python -ne "python") {
    $Python = (Resolve-Path -LiteralPath $Python).Path
}

if (-not $AccountsJsonPath) {
    $AccountsJsonPath = Join-Path $ProjectRoot "configs\alpaca_acounts\alpaca_accounts.local.json"
}
$AccountsJsonPath = (Resolve-Path -LiteralPath $AccountsJsonPath).Path

$startBatPath = Join-Path $ProjectRoot "Start.bat"
if (-not (Test-Path -LiteralPath $startBatPath)) {
    throw "Start.bat not found: $startBatPath"
}
$startBatPath = (Resolve-Path -LiteralPath $startBatPath).Path

function ConvertTo-TaskArg {
    param([string]$Text)
    if ($Text -match '[\s"]') {
        return '"' + ($Text -replace '"', '\"') + '"'
    }
    return $Text
}

function ConvertTo-IsoDelay {
    param([int]$Seconds)
    if ($Seconds -le 0) {
        return $null
    }
    $ts = [TimeSpan]::FromSeconds($Seconds)
    if ($ts.TotalSeconds -lt 60) {
        return "PT$([int]$ts.TotalSeconds)S"
    }
    if (($ts.TotalSeconds % 60) -eq 0) {
        return "PT$([int]$ts.TotalMinutes)M"
    }
    return "PT$([int]$ts.TotalSeconds)S"
}

function Show-AutostartTask {
    $task = Get-ScheduledTask -TaskName $TaskName -TaskPath $TaskPath -ErrorAction SilentlyContinue
    if ($null -eq $task) {
        Write-Host "[Autostart] task not registered: $TaskPath$TaskName" -ForegroundColor Yellow
        return
    }

    $info = Get-ScheduledTaskInfo -TaskName $TaskName -TaskPath $TaskPath
    Write-Host "[Autostart] task registered: $TaskPath$TaskName" -ForegroundColor Green
    Write-Host "[Autostart] state: $($task.State)"
    Write-Host "[Autostart] last run: $($info.LastRunTime)"
    Write-Host "[Autostart] last result: $($info.LastTaskResult)"
    Write-Host "[Autostart] next run: $($info.NextRunTime)"
    foreach ($action in $task.Actions) {
        Write-Host "[Autostart] action: $($action.Execute) $($action.Arguments)"
        if ($action.WorkingDirectory) {
            Write-Host "[Autostart] working dir: $($action.WorkingDirectory)"
        }
    }
}

if ($Unregister) {
    $names = @($TaskName, "US Quant Live Watchdog") | Select-Object -Unique
    foreach ($name in $names) {
        $task = Get-ScheduledTask -TaskName $name -TaskPath $TaskPath -ErrorAction SilentlyContinue
        if ($null -eq $task) {
            Write-Host "[Autostart] task already absent: $TaskPath$name" -ForegroundColor Yellow
            continue
        }
        Unregister-ScheduledTask -TaskName $name -TaskPath $TaskPath -Confirm:$false
        Write-Host "[Autostart] unregistered: $TaskPath$name" -ForegroundColor Green
    }
    exit 0
}

if ($Status) {
    Show-AutostartTask
    exit 0
}

$oldTask = Get-ScheduledTask -TaskName "US Quant Live Watchdog" -TaskPath $TaskPath -ErrorAction SilentlyContinue
if ($null -ne $oldTask -and $TaskName -ne "US Quant Live Watchdog") {
    Unregister-ScheduledTask -TaskName "US Quant Live Watchdog" -TaskPath $TaskPath -Confirm:$false
    Write-Host "[Autostart] removed old headless task: $TaskPath US Quant Live Watchdog" -ForegroundColor Yellow
}

$cmdExe = Join-Path $env:WINDIR "System32\cmd.exe"
$taskArguments = "/c " + (ConvertTo-TaskArg $startBatPath)

$actionParams = @{
    Execute = $cmdExe
    Argument = $taskArguments
}
if ((Get-Command New-ScheduledTaskAction).Parameters.ContainsKey("WorkingDirectory")) {
    $actionParams["WorkingDirectory"] = $ProjectRoot
}
$action = New-ScheduledTaskAction @actionParams

$currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $currentUser
$delay = ConvertTo-IsoDelay -Seconds $DelaySeconds
if ($delay) {
    $trigger.Delay = $delay
}

$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -AllowStartIfOnBatteries

$principal = New-ScheduledTaskPrincipal `
    -UserId $currentUser `
    -LogonType Interactive `
    -RunLevel Limited

Register-ScheduledTask `
    -TaskName $TaskName `
    -TaskPath $TaskPath `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description "Starts the visible US Quant Live tray app at user logon." `
    -Force | Out-Null

Write-Host "[Autostart] registered: $TaskPath$TaskName" -ForegroundColor Green
Write-Host "[Autostart] delay: $DelaySeconds seconds"

if ($RunNow) {
    Start-ScheduledTask -TaskName $TaskName -TaskPath $TaskPath
    Write-Host "[Autostart] started task now"
}

Show-AutostartTask
