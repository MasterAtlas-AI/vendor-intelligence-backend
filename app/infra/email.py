import smtplib
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Protocol

import anyio

from app.core.config import Settings
from app.core.logging import get_logger

log = get_logger(__name__)


@dataclass(frozen=True)
class OutgoingEmail:
    to: str
    subject: str
    body: str


class EmailSender(Protocol):
    async def send(self, message: OutgoingEmail) -> None: ...


class ConsoleEmailSender:
    """Development sender: logs the message (including links) instead of delivering it."""

    async def send(self, message: OutgoingEmail) -> None:
        log.info("email.console", to=message.to, subject=message.subject, body=message.body)


class SmtpEmailSender:
    def __init__(self, settings: Settings) -> None:
        self._s = settings

    def _send_sync(self, message: OutgoingEmail) -> None:
        msg = EmailMessage()
        msg["From"] = self._s.smtp_from
        msg["To"] = message.to
        msg["Subject"] = message.subject
        msg.set_content(message.body)
        with smtplib.SMTP(self._s.smtp_host, self._s.smtp_port, timeout=10) as smtp:
            if self._s.smtp_starttls:
                smtp.starttls()
            if self._s.smtp_username and self._s.smtp_password:
                smtp.login(self._s.smtp_username, self._s.smtp_password.get_secret_value())
            smtp.send_message(msg)

    async def send(self, message: OutgoingEmail) -> None:
        try:
            await anyio.to_thread.run_sync(self._send_sync, message)
        except (OSError, smtplib.SMTPException):
            # Delivery failures must not fail the request (e.g. an invite is still created and can be resent).
            log.exception("email.send_failed", to=message.to, subject=message.subject)


def build_email_sender(settings: Settings) -> EmailSender:
    return SmtpEmailSender(settings) if settings.email_backend == "smtp" else ConsoleEmailSender()


def invite_email(to: str, tenant_name: str, link: str) -> OutgoingEmail:
    return OutgoingEmail(
        to=to,
        subject=f"You've been invited to {tenant_name} on MasterAtlas AI",
        body=f"You have been invited to join {tenant_name} on MasterAtlas AI.\n\n"
        f"Accept the invitation and set your password:\n{link}\n\nThis link expires soon and can be used once.",
    )


def password_reset_email(to: str, link: str) -> OutgoingEmail:
    return OutgoingEmail(
        to=to,
        subject="Reset your MasterAtlas AI password",
        body=f"Use this link to reset your password:\n{link}\n\n"
        "If you did not request a reset, you can ignore this email.",
    )
