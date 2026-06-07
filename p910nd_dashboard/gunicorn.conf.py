# gunicorn.conf.py — Konfigurasi Gunicorn untuk USB Printer Server v3
# Referensi: https://docs.gunicorn.org/en/stable/settings.html

import multiprocessing
import os

# ── Binding ───────────────────────────────────────────────────────────────────
# Bind ke localhost saja; Nginx/Caddy bertindak sebagai reverse proxy
bind = "0.0.0.0:8070"

# ── Workers ───────────────────────────────────────────────────────────────────
# Untuk 100+ user dengan workload I/O-bound (printer, DB):
# Gunakan gevent workers untuk concurrency tinggi tanpa banyak memory
# Formula fallback: 2 * CPU + 1 untuk sync workers
worker_class = "gevent"
worker_connections = 200       # max concurrent connections per worker

# Jumlah workers: 2-4 cukup untuk USB printer server
# (bottleneck ada di printer, bukan CPU)
# Set via env untuk fleksibilitas di berbagai hardware
workers = int(os.environ.get("GUNICORN_WORKERS", 4))

# ── Timeouts ─────────────────────────────────────────────────────────────────
timeout          = 60     # Worker yang hang > 60s akan di-kill & restart
graceful_timeout = 30     # Waktu graceful shutdown
keepalive        = 5      # HTTP keep-alive seconds

# ── Paths ─────────────────────────────────────────────────────────────────────
pidfile    = "/run/usb-printer/gunicorn.pid"
accesslog  = "-"          # stdout → systemd journal
errorlog   = "-"          # stderr → systemd journal
loglevel   = "info"
access_log_format = '%(h)s %(l)s %(u)s %(t)s "%(r)s" %(s)s %(b)s %(D)sµs'

# ── Security ──────────────────────────────────────────────────────────────────
limit_request_line   = 4094
limit_request_fields = 100
limit_request_field_size = 8190

# ── Performance ───────────────────────────────────────────────────────────────
preload_app  = True    # Load app sebelum fork → hemat memory (copy-on-write)
max_requests = 1000    # Restart worker setelah N requests (cegah memory leak)
max_requests_jitter = 100  # Random jitter untuk menghindari restart bersamaan

# ── Callbacks ────────────────────────────────────────────────────────────────

def on_starting(server):
    server.log.info("USB Printer Server v3 starting...")


def post_fork(server, worker):
    # Reset DB connection setelah fork (SQLite thread-safety)
    server.log.info(f"Worker spawned (pid: {worker.pid})")


def worker_exit(server, worker):
    server.log.info(f"Worker exiting (pid: {worker.pid})")


def on_exit(server):
    server.log.info("USB Printer Server stopping.")
