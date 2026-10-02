"""Overdue-invoice reminders (Section 10): draft, edit, approve, send, unsubscribe."""

import logging
import re
import secrets
from datetime import datetime, time, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.enums import ReminderStatus, SendMode, SuppressionReason
from app.mailer import DEMO_FROM_EMAIL, DemoMailer, OutgoingEmail, PostmarkMailer, live_precondition_failures, sender
from app.models import Company, CompanySettings, Invoice, Reminder, Suppression, User
from app.money import fmt_dollars_cents
from app.sync import last_successful_sync, run_sync
from app.validation import is_valid_email
from app.weeks import as_utc, fmt_date, local_date, to_db, to_local

logger = logging.getLogger(__name__)

SEND_WINDOW_START = time(9, 0)
SEND_WINDOW_END = time(17, 0)
MAX_SENT_PER_DAY = 50
MIN_DAYS_BETWEEN_SENDS = 7
SYNC_MAX_AGE = timedelta(hours=24)
SYNC_NEEDED = "Sync needed before sending"
BALANCE_CHANGED = "Balance changed"
INVOICE_CLOSED = "Invoice no longer open"
DISPUTED_NOTE = "Invoice marked disputed"
UNSUBSCRIBED_NOTE = "Customer unsubscribed"
OPEN_STATUSES = (ReminderStatus.DRAFT, ReminderStatus.APPROVED)

BANNED_PHRASES = [
    "collection agency",
    "collections",
    "lawsuit",
    "sue",
    "legal action",
    "attorney",
    "credit report",
    "credit bureau",
    "late fee",
    "interest charge",
    "final notice",
    "garnish",
]
_BANNED_PATTERNS = [
    (phrase, re.compile(r"(?<!\w)" + r"\s+".join(map(re.escape, phrase.split())) + r"(?!\w)", re.IGNORECASE))
    for phrase in BANNED_PHRASES
]

TEMPLATES = {
    1: (
        "Invoice {doc_number} from {company_name}",
        "Hi {customer_name}, a quick reminder that invoice {doc_number} for {balance} was due on {due_date}. "
        "If you've already paid, thank you, and please ignore this note.",
    ),
    2: (
        "Invoice {doc_number} is {days} days past due",
        "Hi {customer_name}, invoice {doc_number} for {balance} was due on {due_date} and is now {days} days "
        "past due. Could you let us know when we can expect payment? If something on the invoice looks wrong, "
        "just reply and we'll sort it out.",
    ),
    3: (
        "Please call us about invoice {doc_number}",
        "Hi {customer_name}, invoice {doc_number} for {balance} is {days} days past due. "
        "Please reply or call us at {company_phone} so we can settle it together.",
    ),
}


class ReminderError(ValueError):
    pass


def banned_word_error(text: str) -> str | None:
    """Whole words or phrases, ignoring case: "sue" must not match "issue"."""
    for phrase, pattern in _BANNED_PATTERNS:
        if pattern.search(text or ""):
            return f'Remove "{phrase}". Reminders must stay polite and must not threaten.'
    return None


def template_level(days_past_due: int) -> int:
    if days_past_due >= 60:
        return 3
    if days_past_due >= 30:
        return 2
    return 1


def unsubscribe_url(token: str) -> str:
    return f"{get_settings().PUBLIC_BASE_URL.rstrip('/')}/r/{token}"


def render_reminder(
    level: int, invoice: Invoice, company: Company, settings: CompanySettings, days: int, token: str
) -> tuple[str, str]:
    values = {
        "customer_name": invoice.customer_name,
        "doc_number": invoice.doc_number,
        "balance": fmt_dollars_cents(invoice.balance_cents),
        "due_date": fmt_date(invoice.due_date),
        "days": days,
        "company_name": company.name,
        "company_phone": settings.company_phone_e164,
    }
    subject_template, body_template = TEMPLATES[level]
    parts = [body_template.format(**values)]
    if settings.payment_instructions:
        parts.append(settings.payment_instructions)
    parts.append(f"Thank you,\n{company.name}")
    parts.append(f"--\nTo stop payment reminders by email: {unsubscribe_url(token)}")
    return subject_template.format(**values), "\n\n".join(parts)


def _settings(db: Session, company_id: int) -> CompanySettings:
    return db.scalar(select(CompanySettings).where(CompanySettings.company_id == company_id))


def _suppressed(db: Session, company_id: int) -> set[str]:
    return {
        email.lower() for email in db.scalars(select(Suppression.email).where(Suppression.company_id == company_id))
    }


