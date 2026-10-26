"""Email delivery with dry-run, SMTP and Resend backends.

Every attempt is recorded in ``email_messages``. Stored bodies have the interview link
redacted so tokens never sit in the database or logs in plaintext. In dry-run mode nothing is
sent and the status says so ("logged_not_sent").
"""

from __future__ import annotations

import logging
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage as MimeMessage
from email.utils import make_msgid

import httpx

from app.config import settings

log = logging.getLogger(__name__)

REDACTED_LINK = "[personal interview link — not stored]"


@dataclass
class OutgoingEmail:
    to: str
    subject: str
    text: str
    html: str


@dataclass
class DeliveryResult:
    status: str  # logged_not_sent | sent | failed
    backend: str
    error: str = ""
    provider_id: str = ""


class EmailBackend:
    name = "base"

    def send(self, msg: OutgoingEmail) -> DeliveryResult:  # pragma: no cover - interface
        raise NotImplementedError


class DryRunBackend(EmailBackend):
    name = "dryrun"

    def send(self, msg: OutgoingEmail) -> DeliveryResult:
        log.info("email dry-run: not sent", extra={"safe": {"subject": msg.subject}})
        return DeliveryResult(status="logged_not_sent", backend=self.name)


class SmtpBackend(EmailBackend):
    name = "smtp"

    def send(self, msg: OutgoingEmail) -> DeliveryResult:
        s = settings()
        if not s.smtp_host:
            return DeliveryResult(status="failed", backend=self.name, error="SMTP_HOST is not configured.")
        mime = MimeMessage()
        mime["From"] = s.email_from
        mime["To"] = msg.to
        mime["Subject"] = msg.subject
        mime["Message-ID"] = make_msgid()
        mime.set_content(msg.text)
        mime.add_alternative(msg.html, subtype="html")
        try:
            ctx = ssl.create_default_context()
            if s.smtp_ssl:
                server: smtplib.SMTP = smtplib.SMTP_SSL(s.smtp_host, s.smtp_port, timeout=20, context=ctx)
            else:
                server = smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=20)
            with server:
                if s.smtp_starttls and not s.smtp_ssl:
                    server.starttls(context=ctx)
                if s.smtp_username:
                    server.login(s.smtp_username, s.smtp_password)
                refused = server.send_message(mime)
            if refused:
                return DeliveryResult(status="failed", backend=self.name, error="Recipient refused by the mail server.")
            return DeliveryResult(status="sent", backend=self.name, provider_id=mime["Message-ID"])
        except smtplib.SMTPAuthenticationError:
            return DeliveryResult(status="failed", backend=self.name, error="SMTP authentication failed. Check SMTP_USERNAME/SMTP_PASSWORD.")
        except (smtplib.SMTPException, OSError) as exc:
            return DeliveryResult(status="failed", backend=self.name, error=f"SMTP error: {type(exc).__name__}")


class ResendBackend(EmailBackend):
    name = "resend"

    def send(self, msg: OutgoingEmail) -> DeliveryResult:
        s = settings()
        if not s.resend_api_key:
            return DeliveryResult(status="failed", backend=self.name, error="RESEND_API_KEY is not configured.")
        try:
            resp = httpx.post(
                "https://api.resend.com/emails",
                headers={"Authorization": f"Bearer {s.resend_api_key}"},
                json={"from": s.email_from, "to": [msg.to], "subject": msg.subject, "html": msg.html, "text": msg.text},
                timeout=20,
            )
        except httpx.HTTPError as exc:
            return DeliveryResult(status="failed", backend=self.name, error=f"Could not reach Resend ({type(exc).__name__}).")
        if resp.status_code >= 400:
            detail = ""
            try:
                detail = str(resp.json().get("message", ""))[:200]
            except ValueError:
                pass
            return DeliveryResult(status="failed", backend=self.name, error=f"Resend rejected the email ({resp.status_code}). {detail}".strip())
        try:
            pid = str(resp.json().get("id", ""))
        except ValueError:
            pid = ""
        return DeliveryResult(status="sent", backend=self.name, provider_id=pid)


def get_backend() -> EmailBackend:
    return {"smtp": SmtpBackend, "resend": ResendBackend}.get(settings().email_backend, DryRunBackend)()
