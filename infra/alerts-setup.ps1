param([Parameter(Mandatory = $true)][string]$Email)

$ErrorActionPreference = "Stop"
$Rg = "rg-sre-meteo"
$Pg = "pg-sre-meteo-koussay"
$ActionGroup = "ag-sre-meteo"

function Invoke-AzCli {
    $out = & az @args
    if ($LASTEXITCODE -ne 0) { throw "FAILED: az $($args -join ' ')" }
    return $out
}

function Get-AzOrNull {
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    $out = & az @args 2>$null
    $code = $LASTEXITCODE
    $ErrorActionPreference = $previous
    if ($code -ne 0) { return $null }
    return $out
}

function New-MetricAlert {
    param([string]$Name, [string]$Scope, [string]$Condition, [string]$Window, [string]$Frequency,
          [int]$Severity, [string]$Description)
    if (Get-AzOrNull monitor metrics alert show -n $Name -g $Rg --query id -o tsv) {
        Write-Host "  ${Name}: already set"
        return
    }
    Invoke-AzCli monitor metrics alert create -n $Name -g $Rg --scopes $Scope --condition $Condition `
        --window-size $Window --evaluation-frequency $Frequency --severity $Severity `
        --description $Description --action $ActionGroupId --only-show-errors -o none
    Write-Host "  ${Name}: created"
}

Write-Host "== 1/3 Azure Monitor provider" -ForegroundColor Cyan
Invoke-AzCli provider register --namespace Microsoft.Insights --wait | Out-Null

Write-Host "== 2/3 Action group (email)" -ForegroundColor Cyan
if (-not (Get-AzOrNull monitor action-group show -n $ActionGroup -g $Rg --query id -o tsv)) {
    Invoke-AzCli monitor action-group create -n $ActionGroup -g $Rg --short-name sremeteo `
        --action email astreinte $Email --only-show-errors -o none
}
$ActionGroupId = Invoke-AzCli monitor action-group show -n $ActionGroup -g $Rg --query id -o tsv

Write-Host "== 3/3 Alert rules" -ForegroundColor Cyan
$FrontendId = Invoke-AzCli containerapp show -n meteo-frontend -g $Rg --query id -o tsv --only-show-errors
$BackendId  = Invoke-AzCli containerapp show -n meteo-backend -g $Rg --query id -o tsv --only-show-errors
$PgId       = Invoke-AzCli postgres flexible-server show -n $Pg -g $Rg --query id -o tsv --only-show-errors

New-MetricAlert -Name "meteo-5xx-utilisateurs" -Scope $FrontendId -Severity 1 -Window 5m -Frequency 1m `
    -Condition "total Requests > 10 where statusCodeCategory includes 5xx" `
    -Description "More than 10 5xx responses in 5 minutes on the public URL"
New-MetricAlert -Name "meteo-backend-plafond-replicas" -Scope $BackendId -Severity 2 -Window 15m -Frequency 5m `
    -Condition "min Replicas >= 8" `
    -Description "Backend at max replicas for 15 minutes: review capacity, database first"
New-MetricAlert -Name "meteo-backend-redemarrages" -Scope $BackendId -Severity 2 -Window 15m -Frequency 5m `
    -Condition "total RestartCount > 3" `
    -Description "Backend containers restarting: crash or failing probe"
New-MetricAlert -Name "meteo-postgres-connexions" -Scope $PgId -Severity 2 -Window 5m -Frequency 1m `
    -Condition "max active_connections >= 30" `
    -Description "PostgreSQL connections close to the B1ms limit of 35"

Write-Host ""
Write-Host "Done. Alert rules are in the portal: Monitor > Alerts > Alert rules." -ForegroundColor Green
Write-Host "Azure sends a confirmation email to $Email (added to the action group)."
