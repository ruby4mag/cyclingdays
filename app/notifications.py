"""Email notifications (Phase 9).

``make_notifier(settings)`` returns an :class:`EmailNotifier` when the SMTP
settings are complete, otherwise a :class:`LogNotifier` that only logs what
it would have sent. Sending never raises: a failed email is logged as
``notification_failed`` and the caller carries on. A broken mail server
must never stop the coach.

Standard library only (``smtplib`` + ``email``), run in a worker thread so
the scheduler's event loop isn't blocked.
"""

from __future__ import annotations

import asyncio
import logging
import smtplib
import ssl
from collections.abc import Sequence
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from typing import Protocol

from app.config import Settings

logger = logging.getLogger(__name__)

SUBJECT_PREFIX = "[CycleDays]"


class Notifier(Protocol):
    async def send(self, subject: str, body: str, to: Sequence[str] | None = None) -> bool:
        """Send one message; True if it was delivered to the mail server.

        ``to``: recipients (Phase 10, an athlete's own address). None: the
        operator, ``NOTIFY_EMAIL_TO``.
        """
        ...


class LogNotifier:
    """Used when email isn't configured: the message only goes to the log."""

    def __init__(self, reason: str) -> None:
        self.reason = reason

    async def send(self, subject: str, body: str, to: Sequence[str] | None = None) -> bool:
        logger.warning(
            "notification_not_sent",
            extra={"subject": subject, "reason": self.reason, "to": list(to or []), "body": body},
        )
        return False


class EmailNotifier:
    def __init__(self, settings: Settings) -> None:
        self._s = settings
        self.recipients = settings.notify_recipients
        self.sender = settings.notify_email_from or settings.smtp_username or ""

    def build(self, subject: str, body: str, to: Sequence[str] | None = None) -> EmailMessage:
        msg = EmailMessage()
        msg["Subject"] = f"{SUBJECT_PREFIX} {subject}"
        msg["From"] = self.sender
        msg["To"] = ", ".join(self.recipients if to is None else to)
        msg["Date"] = formatdate(localtime=True)
        msg["Message-ID"] = make_msgid(domain="cycledays.local")
        msg.set_content(body)
        return msg

    def _send_sync(self, msg: EmailMessage) -> None:
        s = self._s
        host, port, timeout = s.smtp_host or "", s.smtp_port, s.smtp_timeout_seconds
        context = ssl.create_default_context()
        smtp: smtplib.SMTP
        if s.smtp_security == "ssl":
            smtp = smtplib.SMTP_SSL(host, port, timeout=timeout, context=context)
        else:
            smtp = smtplib.SMTP(host, port, timeout=timeout)
        with smtp:
            if s.smtp_security == "starttls":
                smtp.starttls(context=context)
            if s.smtp_username and s.smtp_password is not None:
                smtp.login(s.smtp_username, s.smtp_password.get_secret_value())
            smtp.send_message(msg)

    async def send(self, subject: str, body: str, to: Sequence[str] | None = None) -> bool:
        recipients = list(self.recipients if to is None else to)
        if not recipients:
            return False
        msg = self.build(subject, body, recipients)
        try:
            await asyncio.to_thread(self._send_sync, msg)
        except (OSError, smtplib.SMTPException) as exc:
            logger.error(
                "notification_failed",
                extra={"subject": subject, "error": f"{type(exc).__name__}: {exc}"},
            )
            return False
        logger.info("notification_sent", extra={"subject": subject, "to": recipients})
        return True


def make_notifier(settings: Settings) -> EmailNotifier | LogNotifier:
    problems = settings.notify_email_problems()
    if problems:
        return LogNotifier("; ".join(problems))
    return EmailNotifier(settings)
