#!/bin/sh
# Renders the config from environment variables, then starts Prometheus.
# Remote write receiver: k6 sends its load test metrics here.
# PROMETHEUS_PASSWORD_HASH (bcrypt, from a secret) turns on basic auth (Azure); unset = no auth (local).
set -eu
sed -e "s#__PUSHGATEWAY_TARGET__#${PUSHGATEWAY_TARGET}#" \
    -e "s#__PUSHGATEWAY_SCHEME__#${PUSHGATEWAY_SCHEME}#" \
    /etc/prometheus/prometheus.yml.tpl > /prometheus/prometheus.yml
# cAdvisor (local only): container metrics read from the Docker host
if [ -n "${CADVISOR_TARGET:-}" ]; then
    printf '\n  - job_name: cadvisor\n    static_configs:\n      - targets: ["%s"]\n' "${CADVISOR_TARGET}" \
        >> /prometheus/prometheus.yml
fi

set -- --config.file=/prometheus/prometheus.yml \
       --storage.tsdb.path=/prometheus \
       --storage.tsdb.retention.time="${RETENTION}" \
       --web.enable-remote-write-receiver
if [ -n "${PROMETHEUS_PASSWORD_HASH:-}" ]; then
    printf "basic_auth_users:\n  %s: '%s'\n" "${PROMETHEUS_USER:-prometheus}" "${PROMETHEUS_PASSWORD_HASH}" \
        > /prometheus/web.yml
    set -- "$@" --web.config.file=/prometheus/web.yml
fi
exec /bin/prometheus "$@"
