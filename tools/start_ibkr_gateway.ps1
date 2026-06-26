param(
    [string]$GatewayRoot = "",
    [string]$ConfPath = "",
    [bool]$OpenBrowser = $true
)

$ErrorActionPreference = "Stop"

function Test-Port5000 {
    try {
        $conn = Get-NetTCPConnection -LocalPort 5000 -State Listen -ErrorAction Stop
        return ($null -ne $conn)
    } catch {
        return $false
    }
}

function Get-AuthStatus {
    try {
        $tmp = [System.IO.Path]::GetTempFileName()
        $code = & curl.exe -k -sS --connect-timeout 1 --max-time 2 `
            -H "accept: application/json" `
            -H "content-type: application/json" `
            -X POST `
            -d "{}" `
            -o $tmp `
            -w "%{http_code}" `
            "https://127.0.0.1:5000/v1/api/iserver/auth/status" 2>$null

        if (-not $code) { $code = "000" }
        $body = ""
        if (Test-Path -LiteralPath $tmp) {
            $body = Get-Content -LiteralPath $tmp -Raw -ErrorAction SilentlyContinue
            Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue
        }
        $json = $null
        if ($body) {
            try { $json = $body | ConvertFrom-Json } catch {}
        }
        return [pscustomobject]@{
            http_code = [int]$code
            json = $json
            raw = $body
        }
    } catch {
        return $null
    }
}

$toolsRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $GatewayRoot) {
    $GatewayRoot = Join-Path $toolsRoot "clientportal.gw"
}
if (-not $ConfPath) {
    $ConfPath = Join-Path $GatewayRoot "root/conf.yaml"
}

$runBat = Join-Path $GatewayRoot "bin/run.bat"
if (-not (Test-Path -LiteralPath $runBat)) {
    throw "run.bat not found: $runBat"
}
if (-not (Test-Path -LiteralPath $ConfPath)) {
    throw "conf.yaml not found: $ConfPath"
}

if (-not (Test-Port5000)) {
    Write-Host "[IBKR] starting Client Portal Gateway ..." -ForegroundColor Cyan
    $argLine = "`"$runBat`" `"$ConfPath`""
    Start-Process -FilePath "cmd.exe" -ArgumentList "/c", $argLine -WorkingDirectory $GatewayRoot -WindowStyle Normal | Out-Null
    Start-Sleep -Seconds 3
} else {
    Write-Host "[IBKR] port 5000 already listening; reusing existing gateway." -ForegroundColor Yellow
}

if ([bool]$OpenBrowser) {
    Start-Process "https://localhost:5000" | Out-Null
}

Write-Host "[IBKR] polling auth status ..." -ForegroundColor Cyan
$status = $null
for ($i = 0; $i -lt 20; $i++) {
    $status = Get-AuthStatus
    if ($status -and $status.http_code -ne 0) { break }
    Start-Sleep -Milliseconds 500
}

if ($null -eq $status) {
    Write-Host "[IBKR] gateway is up, but auth endpoint not ready yet." -ForegroundColor Yellow
    Write-Host "Open https://localhost:5000 and log in (paper account + 2FA)." -ForegroundColor Yellow
    exit 0
}

if ($status.http_code -eq 401) {
    Write-Host "[IBKR] gateway is running, but not logged in yet (HTTP 401)." -ForegroundColor Yellow
    Write-Host "Please complete login in browser: https://localhost:5000" -ForegroundColor Yellow
    exit 0
}

$auth = $false
$conn = $false
if ($status.json) {
    $auth = [bool]$status.json.authenticated
    $conn = [bool]$status.json.connected
}
Write-Host ("[IBKR] auth status: http={0}, authenticated={1}, connected={2}" -f $status.http_code, $auth, $conn) -ForegroundColor Green
if (-not $auth -or $status.http_code -ne 200) {
    Write-Host "Please complete login in browser: https://localhost:5000" -ForegroundColor Yellow
}
