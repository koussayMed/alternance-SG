set -u
TRIVY_IMAGE="aquasec/trivy:0.69.3"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
trivy() {
  docker run --rm \
    -v /var/run/docker.sock:/var/run/docker.sock \
    -v trivy-cache:/root/.cache/ \
    -v "$ROOT:/src:ro" \
    "$TRIVY_IMAGE" "$@"
}

failed=0
for image in sre-meteo-backend sre-meteo-frontend; do
  printf "\n%s\n" "=== Vulnerabilities: $image (fails on fixable HIGH/CRITICAL) ==="
  trivy image --scanners vuln --severity HIGH,CRITICAL --ignore-unfixed \
    --ignorefile /src/.trivyignore --exit-code 1 "$image" || failed=1
done

printf "\n%s\n" "=== Dockerfile misconfigurations ==="
trivy config --severity HIGH,CRITICAL --exit-code 1 /src || failed=1

printf "\n%s\n" "=== Committed secrets ==="
trivy fs --scanners secret --skip-dirs /src/backend/.venv --skip-files /src/.env --exit-code 1 /src || failed=1

[ "$failed" -eq 0 ] && printf "\nOK: nothing blocking.\n" || { printf "\nFAILED: see above.\n"; exit 1; }
