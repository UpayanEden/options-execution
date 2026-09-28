"""
Gunicorn production configuration for the Iron Condor web dashboard.

Bind:     0.0.0.0:8000
Workers:  2 (suitable for a read-heavy dashboard; increase if needed)
Logging:  Access and error logs to logs/gunicorn/

Usage::

    gunicorn -c deploy/gunicorn.conf.py web.app:app
"""
import multiprocessing
from pathlib import Path

# ── Binding ────────────────────────────────────────────────────────────────

bind = "0.0.0.0:5051"
"""Address and port to listen on. Matches Docker EXPOSE 5051."""

# ── Workers ────────────────────────────────────────────────────────────────

workers = 2
"""
Number of worker processes. Dashboard is I/O-bound (SQLite + file reads)
so 2 workers is sufficient. Set to (2 × CPU cores + 1) for heavier load.
"""

worker_class = "sync"
"""
Worker class. Use 'sync' for simple dashboard. Switch to 'gevent' or
'gthread' if async behaviour is needed (requires extra packages).
"""

threads = 2
"""Threads per worker for the sync worker class."""

worker_connections = 100
"""Maximum concurrent connections per worker (for async workers)."""

# ── Timeouts ───────────────────────────────────────────────────────────────

timeout = 120
"""
Worker timeout in seconds. Dashboard reads from files/SQLite — should
be fast. 120s gives plenty of headroom for large log files.
"""

keepalive = 5
"""Seconds to keep an idle keep-alive connection open."""

graceful_timeout = 30
"""Seconds to wait for workers to finish after receiving SIGTERM."""

# ── Process naming ─────────────────────────────────────────────────────────

proc_name = "condor_live_dashboard"
"""Process name shown in ps/top."""

# ── Logging ────────────────────────────────────────────────────────────────

_log_dir = Path("logs/gunicorn")
_log_dir.mkdir(parents=True, exist_ok=True)

accesslog = str(_log_dir / "access.log")
"""Path to access log file. Set to '-' to write to stdout."""

errorlog = str(_log_dir / "error.log")
"""Path to error log file. Set to '-' to write to stderr."""

loglevel = "info"
"""Log level: debug, info, warning, error, critical."""

access_log_format = (
    '%(h)s %(l)s %(u)s %(t)s "%(r)s" %(s)s %(b)s '
    '"%(f)s" "%(a)s" %(D)sµs'
)
"""Combined log format with request duration in microseconds."""

# ── Security ───────────────────────────────────────────────────────────────

limit_request_line = 4096
"""Maximum size of HTTP request line."""

limit_request_fields = 100
"""Maximum number of HTTP request headers."""

limit_request_field_size = 8190
"""Maximum size of each HTTP request header."""

# ── Server hooks ───────────────────────────────────────────────────────────


def on_starting(server):
    """Called just before the master process is initialised."""
    server.log.info("Condor Live Dashboard starting up (gunicorn)")


def on_exit(server):
    """Called just before gunicorn exits."""
    server.log.info("Condor Live Dashboard shutting down")


def worker_int(worker):
    """Called when a worker receives SIGINT."""
    worker.log.info("Worker interrupted (SIGINT)")


def worker_abort(worker):
    """Called when a worker receives SIGABRT (e.g. timeout)."""
    worker.log.info("Worker aborted (timeout exceeded)")
