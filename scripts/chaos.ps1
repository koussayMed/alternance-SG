param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("status", "api-down", "api-up", "db-down", "db-up")]
    [string]$Action,
    [switch]$Follow
)

$ErrorActionPreference = "Stop"
$Rg      = "rg-sre-meteo"
$Backend = "meteo-backend"
$Pg      = "pg-sre-meteo-koussay"
$Visitor = "00000000-0000-4000-8000-000000000001"   # load test visitor with 3 cities

function Invoke-AzCli {
    $out = & az @args
    if ($LASTEXITCODE -ne 0) { throw "FAILED: az $($args -join ' ')" }
    return $out
}

function Get-Json([string]$Url, [hashtable]$Headers = @{}) {
    # Explicit UTF-8 decoding (PowerShell 5.1 gets accents wrong otherwise)
    $r = Invoke-WebRequest -Uri $Url -Headers $Headers -UseBasicParsing -TimeoutSec 20
    return [System.Text.Encoding]::UTF8.GetString($r.RawContentStream.ToArray()) | ConvertFrom-Json
}

function Show-State([string]$AppUrl) {
    $time = Get-Date -Format "HH:mm:ss"
    try {
        $info = Get-Json "$AppUrl/api/info"
        Write-Host "$time  version $($info.version)  replica $($info.instance.replica)" -ForegroundColor Cyan
    } catch {
        Write-Host "$time  /api/info failed: $($_.Exception.Message)" -ForegroundColor Red
    }
    try {
        # No @(...): PowerShell 5.1 returns the JSON list as one object and @() would nest it
        $cities = Get-Json "$AppUrl/api/weather" @{ "X-Visitor-Id" = $Visitor }
        foreach ($city in $cities) {
            if ($city.weather) {
                Write-Host ("    {0,-12} {1,6} C   source = {2}" -f $city.name, $city.weather.temperature_c, $city.weather.source)
            } else {
                Write-Host ("    {0,-12} no weather: {1}" -f $city.name, $city.error) -ForegroundColor Yellow
            }
        }
    } catch {
        Write-Host "    /api/weather failed: $($_.Exception.Message)" -ForegroundColor Red
    }
}

$fqdn = Invoke-AzCli containerapp show -n meteo-frontend -g $Rg --query properties.configuration.ingress.fqdn -o tsv --only-show-errors
$AppUrl = "https://$fqdn"

switch ($Action) {
    "status" {
        do {
            Show-State $AppUrl
            if ($Follow) { Start-Sleep -Seconds 5 }
        } while ($Follow)
    }
    "api-down" {
        Write-Host "Weather API outage: new backend revision (about 1 min)..." -ForegroundColor Yellow
        Invoke-AzCli containerapp update -n $Backend -g $Rg --only-show-errors -o none --set-env-vars `
            "FORECAST_API_URL=https://api.open-meteo.invalid/v1/forecast" `
            "GEOCODING_API_URL=https://geocoding-api.open-meteo.invalid/v1/search" `
            "WEATHER_CACHE_TTL_S=0"
        Write-Host "Expected: cities shown with their last value (source = stale), known suggestions still"
        Write-Host "served, new cities cannot be added (503), circuit breakers open."
    }
    "api-up" {
        Write-Host "Weather API back: new backend revision (about 1 min)..." -ForegroundColor Green
        Invoke-AzCli containerapp update -n $Backend -g $Rg --only-show-errors -o none `
            --remove-env-vars FORECAST_API_URL GEOCODING_API_URL WEATHER_CACHE_TTL_S
        Write-Host "Expected: circuits close on the first successful call, source = live then cache."
    }
    "db-down" {
        Write-Host "Stopping PostgreSQL (1 to 3 min)..." -ForegroundColor Yellow
        Invoke-AzCli postgres flexible-server stop -g $Rg -n $Pg --only-show-errors -o none
        Write-Host "Expected: the page loads, weather at your position still works (no database),"
        Write-Host "the city list answers 503 fast (no hang), 5xx alert within 5 minutes."
    }
    "db-up" {
        Write-Host "Starting PostgreSQL (1 to 3 min)..." -ForegroundColor Green
        Invoke-AzCli postgres flexible-server start -g $Rg -n $Pg --only-show-errors -o none
        Write-Host "Expected: back to normal without restarting the backend (the pool reconnects)."
    }
}
