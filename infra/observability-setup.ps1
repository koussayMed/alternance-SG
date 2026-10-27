# Creates Pushgateway, Prometheus and Grafana as Container Apps (run once; safe to re-run):
#   powershell -ExecutionPolicy Bypass -File .\infra\observability-setup.ps1
# Needs infra\azure-setup.ps1 already run, `az login`, Docker running. Later image updates
# are deployed by the CD (cd.yml) when observability/ changes. See README.md.
#
#   backend processes --push--> meteo-pushgateway (internal ingress, 1 replica)
#   meteo-prometheus --scrape--> meteo-pushgateway  (external ingress, basic auth)
#   meteo-grafana --PromQL--> meteo-prometheus      (external ingress, anonymous read-only)
#   meteo-grafana --managed identity--> Azure Monitor (container CPU, memory, replicas)
# Pushgateway and Prometheus keep state locally, so 1 replica each; Prometheus data is lost on restart.
# Cost: about 2 to 3 dollars a day.

$ErrorActionPreference = "Stop"

$Suffix   = "koussay"
$Rg       = "rg-sre-meteo"
$Acr      = "acrsremeteo$Suffix"
$CaeEnv   = "cae-sre-meteo"
$Identity = "id-sre-meteo-apps"
$PushgatewayImage = "prom/pushgateway:v1.11.4"
$PromUser = "prometheus"

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

function Invoke-Docker {
    & docker @args
    if ($LASTEXITCODE -ne 0) { throw "FAILED: docker $($args -join ' ')" }
}

function Test-App([string]$Name) {
    return [bool](Get-AzOrNull containerapp show -n $Name -g $Rg --query name -o tsv --only-show-errors)
}

Write-Host "== 1/6 Context" -ForegroundColor Cyan
$Registry   = "$Acr.azurecr.io"
$IdentityId = Invoke-AzCli identity show -n $Identity -g $Rg --query id -o tsv
$IdentityClientId    = Invoke-AzCli identity show -n $Identity -g $Rg --query clientId -o tsv
$IdentityPrincipalId = Invoke-AzCli identity show -n $Identity -g $Rg --query principalId -o tsv
$SubscriptionId = Invoke-AzCli account show --query id -o tsv
$RgId = Invoke-AzCli group show -n $Rg --query id -o tsv
$Domain     = Invoke-AzCli containerapp env show -n $CaeEnv -g $Rg --query properties.defaultDomain -o tsv --only-show-errors
$PushgatewayFqdn = "meteo-pushgateway.internal.$Domain"
$PrometheusFqdn  = "meteo-prometheus.$Domain"
$GrafanaFqdn     = "meteo-grafana.$Domain"
Write-Host "  domain $Domain"

