FROM nginxinc/nginx-unprivileged:1.31-alpine

USER root
RUN apk upgrade --no-cache
USER 101

COPY nginx/default.conf.template /etc/nginx/templates/default.conf.template
COPY public/ /usr/share/nginx/html/

ENV BACKEND_URL=http://backend:8000

EXPOSE 8080

HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
  CMD wget -q -O /dev/null http://127.0.0.1:8080/healthz || exit 1
