import os

bind = f"0.0.0.0:{os.environ.get('PORT', '8000')}" r
worker_class = "gthread"
workers = int(os.environ.get("GUNICORN_WORKERS", "2"))
threads = int(os.environ.get("GUNICORN_THREADS", "4"))  
timeout = 30
graceful_timeout = 20
keepalive = 5

max_requests = 1000
max_requests_jitter = 100

worker_tmp_dir = "/dev/shm" 
accesslog = None 
errorlog = "-"
loglevel = os.environ.get("LOG_LEVEL", "info").lower()
