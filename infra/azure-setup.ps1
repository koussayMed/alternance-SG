# Creates the Azure infrastructure (run once from your machine; safe to re-run):
#   powershell -ExecutionPolicy Bypass -File .\infra\azure-setup.ps1
# Needs: Azure CLI, `az login`, Docker running. See README.md.
#
#   Browser --HTTPS--> meteo-frontend (external ingress, nginx)
#                        --HTTPS--> meteo-backend (internal ingress, Flask)
#                                     --TLS--> PostgreSQL Flexible Server
#   Images in Azure Container Registry, pulled with a managed identity (no registry password).
# Cost: a few dollars a day. Clean up: az group delete -n rg-sre-meteo --yes --no-wait
# Windows note: az is a .cmd file, so avoid ( ) | & in its arguments.

$ErrorActionPreference = "Stop"

# --- Settings ----------------------------------------------------------------------------------
$Location = "francecentral"
$Suffix   = "koussay"                # ACR and PostgreSQL names must be globally unique
$Rg       = "rg-sre-meteo"
$Acr      = "acrsremeteo$Suffix"     # letters and digits only
$Pg       = "pg-sre-meteo-$Suffix"
$CaeEnv   = "cae-sre-meteo"
$Identity = "id-sre-meteo-apps"
$DbName   = "meteo"
$DbUser   = "meteoadmin"
# Random DB password, never written to the repo, only stored as an Azure secret.
# Regenerated on every run and applied to both the server and the backend secret.
$DbPassword = -join ((48..57) + (65..90) + (97..122) | Get-Random -Count 28 | ForEach-Object { [char]$_ })

function Invoke-AzCli {
    # Runs az and stops on failure (never prints a secret)
    $out = & az @args
    if ($LASTEXITCODE -ne 0) {
        $safe = ($args -join ' ') -replace 'postgresql://\S+', 'postgresql://***' -replace '(--admin-password\s+)\S+', '$1***'
        throw "FAILED: az $safe"
    }
    return $out
}

function Get-AzOrNull {
    # Runs az and returns $null on failure (e.g. missing resource) without stopping.
    # (PowerShell 5.1 turns stderr into a terminating error under ErrorActionPreference=Stop.)
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    $out = & az @args 2>$null
    $code = $LASTEXITCODE
    $ErrorActionPreference = $previous
    if ($code -ne 0) { return $null }
    return $out
}

function Deploy-ContainerApp {
    # Creates or updates a Container App: a "Failed" app is deleted and recreated,
    # and transient Azure errors are retried twice.
    param([string]$Name, [scriptblock]$Create, [scriptblock]$Update)
    for ($attempt = 1; $attempt -le 3; $attempt++) {
        $state = Get-AzOrNull containerapp show -n $Name -g $Rg --query properties.provisioningState -o tsv
        if ($state -eq "Failed") {
            Write-Host "  $Name is Failed: deleting before recreating" -ForegroundColor Yellow
            Invoke-AzCli containerapp delete -n $Name -g $Rg --yes -o none
            $state = $null
        }
        try {
            if ($state) { & $Update } else { & $Create }
            return
        } catch {
            if ($attempt -eq 3) { throw }
            Write-Host "  Attempt $attempt failed, retrying in 30 s" -ForegroundColor Yellow
            Start-Sleep -Seconds 30
        }
    }
}

Write-Host "== 1/7 Resource providers and CLI extension" -ForegroundColor Cyan
az extension add --name containerapp --upgrade --only-show-errors
foreach ($ns in "Microsoft.App", "Microsoft.OperationalInsights", "Microsoft.ContainerRegistry",
                "Microsoft.DBforPostgreSQL", "Microsoft.ManagedIdentity") {
    Invoke-AzCli provider register --namespace $ns --wait | Out-Null
}

