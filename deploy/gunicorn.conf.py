"""Gunicorn configuration for the Notebooks app.

Used by deploy/systemd/notebooks.service. The socket is NOT configured here --
it is created by notebooks.socket and inherited through systemd socket
activation, which is what lets systemd give it the right owner and group
without granting nginx broader access.
"""

# Worker count is deliberately low rather than the usual (2 x cores) + 1.
# The database is SQLite: every extra worker is another potential concurrent
# writer, and past a handful the contention costs more than the parallelism
# buys. Three comfortably covers a small self-hosted instance.
workers = 3
worker_class = 'sync'

# Generous enough for a slow first request after a cold start, short enough
# that a genuinely wedged worker gets recycled.
timeout = 60
graceful_timeout = 30
keepalive = 5

# Periodic worker recycling. Bounds any slow leak, and incidentally refreshes
# the per-process mtime cache in notebooks/templatetags/vstatic.py, so a static
# rebuild is picked up without a restart.
max_requests = 1000
max_requests_jitter = 100

# '-' means stdout/stderr. Under systemd that is captured by journald, which
# already handles rotation and retention: `journalctl -u notebooks`.
accesslog = '-'
errorlog = '-'
loglevel = 'info'

# %({x-forwarded-for}i)s rather than %(h)s: behind nginx every connection
# appears to come from the unix socket, so the real client IP is only in the
# forwarded header.
access_log_format = '%({x-forwarded-for}i)s %(m)s %(U)s %(s)s %(L)ss'

proc_name = 'notebooks'
