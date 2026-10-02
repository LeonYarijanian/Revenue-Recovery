"""DemoMailer (./outbox) and PostmarkMailer, plus the LIVE mode preconditions (Section 9)."""

import logging
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
from pathlib import Path

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.enums import QboEnvironment, SourceType
from app.models import Company, CompanySettings, QboConnection

logger = logging.getLogger(__name__)

OUTBOX_DIR = Path("outbox")
POSTMARK_URL = "https://api.postmarkapp.com/email"
MESSAGE_STREAM = "outbound"
DEMO_FROM_EMAIL = "reports@demo.local"
DEMO_FROM_NAME = "Owner KPI Report"

# Tests replace this with httpx.MockTransport; None means the real network.
TRANSPORT: httpx.BaseTransport | None = None


@dataclass
class OutgoingEmail:
    from_addr: str
    to: list[str]
    reply_to: str
    subject: str
    text_body: str
    html_body: str | None = None


@dataclass
class SendResult:
    ok: bool
    message_ids: list[str] = field(default_factory=list)
    error: str | None = None


def mask_email(address: str) -> str:
    local, _, domain = address.partition("@")
    return f"{local[:1]}***@{domain}" if domain else "***"


def sender(name: str, email: str) -> str:
    """RFC 5322 From/Reply-To value; quotes display names such as 'Glendale Comfort Air (Demo)'."""
    return formataddr((name, email))


class DemoMailer:
    """Writes the email to ./outbox; nothing leaves the computer."""

    def send(self, email: OutgoingEmail, file_stem: str) -> SendResult:
        OUTBOX_DIR.mkdir(parents=True, exist_ok=True)
        if email.html_body is not None:
            (OUTBOX_DIR / f"{file_stem}.html").write_text(email.html_body, encoding="utf-8")
        message = EmailMessage()
        message["From"] = email.from_addr
        message["To"] = ", ".join(email.to) if email.to else "undisclosed-recipients:;"
        if email.reply_to:
            message["Reply-To"] = email.reply_to
        message["Subject"] = email.subject
        message["Date"] = formatdate(usegmt=True)
        message["Message-ID"] = make_msgid(domain="demo.local")
        message.set_content(email.text_body)
        if email.html_body is not None:
            message.add_alternative(email.html_body, subtype="html")
        (OUTBOX_DIR / f"{file_stem}.eml").write_bytes(bytes(message))
        masked = ",".join(mask_email(address) for address in email.to) or "(none)"
        print(f"[DEMO EMAIL] to={masked} subject={email.subject}")
        return SendResult(ok=True, message_ids=[f"demo-{file_stem}"])


class PostmarkMailer:
    """1 POST per recipient. Success when ErrorCode is 0."""

    def send(self, email: OutgoingEmail) -> SendResult:
        settings = get_settings()
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-Postmark-Server-Token": settings.POSTMARK_SERVER_TOKEN,
        }
        message_ids, errors = [], []
        with httpx.Client(transport=TRANSPORT, timeout=30.0) as client:
            for recipient in email.to:
                payload = {
                    "From": email.from_addr,
                    "To": recipient,
                    "ReplyTo": email.reply_to,
                    "Subject": email.subject,
                    "TextBody": email.text_body,
                    "MessageStream": MESSAGE_STREAM,
                }
                if email.html_body is not None:
                    payload["HtmlBody"] = email.html_body
                try:
                    response = client.post(POSTMARK_URL, json=payload, headers=headers)
                    data = response.json()
                except (httpx.HTTPError, ValueError) as exc:
                    errors.append(f"Postmark request failed: {type(exc).__name__}")
                    continue
                if data.get("ErrorCode") == 0:
                    message_ids.append(str(data.get("MessageID", "")))
                else:
                    errors.append(f"Postmark ErrorCode {data.get('ErrorCode')}: {data.get('Message', '')}".strip())
        if not email.to:
            errors.append("No recipients.")
        if errors:
            logger.warning("Postmark send failed for %d of %d recipients.", len(errors), len(email.to))
        return SendResult(ok=not errors, message_ids=message_ids, error="; ".join(errors) or None)


def live_precondition_failures(db: Session, company: Company, recipients: list[str] | None = None) -> list[str]:
    """Every LIVE precondition that fails, numbered as in Section 9. Empty means LIVE is allowed."""
    settings = get_settings()
    failures = []
    if not settings.ALLOW_LIVE_EMAIL:
        failures.append("1. ALLOW_LIVE_EMAIL=true is not set in .env.")
    missing = [
        name
        for name in ("POSTMARK_SERVER_TOKEN", "REPORT_FROM_EMAIL", "REPORT_FROM_NAME", "OPERATOR_EMAIL")
        if not getattr(settings, name)
    ]
    if missing:
        failures.append(f"2. Not set in .env: {', '.join(missing)}.")
    if not settings.SENDER_DOMAIN_VERIFIED:
        failures.append("3. SENDER_DOMAIN_VERIFIED=true is not set in .env.")
    if not settings.PUBLIC_BASE_URL.startswith("https://"):
        failures.append("4. PUBLIC_BASE_URL does not start with https://.")
    if recipients is None:
        company_settings = db.scalar(select(CompanySettings).where(CompanySettings.company_id == company.id))
        recipients = company_settings.report_recipients if company_settings else []
    if not recipients:
        failures.append("5. The company has no report recipient.")
    connection = db.scalar(select(QboConnection).where(QboConnection.company_id == company.id))
    if (
        company.source_type != SourceType.QBO
        or connection is None
        or connection.environment != QboEnvironment.PRODUCTION
    ):
        failures.append("6. The company's source must be QBO with a PRODUCTION QuickBooks connection.")
    return failures
