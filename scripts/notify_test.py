"""Send a test email with the notification settings from .env (Phase 9).

    python -m scripts.notify_test
    docker compose exec scheduler python -m scripts.notify_test

Exit code: 0 sent, 1 sending failed, 2 email not configured.
"""

from __future__ import annotations

import asyncio
import json
import socket
import sys

from app.config import get_settings
from app.logging_config import configure_logging
from app.notifications import EmailNotifier, make_notifier


async def main() -> int:
    settings = get_settings()
    configure_logging(settings, stream=sys.stderr)
    notifier = make_notifier(settings)
    if not isinstance(notifier, EmailNotifier):
        print(json.dumps({"ok": False, "problems": settings.notify_email_problems()}))
        return 2
    ok = await notifier.send(
        "Test notification",
        "This is a test email from CycleDays.\n\n"
        f"Sent from: {socket.gethostname()}\n"
        f"SMTP: {settings.smtp_host}:{settings.smtp_port} ({settings.smtp_security})\n"
        "Problems and the daily summary will arrive the same way.",
    )
    print(json.dumps({"ok": ok, "to": notifier.recipients, "from": notifier.sender}))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
