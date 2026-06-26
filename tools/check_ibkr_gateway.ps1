param(
    [string]$BaseUrl = "https://127.0.0.1:5000/v1/api"
)

$ErrorActionPreference = "Stop"

function Invoke-IbkrJson {
    param(
        [string]$Method,
        [string]$Path
    )
    $url = ($BaseUrl.TrimEnd("/") + $Path)
    $tmp = [System.IO.Path]::GetTempFileName()
    if ($Method -eq "POST") {
        $code = & curl.exe -k -sS `
            -H "accept: application/json" `
            -H "content-type: application/json" `
            -X POST `
            -d "{}" `
            -o $tmp `
            -w "%{http_code}" `
            $url 2>$null
    } else {
        $code = & curl.exe -k -sS -H "accept: application/json" -X $Method -o $tmp -w "%{http_code}" $url 2>$null
    }

    $raw = ""
    if (Test-Path -LiteralPath $tmp) {
        $raw = Get-Content -LiteralPath $tmp -Raw -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue
    }

    $parsed = $null
    try {
        if ($raw) { $parsed = $raw | ConvertFrom-Json }
    } catch {}

    return [ordered]@{
        http_code = [int]$code
        json = $parsed
        raw = $raw
    }
}

$status = Invoke-IbkrJson -Method "POST" -Path "/iserver/auth/status"
$accounts = Invoke-IbkrJson -Method "GET" -Path "/iserver/accounts"

$summary = [ordered]@{
    ok = $true
    base_url = $BaseUrl
    timestamp = (Get-Date).ToString("s")
    auth = $status
    accounts = $accounts
}

$summary | ConvertTo-Json -Depth 8
