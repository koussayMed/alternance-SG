$TrivyImage = "aquasec/trivy:0.69.3"
$Root = (Resolve-Path "$PSScriptRoot\..").Path
$Common = @("run", "--rm",
    "-v", "/var/run/docker.sock:/var/run/docker.sock",
    "-v", "trivy-cache:/root/.cache/",
    "-v", "${Root}:/src:ro",
    $TrivyImage)

$failed = $false
foreach ($image in @("sre-meteo-backend", "sre-meteo-frontend")) {
    Write-Host "`n=== Vulnerabilities: $image (fails on fixable HIGH/CRITICAL) ===" -ForegroundColor Cyan
    docker @Common image --scanners vuln --severity HIGH,CRITICAL --ignore-unfixed `
        --ignorefile /src/.trivyignore --exit-code 1 $image
    if ($LASTEXITCODE -ne 0) { $failed = $true }
}

Write-Host "`n=== Dockerfile misconfigurations ===" -ForegroundColor Cyan
docker @Common config --severity HIGH,CRITICAL --exit-code 1 /src
if ($LASTEXITCODE -ne 0) { $failed = $true }

Write-Host "`n=== Committed secrets ===" -ForegroundColor Cyan
docker @Common fs --scanners secret --skip-dirs /src/backend/.venv --skip-files /src/.env --exit-code 1 /src
if ($LASTEXITCODE -ne 0) { $failed = $true }

if ($failed) { Write-Host "`nFAILED: see above." -ForegroundColor Red; exit 1 }
Write-Host "`nOK: nothing blocking." -ForegroundColor Green
