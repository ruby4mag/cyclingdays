"""Structured logging built on the standard library.

* ``json`` format emits one JSON object per line (for unattended operation and
  log shipping); ``console`` is a human-readable variant for development.
* Extra fields passed via ``logger.info("msg", extra={...})`` become top-level
  keys.
* A per-request ``request_id`` (set by the HTTP middleware) is attached
  automatically via a context variable.
* Keys that look like secrets are redacted.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import sys
from contextvars import ContextVar
from typing import Any, TextIO

from app.config import Settings

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)

_RESERVED_ATTRS = frozenset(
    logging.LogRecord("", 0, "", 0, "", (), None).__dict__.keys()
    | {"message", "asctime", "color_message"}  # color_message: uvicorn's ANSI duplicate
)
_SECRET_MARKERS = ("password", "secret", "token", "api_key", "apikey", "authorization")
REDACTED = "***REDACTED***"
# Exact keys that contain a marker but are not secrets (LLM token *counts*).
_NOT_SECRET = frozenset({"input_tokens", "output_tokens", "total_tokens", "reasoning_tokens"})


def _is_secret_key(key: str) -> bool:
    lowered = key.lower()
    if lowered in _NOT_SECRET:
        return False
    return any(marker in lowered for marker in _SECRET_MARKERS)


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: (REDACTED if _is_secret_key(str(k)) else _redact(v)) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_redact(v) for v in value]
    return value


def redact_secrets(value: Any) -> Any:
    """Return a copy of ``value`` with secret-like dict keys redacted (recursive).

    Also used to sanitise third-party payloads before they are stored, e.g. the
    Intervals.icu athlete object contains ``icu_api_key``.
    """
    return _redact(value)


def _extra_fields(record: logging.LogRecord) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    for key, value in record.__dict__.items():
        if key in _RESERVED_ATTRS or key.startswith("_"):
            continue
        fields[key] = REDACTED if _is_secret_key(key) else _redact(value)
    return fields


class JsonFormatter(logging.Formatter):
    """Render log records as single-line JSON."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": dt.datetime.fromtimestamp(record.created, tz=dt.UTC).isoformat(
                timespec="milliseconds"
            ),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        request_id = request_id_var.get()
        if request_id:
            payload["request_id"] = request_id
        payload.update(_extra_fields(record))
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


class ConsoleFormatter(logging.Formatter):
    """Human-readable formatter that still shows structured extras."""

    def __init__(self) -> None:
        super().__init__("%(asctime)s %(levelname)-8s %(name)s: %(message)s")

    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        extras = _extra_fields(record)
        request_id = request_id_var.get()
        if request_id:
            extras["request_id"] = request_id
        if extras:
            base += " " + " ".join(f"{k}={v!r}" for k, v in sorted(extras.items()))
        return base


def configure_logging(settings: Settings, stream: TextIO | None = None) -> None:
    """Configure the root logger. Safe to call more than once.

    The service logs to stdout; CLI scripts pass ``sys.stderr`` so their
    stdout carries only the machine-readable result.
    """
    handler = logging.StreamHandler(stream or sys.stdout)
    handler.setFormatter(JsonFormatter() if settings.log_format == "json" else ConsoleFormatter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(settings.log_level)

    # Route uvicorn through our handler; request logging is done by our middleware.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uv_logger = logging.getLogger(name)
        uv_logger.handlers.clear()
        uv_logger.propagate = True
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    # The HTTP client logs every request at INFO; our Intervals client already
    # logs requests with retry context (and never the Authorization header).
    for name in ("httpx", "httpx2", "httpcore", "httpcore2"):
        logging.getLogger(name).setLevel(logging.WARNING)
    # Alembic is imported at startup only to read the migration head revision.
    logging.getLogger("alembic").setLevel(logging.WARNING)
    logging.getLogger("sqlalchemy.engine").setLevel(
        logging.INFO if settings.db_echo else logging.WARNING
    )
