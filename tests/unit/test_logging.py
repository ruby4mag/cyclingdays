from __future__ import annotations

import json
import logging

import pytest

from app.logging_config import (
    REDACTED,
    ConsoleFormatter,
    JsonFormatter,
    redact_secrets,
    request_id_var,
)


def _record(msg: str = "hello", **extra: object) -> logging.LogRecord:
    record = logging.LogRecord("test.logger", logging.INFO, __file__, 1, msg, (), None)
    for key, value in extra.items():
        setattr(record, key, value)
    return record


def test_json_formatter_emits_core_fields_and_extras() -> None:
    out = json.loads(JsonFormatter().format(_record(athlete_id=7, path="/health")))
    assert out["message"] == "hello"
    assert out["level"] == "INFO"
    assert out["logger"] == "test.logger"
    assert out["timestamp"].endswith("+00:00")
    assert out["athlete_id"] == 7
    assert out["path"] == "/health"


def test_json_formatter_includes_request_id_from_context() -> None:
    token = request_id_var.set("req-123")
    try:
        out = json.loads(JsonFormatter().format(_record()))
    finally:
        request_id_var.reset(token)
    assert out["request_id"] == "req-123"


@pytest.mark.parametrize("key", ["password", "api_key", "intervals_token", "Authorization"])
def test_secret_like_keys_are_redacted(key: str) -> None:
    out = json.loads(JsonFormatter().format(_record(**{key: "s3cr3t"})))
    assert out[key] == REDACTED


def test_nested_secrets_are_redacted() -> None:
    out = json.loads(
        JsonFormatter().format(_record(headers={"Authorization": "Basic abc", "accept": "json"}))
    )
    assert out["headers"] == {"Authorization": REDACTED, "accept": "json"}


def test_uvicorn_color_message_is_dropped() -> None:
    out = json.loads(JsonFormatter().format(_record(color_message="\x1b[36mhi\x1b[0m")))
    assert "color_message" not in out


def test_exception_is_serialised() -> None:
    try:
        raise ValueError("boom")
    except ValueError:
        import sys

        record = logging.LogRecord("t", logging.ERROR, __file__, 1, "failed", (), sys.exc_info())
    out = json.loads(JsonFormatter().format(record))
    assert "ValueError: boom" in out["exception"]


def test_console_formatter_shows_extras_and_redacts() -> None:
    line = ConsoleFormatter().format(_record(athlete_id=3, password="x"))
    assert "athlete_id=3" in line
    assert f"password='{REDACTED}'" in line


def test_token_counts_are_not_redacted() -> None:
    out = redact_secrets({"input_tokens": 10, "output_tokens": 5, "access_token": "x"})
    assert out == {"input_tokens": 10, "output_tokens": 5, "access_token": REDACTED}