def draft_reminders(db: Session, company_id: int, now: datetime) -> list[Reminder]:
    """Draft a reminder for every open invoice that meets all 7 conditions."""
    company = db.get(Company, company_id)
    settings = _settings(db, company_id)
    if company is None or settings is None or not settings.reminders_enabled:
        return []
    today = local_date(now, company.timezone)
    suppressed = _suppressed(db, company_id)
    invoices = db.scalars(
        select(Invoice)
        .where(Invoice.company_id == company_id, Invoice.is_open.is_(True))
        .order_by(Invoice.due_date, Invoice.id)
    ).all()
    drafted = []
    for invoice in invoices:
        days = (today - invoice.due_date).days
        email = (invoice.customer_email or "").strip().lower()
        if invoice.balance_cents <= 0:  # 1
            continue
        if days < settings.reminder_min_days_past_due:  # 2
            continue
        if not is_valid_email(email) or email in suppressed:  # 3
            continue
        if invoice.is_disputed:  # 4
            continue
        history = db.scalars(select(Reminder).where(Reminder.invoice_id == invoice.id)).all()
        if any(r.status in OPEN_STATUSES for r in history):  # 5
            continue
        sent = [r for r in history if r.status == ReminderStatus.SENT and r.sent_at is not None]
        if sent:
            last_sent = local_date(max(r.sent_at for r in sent), company.timezone)
            if (today - last_sent).days < MIN_DAYS_BETWEEN_SENDS:  # 6
                continue
        if len(sent) >= settings.reminder_max_per_invoice:  # 7
            continue

        level = template_level(days)
        token = secrets.token_hex(16)
        subject, body = render_reminder(level, invoice, company, settings, days, token)
        reminder = Reminder(
            company_id=company_id,
            invoice_id=invoice.id,
            sequence_number=len(sent) + 1,
            template_level=level,
            status=ReminderStatus.DRAFT,
            to_email=email,
            subject=subject,
            body=body,
            token=token,
            drafted_at=to_db(now),
            balance_cents_at_draft=invoice.balance_cents,
        )
        db.add(reminder)
        drafted.append(reminder)
    db.commit()
    return drafted


def update_draft(db: Session, reminder: Reminder, subject: str, body: str, to_email: str | None = None) -> Reminder:
    """Save edits to a DRAFT. Raises ReminderError with the message to show."""
    if reminder.status != ReminderStatus.DRAFT:
        raise ReminderError("Only drafts can be edited.")
    subject = (subject or "").strip()
    body = (body or "").replace("\r\n", "\n").strip()
    if not subject or not body:
        raise ReminderError("Subject and body cannot be empty.")
    if len(subject) > 200:
        raise ReminderError("Subject must be 200 characters or fewer.")
    for text in (subject, body):
        error = banned_word_error(text)
        if error:
            raise ReminderError(error)
    if unsubscribe_url(reminder.token) not in body:
        raise ReminderError("Keep the unsubscribe line at the end of the reminder.")
    if to_email is not None:
        to_email = to_email.strip().lower()
        if not is_valid_email(to_email):
            raise ReminderError("Enter a valid email address.")
        if to_email in _suppressed(db, reminder.company_id):
            raise ReminderError("That email address unsubscribed from payment reminders.")
        reminder.to_email = to_email
    reminder.subject = subject
    reminder.body = body
    db.commit()
    return reminder


def approve(db: Session, reminder: Reminder, user: User, now: datetime) -> Reminder:
    if reminder.status != ReminderStatus.DRAFT:
        raise ReminderError("Only drafts can be approved.")
    reminder.status = ReminderStatus.APPROVED
    reminder.approved_by_user_id = user.id
    reminder.approved_at = to_db(now)
    db.commit()
    return reminder


def approve_all(db: Session, company_id: int, user: User, now: datetime) -> int:
    drafts = db.scalars(
        select(Reminder).where(Reminder.company_id == company_id, Reminder.status == ReminderStatus.DRAFT)
    ).all()
    for reminder in drafts:
        reminder.status = ReminderStatus.APPROVED
        reminder.approved_by_user_id = user.id
        reminder.approved_at = to_db(now)
    db.commit()
    return len(drafts)


def skip(db: Session, reminder: Reminder) -> Reminder:
    if reminder.status != ReminderStatus.DRAFT:
        raise ReminderError("Only drafts can be skipped.")
    reminder.status = ReminderStatus.CANCELLED
    db.commit()
    return reminder


def set_disputed(db: Session, invoice: Invoice, disputed: bool) -> None:
    """Disputed invoices never get reminders, so marking one cancels its open reminders."""
    invoice.is_disputed = disputed
    if disputed:
        for reminder in db.scalars(
            select(Reminder).where(Reminder.invoice_id == invoice.id, Reminder.status.in_(OPEN_STATUSES))
        ):
            reminder.status = ReminderStatus.CANCELLED
            reminder.error_text = DISPUTED_NOTE
    db.commit()


def start_sync(db: Session, company_id: int, now: datetime) -> None:
    run_sync(db, company_id, now)


def in_send_window(local: datetime) -> bool:
    return local.weekday() < 5 and SEND_WINDOW_START <= local.time() < SEND_WINDOW_END


