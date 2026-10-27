"""gunicorn settings, overridable by environment variables."""

import os

bind = f"0.0.0.0:{os.environ.get('PORT', '8000')}"  # noqa: S104 - listen inside the container
worker_class = "gthread"
workers = int(os.environ.get("GUNICORN_WORKERS", "2"))
threads = int(os.environ.get("GUNICORN_THREADS", "4"))  # threads per worker

timeout = 30
# Container Apps sends SIGTERM, then SIGKILL 30 s later: finish in-flight requests first
graceful_timeout = 20
keepalive = 5

# Recycle workers now and then (limits the impact of a memory leak)
max_requests = 1000
max_requests_jitter = 100

worker_tmp_dir = "/dev/shm"  # noqa: S108 - worker heartbeat in memory, not on disk
accesslog = None  # the app writes its own JSON access log
errorlog = "-"
loglevel = os.environ.get("LOG_LEVEL", "info").lower()