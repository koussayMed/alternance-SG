# Prometheus with its config baked in (rendered at startup from the environment)
FROM prom/prometheus:v3.15.0

COPY prometheus.yml.tpl /etc/prometheus/prometheus.yml.tpl
COPY entrypoint.sh /etc/prometheus/entrypoint.sh

ENV PUSHGATEWAY_TARGET=pushgateway:9091 \
    PUSHGATEWAY_SCHEME=http \
    RETENTION=7d

# Non-root user of the official image
USER nobody
EXPOSE 9090
HEALTHCHECK --interval=10s --timeout=3s --retries=5 CMD wget -qO- http://localhost:9090/-/ready || exit 1
ENTRYPOINT ["/bin/sh", "/etc/prometheus/entrypoint.sh"]
