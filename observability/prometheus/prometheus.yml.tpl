# Prometheus config, rendered at startup by entrypoint.sh: __PUSHGATEWAY_*__ come from the environment
# (local: pushgateway:9091 over http; Azure: the Pushgateway internal FQDN).
global:
  scrape_interval: 15s
  evaluation_interval: 15s

scrape_configs:
  # Metrics pushed by each backend process (see backend/app/metrics.py)
  - job_name: pushgateway
    honor_labels: true # keep the pushed labels (job, replica, worker)
    scheme: __PUSHGATEWAY_SCHEME__
    static_configs:
      - targets: ["__PUSHGATEWAY_TARGET__"]

  - job_name: prometheus
    static_configs:
      - targets: ["localhost:9090"]
