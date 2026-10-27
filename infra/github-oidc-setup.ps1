
$ErrorActionPreference = "Stop"

$GitHubRepo  = "koussayMed/alternance-SG"  
$Environment = "production"               
$GitHubSubject = ""
$Suffix      = "koussay"
$Rg          = "rg-sre-meteo"
$Acr         = "acrsremeteo$Suffix"
$AppName     = "gh-sre-meteo-deploy"    

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

function Grant-Role {
    param([string]$PrincipalId, [string]$Role, [string]$Scope)
    $ids = Get-AzOrNull role assignment list --assignee $PrincipalId --role $Role --scope $Scope --query "[].id" -o tsv
    if ($ids) { Write-Host " ${Role}: already granted"; return }
    for ($attempt = 1; $attempt -le 5; $attempt++) {
        $ok = Get-AzOrNull role assignment create --assignee-object-id $PrincipalId `
            --assignee-principal-type ServicePrincipal --role $Role --scope $Scope -o none
        if ($LASTEXITCODE -eq 0) { Write-Host "  ${Role}: granted"; return }
        Write-Host "  ${Role}: principal not visible yet, retrying in 15 s" -ForegroundColor Yellow
        Start-Sleep -Seconds 15
    }
    throw "FAILED: role assignment $Role"
}

if (-not $GitHubSubject) {
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    try {
        $repoInfo = Invoke-RestMethod -Uri "https://api.github.com/repos/$GitHubRepo" `
            -Headers @{ "User-Agent" = "sre-meteo-setup" } -TimeoutSec 20
    } catch {
        throw "Repo $GitHubRepo not found on the GitHub API: wrong name or private repo. Set `$GitHubSubject."
    }
    $GitHubSubject = "repo:$($repoInfo.owner.login)@$($repoInfo.owner.id)/$($repoInfo.name)@$($repoInfo.id):environment:$Environment"
}
Write-Host "Expected subject: $GitHubSubject"

Write-Host "== 1/4 Subscription and resources" -ForegroundColor Cyan
$SubscriptionId = Invoke-AzCli account show --query id -o tsv
$TenantId       = Invoke-AzCli account show --query tenantId -o tsv
$RgId           = Invoke-AzCli group show -n $Rg --query id -o tsv
$AcrId          = Invoke-AzCli acr show -n $Acr -g $Rg --query id -o tsv

Write-Host "== 2/4 Pipeline Entra ID app (no secret)" -ForegroundColor Cyan
$AppId = Invoke-AzCli ad app list --display-name $AppName --query "[0].appId" -o tsv
if (-not $AppId) {
    $AppId = Invoke-AzCli ad app create --display-name $AppName --query appId -o tsv
}
$SpId = Get-AzOrNull ad sp show --id $AppId --query id -o tsv
if (-not $SpId) {
    $SpId = Invoke-AzCli ad sp create --id $AppId --query id -o tsv
}

Write-Host "== 3/4 Trust GitHub: repo $GitHubRepo, environment $Environment" -ForegroundColor Cyan
$CredName = "github-$Environment"
# Current subject (lines "name<TAB>subject"): replace it if it changed (new repo)
$currentSubject = $null
foreach ($line in @(Invoke-AzCli ad app federated-credential list --id $AppId --query "[].[name,subject]" -o tsv)) {
    $parts = "$line" -split "`t"
    if ($parts[0] -eq $CredName) { $currentSubject = $parts[1] }
}
if ($currentSubject -and $currentSubject -ne $GitHubSubject) {
    Write-Host "  replacing old subject $currentSubject" -ForegroundColor Yellow
    Invoke-AzCli ad app federated-credential delete --id $AppId --federated-credential-id $CredName -o none
    $currentSubject = $null
}
if ($currentSubject) {
    Write-Host "  already set"
} else {
    $credential = @{
        name        = $CredName
        issuer      = "https://token.actions.githubusercontent.com"
        subject     = $GitHubSubject
        audiences   = @("api://AzureADTokenExchange")
        description = "GitHub Actions deployments of $GitHubRepo"
    } | ConvertTo-Json
    # JSON goes through a file (az.cmd mangles it as an argument); UTF-8 without BOM
    $file = Join-Path $env:TEMP "sre-meteo-federated-credential.json"
    [System.IO.File]::WriteAllText($file, $credential)
    try {
        Invoke-AzCli ad app federated-credential create --id $AppId --parameters $file -o none
    } finally {
        Remove-Item $file -ErrorAction SilentlyContinue
    }
    Write-Host "  created"
}

Write-Host "== 4/4 Pipeline permissions" -ForegroundColor Cyan
Grant-Role -PrincipalId $SpId -Role "AcrPush" -Scope $AcrId
Grant-Role -PrincipalId $SpId -Role "Contributor" -Scope $RgId

Write-Host ""
Write-Host "Done. Add in GitHub: Settings > Secrets and variables > Actions > New repository secret" -ForegroundColor Green
Write-Host "  AZURE_CLIENT_ID       = $AppId"
Write-Host "  AZURE_TENANT_ID       = $TenantId"
Write-Host "  AZURE_SUBSCRIPTION_ID = $SubscriptionId"
Write-Host "These are identifiers, not passwords: without a token from the right repo they grant nothing."