Write-Host "== 2/7 Resource group, registry and managed identity" -ForegroundColor Cyan
Invoke-AzCli group create -n $Rg -l $Location -o none
if (-not (Get-AzOrNull acr show -n $Acr -g $Rg --query id -o tsv)) {
    Invoke-AzCli acr create -n $Acr -g $Rg --sku Basic --admin-enabled false -o none
}
if (-not (Get-AzOrNull identity show -n $Identity -g $Rg --query id -o tsv)) {
    Invoke-AzCli identity create -n $Identity -g $Rg -o none
}
$IdentityId  = Invoke-AzCli identity show -n $Identity -g $Rg --query id -o tsv
$PrincipalId = Invoke-AzCli identity show -n $Identity -g $Rg --query principalId -o tsv
$AcrId       = Invoke-AzCli acr show -n $Acr -g $Rg --query id -o tsv
# The apps' identity can only PULL images (least privilege)
$pullIds = Invoke-AzCli role assignment list --assignee $PrincipalId --role AcrPull --scope $AcrId --query "[].id" -o tsv
if (-not $pullIds) {
    Invoke-AzCli role assignment create --assignee-object-id $PrincipalId --assignee-principal-type ServicePrincipal `
        --role AcrPull --scope $AcrId -o none
}

Write-Host "== 3/7 Managed PostgreSQL (5 to 10 minutes on creation)" -ForegroundColor Cyan
# --public-access 0.0.0.0: only Azure services pass the firewall (better: VNet + private endpoint)
if (Get-AzOrNull postgres flexible-server show -g $Rg -n $Pg --query name -o tsv) {
    # Existing server: apply this run's password
    Invoke-AzCli postgres flexible-server update -g $Rg -n $Pg --admin-password $DbPassword -o none
} else {
    Invoke-AzCli postgres flexible-server create -g $Rg -n $Pg -l $Location `
        --tier Burstable --sku-name Standard_B1ms --storage-size 32 --version 16 `
        --admin-user $DbUser --admin-password $DbPassword --public-access 0.0.0.0 --yes -o none
}
$dbNames = Invoke-AzCli postgres flexible-server db list -g $Rg --server-name $Pg --query "[].name" -o tsv
if (-not ($dbNames -contains $DbName)) {
    Invoke-AzCli postgres flexible-server db create --resource-group $Rg --server-name $Pg --name $DbName -o none
}
$DatabaseUrl = "postgresql://${DbUser}:${DbPassword}@$Pg.postgres.database.azure.com:5432/${DbName}?sslmode=require"

Write-Host "== 4/7 Build and push the images" -ForegroundColor Cyan
$Registry = "$Acr.azurecr.io"
# Unique tag per run, so Azure always pulls the new image
$Tag = Get-Date -Format "yyyyMMddHHmmss"
Invoke-AzCli acr login -n $Acr | Out-Null
docker build -t "$Registry/meteo-backend:$Tag" --build-arg APP_VERSION=$Tag ./backend
if ($LASTEXITCODE -ne 0) { throw "FAILED: build backend" }
docker push "$Registry/meteo-backend:$Tag"
if ($LASTEXITCODE -ne 0) { throw "FAILED: push backend" }
docker build -t "$Registry/meteo-frontend:$Tag" ./frontend
if ($LASTEXITCODE -ne 0) { throw "FAILED: build frontend" }
docker push "$Registry/meteo-frontend:$Tag"
if ($LASTEXITCODE -ne 0) { throw "FAILED: push frontend" }

Write-Host "== 5/7 Container Apps environment (+ Log Analytics)" -ForegroundColor Cyan
if (-not (Get-AzOrNull containerapp env show -n $CaeEnv -g $Rg --query name -o tsv)) {
    Invoke-AzCli containerapp env create -n $CaeEnv -g $Rg -l $Location -o none
}

Write-Host "== 6/7 Backend (internal ingress: not reachable from the Internet)" -ForegroundColor Cyan
$DeployedAt = $Tag   # changes every run: forces a new revision
Deploy-ContainerApp -Name "meteo-backend" -Create {
    # Max 8 replicas: PostgreSQL B1ms connection budget (35) / 4 connections per replica
    Invoke-AzCli containerapp create -n meteo-backend -g $Rg --environment $CaeEnv `
        --image "$Registry/meteo-backend:$Tag" `
        --registry-server $Registry --registry-identity $IdentityId --user-assigned $IdentityId `
        --ingress internal --target-port 8000 `
        --cpu 0.5 --memory 1Gi --min-replicas 1 --max-replicas 8 `
        --scale-rule-name http-concurrency --scale-rule-type http --scale-rule-http-concurrency 10 `
        --secrets "database-url=$DatabaseUrl" `
        --env-vars "DATABASE_URL=secretref:database-url" "APP_ENV=azure" "LOG_LEVEL=INFO" "DEPLOYED_AT=$DeployedAt" `
        -o none
} -Update {
    # Existing app: store the new password, then a new revision reads it
    Invoke-AzCli containerapp secret set -n meteo-backend -g $Rg --secrets "database-url=$DatabaseUrl" -o none
    Invoke-AzCli containerapp update -n meteo-backend -g $Rg --image "$Registry/meteo-backend:$Tag" `
        --set-env-vars "DEPLOYED_AT=$DeployedAt" -o none
}
$BackendFqdn = Invoke-AzCli containerapp show -n meteo-backend -g $Rg --query properties.configuration.ingress.fqdn -o tsv

Write-Host "== 7/7 Frontend (external ingress: public HTTPS URL)" -ForegroundColor Cyan
Deploy-ContainerApp -Name "meteo-frontend" -Create {
    Invoke-AzCli containerapp create -n meteo-frontend -g $Rg --environment $CaeEnv `
        --image "$Registry/meteo-frontend:$Tag" `
        --registry-server $Registry --registry-identity $IdentityId --user-assigned $IdentityId `
        --ingress external --target-port 8080 `
        --cpu 0.25 --memory 0.5Gi --min-replicas 1 --max-replicas 5 `
        --scale-rule-name http-concurrency --scale-rule-type http --scale-rule-http-concurrency 50 `
        --env-vars "BACKEND_URL=https://$BackendFqdn" "DEPLOYED_AT=$DeployedAt" -o none
} -Update {
    Invoke-AzCli containerapp update -n meteo-frontend -g $Rg --image "$Registry/meteo-frontend:$Tag" `
        --set-env-vars "BACKEND_URL=https://$BackendFqdn" "DEPLOYED_AT=$DeployedAt" -o none
}
$FrontendFqdn = Invoke-AzCli containerapp show -n meteo-frontend -g $Rg --query properties.configuration.ingress.fqdn -o tsv

Write-Host "`nApplication : https://$FrontendFqdn" -ForegroundColor Green
Write-Host "Registry    : $Registry"
Write-Host "Next (once): .\infra\github-oidc-setup.ps1 for automatic deployments"
