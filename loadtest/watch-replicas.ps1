# Shows backend and frontend replica counts every 10 s during a load test (Ctrl+C to stop):
#   powershell -ExecutionPolicy Bypass -File .\loadtest\watch-replicas.ps1
# Extra replicas are removed about 5 minutes after the load ends.

param([int]$IntervalSeconds = 10)

$Rg = "rg-sre-meteo"

function Get-ReplicaCount([string]$App) {
    $names = & az containerapp replica list -n $App -g $Rg --query "[].name" -o tsv --only-show-errors 2>$null
    if ($LASTEXITCODE -ne 0) { return "?" }
    return @($names | Where-Object { $_ }).Count
}

Write-Host "Time       backend  frontend" -ForegroundColor Cyan
while ($true) {
    $backend  = Get-ReplicaCount "meteo-backend"
    $frontend = Get-ReplicaCount "meteo-frontend"
    $bar = if ($backend -is [int]) { "#" * $backend } else { "" }
    Write-Host ("{0}  {1,7}  {2,8}  {3}" -f (Get-Date -Format "HH:mm:ss"), $backend, $frontend, $bar)
    Start-Sleep -Seconds $IntervalSeconds
}