function New-Password([int]$Length) {
    return -join ((48..57) + (65..90) + (97..122) | Get-Random -Count $Length | ForEach-Object { [char]$_ })
}
# Prometheus password: reuse the one stored in Grafana, else make a new one
$PromPassword = Get-AzOrNull containerapp secret show -n meteo-grafana -g $Rg --secret-name prometheus-password `
    --query value -o tsv --only-show-errors
if (-not $PromPassword) { $PromPassword = New-Password 24 }
# bcrypt hash for Prometheus, computed with Apache htpasswd in a throwaway container
$htpasswd = & docker run --rm httpd:2.4-alpine htpasswd -nbB $PromUser $PromPassword
if ($LASTEXITCODE -ne 0 -or -not $htpasswd) { throw "FAILED: bcrypt hash with htpasswd" }
$PromHash = ("$htpasswd".Trim() -split ":", 2)[1]

Write-Host "== 2/6 Images in the registry" -ForegroundColor Cyan
$Tag = Get-Date -Format "yyyyMMddHHmmss"
Invoke-AzCli acr login -n $Acr | Out-Null
Invoke-Docker pull $PushgatewayImage
Invoke-Docker tag $PushgatewayImage "$Registry/$PushgatewayImage"
Invoke-Docker push "$Registry/$PushgatewayImage"
Invoke-Docker build -t "$Registry/meteo-prometheus:$Tag" ./observability/prometheus
Invoke-Docker push "$Registry/meteo-prometheus:$Tag"
# Grafana: temporary copy where the subscription id is written into the Azure Monitor dashboard
$GrafanaBuild = Join-Path $env:TEMP "meteo-grafana-build"
if (Test-Path $GrafanaBuild) { Remove-Item $GrafanaBuild -Recurse -Force }
Copy-Item ./observability/grafana $GrafanaBuild -Recurse
$AzureDashboard = Join-Path $GrafanaBuild "dashboards\conteneurs-azure.json"
$json = [System.IO.File]::ReadAllText($AzureDashboard) -replace "__AZURE_SUBSCRIPTION_ID__", $SubscriptionId
[System.IO.File]::WriteAllText($AzureDashboard, $json, (New-Object System.Text.UTF8Encoding($false)))
Invoke-Docker build -t "$Registry/meteo-grafana:$Tag" $GrafanaBuild
Invoke-Docker push "$Registry/meteo-grafana:$Tag"

Write-Host "== 3/6 Pushgateway (internal ingress)" -ForegroundColor Cyan
if (Test-App "meteo-pushgateway") {
    Invoke-AzCli containerapp update -n meteo-pushgateway -g $Rg --image "$Registry/$PushgatewayImage" -o none --only-show-errors
} else {
    # --allow-insecure: plain HTTP inside the environment only
    Invoke-AzCli containerapp create -n meteo-pushgateway -g $Rg --environment $CaeEnv `
        --image "$Registry/$PushgatewayImage" `
        --registry-server $Registry --registry-identity $IdentityId --user-assigned $IdentityId `
        --ingress internal --target-port 9091 --allow-insecure `
        --cpu 0.25 --memory 0.5Gi --min-replicas 1 --max-replicas 1 -o none --only-show-errors
}

Write-Host "== 4/6 Prometheus (external ingress, basic auth)" -ForegroundColor Cyan
$PromEnv = @("PUSHGATEWAY_TARGET=${PushgatewayFqdn}:80", "PUSHGATEWAY_SCHEME=http", "RETENTION=7d",
             "PROMETHEUS_USER=$PromUser", "PROMETHEUS_PASSWORD_HASH=secretref:prometheus-password-hash")
if (Test-App "meteo-prometheus") {
    Invoke-AzCli containerapp secret set -n meteo-prometheus -g $Rg --secrets "prometheus-password-hash=$PromHash" `
        -o none --only-show-errors
    Invoke-AzCli containerapp update -n meteo-prometheus -g $Rg --image "$Registry/meteo-prometheus:$Tag" `
        --set-env-vars @PromEnv -o none --only-show-errors
    # Old IP rules from a first version of this script: reset the ingress (disable + enable)
    # instead of deleting them one by one; the address does not change
    $oldRules = Get-AzOrNull containerapp ingress access-restriction list -n meteo-prometheus -g $Rg `
        --query "[].name" -o tsv --only-show-errors
    if ($oldRules) {
        Invoke-AzCli containerapp ingress disable -n meteo-prometheus -g $Rg -o none --only-show-errors
        Invoke-AzCli containerapp ingress enable -n meteo-prometheus -g $Rg --type external --target-port 9090 `
            -o none --only-show-errors
    }
} else {
    Invoke-AzCli containerapp create -n meteo-prometheus -g $Rg --environment $CaeEnv `
        --image "$Registry/meteo-prometheus:$Tag" `
        --registry-server $Registry --registry-identity $IdentityId --user-assigned $IdentityId `
        --ingress external --target-port 9090 `
        --cpu 0.5 --memory 1Gi --min-replicas 1 --max-replicas 1 `
        --secrets "prometheus-password-hash=$PromHash" `
        --env-vars @PromEnv -o none --only-show-errors
}

Write-Host "== 5/6 Grafana (external ingress, read-only without login)" -ForegroundColor Cyan
# Azure Monitor metrics read by the apps' managed identity: Monitoring Reader, resource group only
$readerIds = Invoke-AzCli role assignment list --assignee $IdentityPrincipalId --role "Monitoring Reader" `
    --scope $RgId --query "[].id" -o tsv
if (-not $readerIds) {
    Invoke-AzCli role assignment create --assignee-object-id $IdentityPrincipalId --assignee-principal-type ServicePrincipal `
        --role "Monitoring Reader" --scope $RgId -o none
}
$GrafanaEnv = @("PROMETHEUS_URL=https://$PrometheusFqdn", "PROMETHEUS_USER=$PromUser",
                "GF_AZURE_MANAGED_IDENTITY_ENABLED=true", "GF_AZURE_MANAGED_IDENTITY_CLIENT_ID=$IdentityClientId",
                "AZURE_SUBSCRIPTION_ID=$SubscriptionId",
                "PROMETHEUS_PASSWORD=secretref:prometheus-password", "GF_SERVER_ROOT_URL=https://$GrafanaFqdn",
                "GF_SECURITY_ADMIN_PASSWORD=secretref:grafana-admin-password")
$NewPassword = $null
if (Test-App "meteo-grafana") {
    # Keep the existing admin password
    Invoke-AzCli containerapp secret set -n meteo-grafana -g $Rg --secrets "prometheus-password=$PromPassword" `
        -o none --only-show-errors
    Invoke-AzCli containerapp update -n meteo-grafana -g $Rg --image "$Registry/meteo-grafana:$Tag" `
        --set-env-vars @GrafanaEnv -o none --only-show-errors
} else {
    $NewPassword = New-Password 20
    Invoke-AzCli containerapp create -n meteo-grafana -g $Rg --environment $CaeEnv `
        --image "$Registry/meteo-grafana:$Tag" `
        --registry-server $Registry --registry-identity $IdentityId --user-assigned $IdentityId `
        --ingress external --target-port 3000 `
        --cpu 0.5 --memory 1Gi --min-replicas 1 --max-replicas 1 `
        --secrets "grafana-admin-password=$NewPassword" "prometheus-password=$PromPassword" `
        --env-vars @GrafanaEnv -o none --only-show-errors
}

Write-Host "== 6/6 Backend pushes to the Pushgateway (new revision, no downtime)" -ForegroundColor Cyan
Invoke-AzCli containerapp update -n meteo-backend -g $Rg --only-show-errors -o none `
    --set-env-vars "METRICS_PUSHGATEWAY_URL=http://$PushgatewayFqdn"

Write-Host ""
Write-Host "Grafana    : https://$GrafanaFqdn   (shareable, read-only dashboards)" -ForegroundColor Green
Write-Host "             dashboards: Meteo des villes - SRE; Conteneurs - Azure (Azure Monitor)"
Write-Host "Prometheus : https://$PrometheusFqdn   (user $PromUser, password below)" -ForegroundColor Green
Write-Host "Prometheus password (UI and k6): $PromPassword" -ForegroundColor Yellow
if ($NewPassword) {
    Write-Host "Grafana admin: admin / $NewPassword   (write it down now)" -ForegroundColor Yellow
}
Write-Host "Get the passwords back: az containerapp secret show -n meteo-grafana -g $Rg --secret-name grafana-admin-password (or prometheus-password) --query value -o tsv"
Write-Host "First metrics show up about 1 minute after the new backend revision starts."