def send_reminders(db: Session, now: datetime, company_id: int | None = None) -> list[Reminder]:
    """Send APPROVED reminders inside Monday-Friday 09:00-17:00 local, at most 50 per company per day."""
    query = select(Company)
    if company_id is not None:
        query = query.where(Company.id == company_id)
    processed = []
    for company in db.scalars(query).all():
        processed.extend(_send_for_company(db, company, now))
    return processed


def _send_for_company(db: Session, company: Company, now: datetime) -> list[Reminder]:
    settings = _settings(db, company.id)
    if settings is None or not settings.reminders_enabled:
        return []
    local = to_local(now, company.timezone)
    if not in_send_window(local):
        return []
    approved = db.scalars(
        select(Reminder)
        .where(Reminder.company_id == company.id, Reminder.status == ReminderStatus.APPROVED)
        .order_by(Reminder.approved_at, Reminder.id)
    ).all()
    if not approved:
        return []

    last_ok = last_successful_sync(db, company.id)
    if last_ok is None or as_utc(now) - as_utc(last_ok.finished_at) > SYNC_MAX_AGE:
        for reminder in approved:
            reminder.error_text = SYNC_NEEDED
        db.commit()
        start_sync(db, company.id, now)
        return approved

    capacity = MAX_SENT_PER_DAY - _count_sent_today(db, company, now)
    processed = []
    for reminder in approved:
        invoice = db.get(Invoice, reminder.invoice_id)
        if not invoice.is_open or invoice.balance_cents <= 0:
            reminder.status = ReminderStatus.CANCELLED
            reminder.error_text = INVOICE_CLOSED
        elif invoice.balance_cents != reminder.balance_cents_at_draft:
            reminder.status = ReminderStatus.CANCELLED
            reminder.error_text = BALANCE_CHANGED
        elif capacity <= 0:
            break
        else:
            _deliver(db, company, settings, reminder, now)
            if reminder.status == ReminderStatus.SENT:
                capacity -= 1
        db.commit()
        processed.append(reminder)
    return processed


def _count_sent_today(db: Session, company: Company, now: datetime) -> int:
    today = local_date(now, company.timezone)
    sent_times = db.scalars(
        select(Reminder.sent_at).where(
            Reminder.company_id == company.id,
            Reminder.status == ReminderStatus.SENT,
            Reminder.sent_at >= to_db(now) - timedelta(days=2),
        )
    ).all()
    return sum(1 for sent_at in sent_times if local_date(sent_at, company.timezone) == today)


def _deliver(db: Session, company: Company, settings: CompanySettings, reminder: Reminder, now: datetime) -> None:
    app_settings = get_settings()
    if settings.send_mode == SendMode.LIVE:
        failures = live_precondition_failures(db, company)
        if failures:
            reminder.status = ReminderStatus.FAILED
            reminder.error_text = ("LIVE preconditions failed: " + " ".join(failures))[:500]
            return
        email = OutgoingEmail(
            from_addr=sender(company.name, app_settings.REPORT_FROM_EMAIL),
            to=[reminder.to_email],
            reply_to=settings.reminder_reply_to,
            subject=reminder.subject,
            text_body=reminder.body,
        )
        result = PostmarkMailer().send(email)
    else:
        email = OutgoingEmail(
            from_addr=sender(company.name, app_settings.REPORT_FROM_EMAIL or DEMO_FROM_EMAIL),
            to=[reminder.to_email],
            reply_to=settings.reminder_reply_to,
            subject=reminder.subject,
            text_body=reminder.body,
        )
        result = DemoMailer().send(email, f"reminder-{company.id}-{reminder.id}")
    if result.ok:
        reminder.status = ReminderStatus.SENT
        reminder.sent_at = to_db(now)
        reminder.provider_message_id = (result.message_ids or [None])[0]
        reminder.error_text = None
    else:
        reminder.status = ReminderStatus.FAILED
        reminder.error_text = (result.error or "Send failed")[:500]


def unsubscribe(db: Session, token: str) -> Company | None:
    """Suppress the reminder's email for its company and cancel that email's open reminders."""
    reminder = db.scalar(select(Reminder).where(Reminder.token == token))
    if reminder is None:
        return None
    email = reminder.to_email.lower()
    exists = db.scalar(
        select(func.count(Suppression.id)).where(
            Suppression.company_id == reminder.company_id, func.lower(Suppression.email) == email
        )
    )
    if not exists:
        db.add(Suppression(company_id=reminder.company_id, email=email, reason=SuppressionReason.UNSUBSCRIBED))
    for open_reminder in db.scalars(
        select(Reminder).where(
            Reminder.company_id == reminder.company_id,
            func.lower(Reminder.to_email) == email,
            Reminder.status.in_(OPEN_STATUSES),
        )
    ):
        open_reminder.status = ReminderStatus.CANCELLED
        open_reminder.error_text = UNSUBSCRIBED_NOTE
    db.commit()
    return db.get(Company, reminder.company_id)
