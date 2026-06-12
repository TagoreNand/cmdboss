"""
Observability: structured logging, request-id propagation, and metrics hooks.

- Logging is JSON-structured (python-json-logger) when ``log_json`` is enabled,
  otherwise a readable console format. A ``request_id`` is injected into every
  record via a :class:`contextvars.ContextVar`, so logs correlate across the
  async call stack without threading the id through every function.
- Metrics are exposed through a tiny backend-agnostic facade. When
  ``prometheus_client`` is installed and metrics are enabled the facade emits
  real counters/histograms and ``render_metrics`` returns the exposition text;
  otherwise every call is a cheap no-op. This keeps Prometheus an *optional*
  dependency while giving production deployments first-class instrumentation.
"""

from __future__ import annotations

import contextvars
import logging
import sys
import time
import uuid

# --------------------------------------------------------------------------- #
# Request-id context
# --------------------------------------------------------------------------- #

_request_id_ctx: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "cmdboss_request_id", default=None
)


def set_request_id(request_id: str | None) -> str:
    rid = request_id or uuid.uuid4().hex
    _request_id_ctx.set(rid)
    return rid


def get_request_id() -> str | None:
    return _request_id_ctx.get()


# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #


class _RequestIdFilter(logging.Filter):
    """Attach the current request id (or '-') to every record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = get_request_id() or "-"
        return True


_LOGGING_CONFIGURED = False


def configure_logging(level: str = "INFO", json_output: bool = True) -> None:
    global _LOGGING_CONFIGURED
    root = logging.getLogger()
    root.setLevel(level)

    # Idempotent: clear our own handlers so repeated calls (tests, reload) don't stack.
    for h in list(root.handlers):
        root.removeHandler(h)

    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(_RequestIdFilter())

    if json_output:
        try:
            from pythonjsonlogger import jsonlogger

            fmt = jsonlogger.JsonFormatter(
                "%(asctime)s %(levelname)s %(name)s %(request_id)s %(message)s",
                rename_fields={"asctime": "ts", "levelname": "level", "name": "logger"},
            )
        except Exception:  # pragma: no cover - fallback if dep missing
            fmt = logging.Formatter(
                "%(asctime)s | %(levelname)-8s | %(name)s | rid=%(request_id)s | %(message)s"
            )
    else:
        fmt = logging.Formatter(
            "%(asctime)s | %(levelname)-8s | %(name)s | rid=%(request_id)s | %(message)s"
        )

    handler.setFormatter(fmt)
    root.addHandler(handler)
    _LOGGING_CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    if not _LOGGING_CONFIGURED:
        configure_logging()
    return logging.getLogger(name)


# --------------------------------------------------------------------------- #
# Metrics facade
# --------------------------------------------------------------------------- #


class _Metrics:
    """Backend-agnostic metrics facade. No-op unless prometheus_client is present."""

    def __init__(self) -> None:
        self._enabled = False
        self._counters: dict = {}
        self._histograms: dict = {}
        self._client = None

    def enable(self) -> None:
        try:
            import prometheus_client  # noqa: F401

            self._client = prometheus_client
            self._enabled = True
            self._register()
        except Exception:  # pragma: no cover - optional dependency
            self._enabled = False

    def _register(self) -> None:
        c = self._client
        self._counters["http_requests_total"] = c.Counter(
            "cmdboss_http_requests_total", "HTTP requests", ["method", "path", "status"]
        )
        self._counters["ci_operations_total"] = c.Counter(
            "cmdboss_ci_operations_total", "CI operations", ["type", "operation"]
        )
        self._counters["events_published_total"] = c.Counter(
            "cmdboss_events_published_total", "Events published", ["event_type"]
        )
        self._counters["events_dropped_total"] = c.Counter(
            "cmdboss_events_dropped_total", "Events dropped due to a full queue", []
        )
        self._histograms["http_request_duration_seconds"] = c.Histogram(
            "cmdboss_http_request_duration_seconds", "Request latency", ["method", "path"]
        )

    def incr(self, name: str, labels: dict | None = None, amount: float = 1.0) -> None:
        if not self._enabled:
            return
        metric = self._counters.get(name)
        if metric is None:
            return
        try:
            (metric.labels(**labels) if labels else metric).inc(amount)
        except Exception:  # pragma: no cover - never let metrics break a request
            pass

    def observe(self, name: str, value: float, labels: dict | None = None) -> None:
        if not self._enabled:
            return
        metric = self._histograms.get(name)
        if metric is None:
            return
        try:
            (metric.labels(**labels) if labels else metric).observe(value)
        except Exception:  # pragma: no cover
            pass

    @property
    def enabled(self) -> bool:
        return self._enabled

    def render(self) -> tuple[bytes, str]:
        if not self._enabled or self._client is None:
            return b"", "text/plain"
        return self._client.generate_latest(), self._client.CONTENT_TYPE_LATEST


metrics = _Metrics()


class Timer:
    """Context manager that records an elapsed-time observation."""

    def __init__(self, name: str, labels: dict | None = None) -> None:
        self._name = name
        self._labels = labels
        self._start = 0.0

    def __enter__(self) -> Timer:
        self._start = time.perf_counter()
        return self

    def __exit__(self, *exc) -> None:
        metrics.observe(self._name, time.perf_counter() - self._start, self._labels)
