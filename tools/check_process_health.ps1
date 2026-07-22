param(
    [string]$DashboardUrl = "http://127.0.0.1:18076"
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$OutputEncoding = [Console]::OutputEncoding

$url = $DashboardUrl.TrimEnd("/") + "/api/process-health"
try {
    $health = Invoke-RestMethod -Uri $url -TimeoutSec 8
} catch {
    Write-Host "[ProcessHealth] dashboard unreachable: $url" -ForegroundColor Red
    throw
}

$status = [string]$health.status
$color = if ($status -eq "pass") { "Green" } else { "Yellow" }
Write-Host "[ProcessHealth] status=$status" -ForegroundColor $color
Write-Host "[ProcessHealth] launcher=$($health.pid_files.launcher) scheduler=$($health.pid_files.scheduler) dashboard=$($health.pid_files.dashboard) listener=$($health.pid_files.dashboard_port_listener)"
Write-Host "[ProcessHealth] scheduler_bound_to_launcher=$($health.bindings.scheduler_bound_to_launcher) dashboard_bound_to_scheduler=$($health.bindings.dashboard_bound_to_scheduler) dashboard_listening=$($health.bindings.dashboard_listening) stub_chain_detected=$($health.bindings.stub_chain_detected)"
Write-Host "[ProcessHealth] role_counts=$($health.role_counts | ConvertTo-Json -Compress)"
if ($health.issues.Count -gt 0) {
    Write-Host "[ProcessHealth] issues=$($health.issues -join ',')" -ForegroundColor Yellow
}

$health.processes |
    Sort-Object pid |
    Select-Object pid,parent_pid,role,name,executable_path |
    Format-Table -AutoSize
