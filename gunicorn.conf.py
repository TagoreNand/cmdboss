"""
CMDBoss — Gunicorn Configuration
=================================
Production WSGI/ASGI server settings for CMDBoss.
Uses Uvicorn workers to serve the FastAPI async application.

Reference: https://docs.gunicorn.org/en/stable/settings.html
"""

import multiprocessing

# ---------------------------------------------------------------------------
# Server socket
# ---------------------------------------------------------------------------
bind = "0.0.0.0:8000"
backlog = 2048

# ---------------------------------------------------------------------------
# Workers
# ---------------------------------------------------------------------------
# Recommended: (2 x CPU cores) + 1  for I/O-bound async apps
workers = (multiprocessing.cpu_count() * 2) + 1
worker_class = "uvicorn.workers.UvicornWorker"
worker_connections = 1000
timeout = 30
keepalive = 5

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
loglevel = "info"
accesslog = "-"          # stdout
errorlog = "-"           # stdout
access_log_format = '%(h)s %(l)s %(u)s %(t)s "%(r)s" %(s)s %(b)s "%(f)s" "%(a)s" %(D)s'

# ---------------------------------------------------------------------------
# Process naming
# ---------------------------------------------------------------------------
proc_name = "cmdboss"

# ---------------------------------------------------------------------------
# Security
# ---------------------------------------------------------------------------
limit_request_line = 4096
limit_request_fields = 100
limit_request_field_size = 8190

# ---------------------------------------------------------------------------
# Graceful shutdown
# ---------------------------------------------------------------------------
graceful_timeout = 30
