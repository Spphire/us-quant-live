param(
    [string]$Date = "",
    [string]$IbkrBaseUrl = "https://127.0.0.1:5000/v1/api",
    [string]$IbkrAccountId = "",
    [string]$OutputRoot = ""
)

$ErrorActionPreference = "Stop"

$toolsRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectRoot = Split-Path -Parent $toolsRoot
$executorPath = Join-Path $projectRoot "src/ibkr_executor.py"
$alpacaAccountsPath = Join-Path $projectRoot "configs/alpaca_acounts/alpaca_accounts.local.json"

if (-not (Test-Path -LiteralPath $executorPath)) {
    throw "ibkr_executor.py not found: $executorPath"
}
if (-not (Test-Path -LiteralPath $alpacaAccountsPath)) {
    throw "alpaca account config not found: $alpacaAccountsPath"
}

if (-not $Date) {
    $Date = (Get-Date).ToString("yyyy-MM-dd")
}
if (-not $OutputRoot) {
    $OutputRoot = Join-Path $projectRoot ("artifacts/ibkr_executor/{0}_planonly" -f $Date.Replace('-', ''))
}

if (-not $IbkrAccountId) {
    try {
        $accRaw = & curl.exe -k -sS "$IbkrBaseUrl/iserver/accounts" 2>$null
        $acc = $accRaw | ConvertFrom-Json
        if ($acc -and $acc.selectedAccount) {
            $IbkrAccountId = [string]$acc.selectedAccount
        } elseif ($acc -and $acc.accounts -and $acc.accounts.Count -gt 0) {
            $IbkrAccountId = [string]$acc.accounts[0]
        }
    } catch {
    }
}

New-Item -ItemType Directory -Force -Path $OutputRoot | Out-Null

$args = @(
    $executorPath,
    "--date", $Date,
    "--trigger-mode", "plan_only",
    "--no-submit",
    "--accounts-json-path", $alpacaAccountsPath,
    "--account-name", "ALPACA_US_FULL",
    "--ibkr-base-url", $IbkrBaseUrl,
    "--output-root", $OutputRoot
)

if ($IbkrAccountId) {
    $args += @("--ibkr-account-id", $IbkrAccountId)
}

if ($IbkrAccountId) {
    Write-Host "[IBKR] running plan-only with account: $IbkrAccountId" -ForegroundColor Cyan
} else {
    Write-Host "[IBKR] running plan-only (account id will be auto-resolved from gateway session)." -ForegroundColor Cyan
}
Push-Location $projectRoot
try {
    & python @args
    exit $LASTEXITCODE
} finally {
    Pop-Location
}
