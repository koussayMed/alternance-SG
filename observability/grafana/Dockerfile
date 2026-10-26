FROM grafana/grafana:13.2.3

COPY grafana.ini /etc/grafana/grafana.ini
COPY provisioning /etc/grafana/provisioning
COPY dashboards /etc/grafana/dashboards

ENV PROMETHEUS_URL=http://prometheus:9090

# Non-root user of the official image
USER 472
EXPOSE 3000
HEALTHCHECK --interval=10s --timeout=3s --retries=10 CMD wget -qO- http://localhost:3000/api/health || exit 1
