"""Phase 9: email notifier (SMTP replaced by a fake; no network)."""

from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage
from typing import Any, ClassVar

import pytest

from app.notifications import EmailNotifier, LogNotifier, make_notifier
from tests.conftest import make_unit_settings

EMAIL = {
    "notify_email_enabled": True,
    "notify_email_to": "rider@example.com, coach@example.com",
    "smtp_host": "smtp.example.com",
    "smtp_username": "sender@example.com",
    "smtp_password": "app-password-123",
}


class FakeSMTP:
    instances: ClassVar[list[FakeSMTP]] = []
    fail_with: ClassVar[Exception | None] = None

    def __init__(self, host: str, port: int, timeout: float, **kwargs: Any) -> None:
        self.host, self.port, self.timeout = host, port, timeout
        self.calls: list[str] = []
        self.sent: list[EmailMessage] = []
        FakeSMTP.instances.append(self)

    def __enter__(self) -> FakeSMTP:
        return self

    def __exit__(self, *exc: object) -> None:
        self.calls.append("quit")

    def starttls(self, context: Any = None) -> None:
        self.calls.append("starttls")

    def login(self, user: str, password: str) -> None:
        self.calls.append(f"login:{user}:{password}")

    def send_message(self, msg: EmailMessage) -> None:
        if FakeSMTP.fail_with is not None:
            raise FakeSMTP.fail_with
        self.sent.append(msg)


@pytest.fixture(autouse=True)
def fake_smtp(monkeypatch: pytest.MonkeyPatch) -> type[FakeSMTP]:
    FakeSMTP.instances = []
    FakeSMTP.fail_with = None
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSMTP)
    return FakeSMTP


def test_disabled_or_incomplete_settings_only_log() -> None:
    assert isinstance(make_notifier(make_unit_settings()), LogNotifier)
    incomplete = make_unit_settings(notify_email_enabled=True, smtp_host="smtp.example.com")
    notifier = make_notifier(incomplete)
    assert isinstance(notifier, LogNotifier)
    assert "NOTIFY_EMAIL_TO is not set" in notifier.reason
    assert "SMTP_PASSWORD" not in notifier.reason  # no username either
    assert make_unit_settings(**EMAIL).notify_email_problems() == []


async def test_log_notifier_never_sends() -> None:
    assert await LogNotifier("off").send("s", "b") is False
    assert FakeSMTP.instances == []


async def test_email_is_sent_with_starttls_and_login() -> None:
    notifier = make_notifier(make_unit_settings(**EMAIL))
    assert isinstance(notifier, EmailNotifier)
    assert await notifier.send("Problem: x", "body text") is True
    [smtp] = FakeSMTP.instances
    assert (smtp.host, smtp.port) == ("smtp.example.com", 587)
    assert smtp.calls[:2] == ["starttls", "login:sender@example.com:app-password-123"]
    [msg] = smtp.sent
    assert msg["Subject"] == "[CycleDays] Problem: x"
    assert msg["From"] == "sender@example.com"
    assert msg["To"] == "rider@example.com, coach@example.com"
    assert msg.get_content().strip() == "body text"


async def test_ssl_mode_skips_starttls() -> None:
    notifier = make_notifier(make_unit_settings(**EMAIL, smtp_security="ssl", smtp_port=465))
    assert await notifier.send("s", "b") is True
    assert "starttls" not in FakeSMTP.instances[0].calls


async def test_a_failing_server_is_logged_not_raised(caplog: pytest.LogCaptureFixture) -> None:
    FakeSMTP.fail_with = smtplib.SMTPAuthenticationError(535, b"bad credentials")
    notifier = make_notifier(make_unit_settings(**EMAIL))
    with caplog.at_level(logging.ERROR, logger="app.notifications"):
        assert await notifier.send("s", "b") is False
    assert any(r.message == "notification_failed" for r in caplog.records)
    assert "app-password-123" not in caplog.text
