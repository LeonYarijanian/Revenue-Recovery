"""Reminders page and the public unsubscribe link (Section 10)."""

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import current_user, scoped_or_404
from app.db import get_db
from app.enums import ReminderStatus
from app.models import Company, Invoice, Reminder, User
from app.reminders import (
    ReminderError,
    approve,
    approve_all,
    draft_reminders,
    send_reminders,
    set_disputed,
    skip,
    unsubscribe,
    update_draft,
)
from app.routes.common import page_context, pop_flash, render, set_flash
from app.weeks import local_date, utcnow

router = APIRouter()


def _reminders_context(request: Request, db: Session, user: User, errors=None, edits=None) -> dict | None:
    ctx = page_context(request, db, user)
    company = ctx["company"]
    if company is None:
        return None
    today = local_date(utcnow(), company.timezone)

    def rows(statuses, limit=None):
        query = (
            select(Reminder, Invoice)
            .join(Invoice, Invoice.id == Reminder.invoice_id)
            .where(Reminder.company_id == company.id, Reminder.status.in_(statuses))
            .order_by(Reminder.updated_at.desc(), Reminder.id.desc())
        )
        if limit:
            query = query.limit(limit)
        return [
            {"reminder": reminder, "invoice": invoice, "days": (today - invoice.due_date).days}
            for reminder, invoice in db.execute(query).all()
        ]

    ctx.update(
        drafts=rows([ReminderStatus.DRAFT]),
        waiting=rows([ReminderStatus.APPROVED]),
        recent=rows([ReminderStatus.SENT, ReminderStatus.CANCELLED, ReminderStatus.FAILED], limit=20),
        errors=errors or {},
        edits=edits or {},
        flash=pop_flash(request),
    )
    return ctx


@router.get("/reminders")
def reminders_page(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    ctx = _reminders_context(request, db, user)
    if ctx is None:
        return RedirectResponse("/operator", status_code=302)
    return render(request, "reminders.html", ctx)


def _company_id(request: Request, db: Session, user: User) -> int:
    company = page_context(request, db, user)["company"]
    if company is None:
        raise HTTPException(status_code=404)
    return company.id


def _reminder(request: Request, db: Session, user: User, reminder_id: int) -> Reminder:
    return scoped_or_404(db.get(Reminder, reminder_id), page_context(request, db, user)["company"])


@router.post("/reminders/draft")
def draft_now(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    drafted = draft_reminders(db, _company_id(request, db, user), utcnow())
    set_flash(request, f"{len(drafted)} new reminder drafts.")
    return RedirectResponse("/reminders", status_code=303)


@router.post("/reminders/approve-all")
def approve_all_drafts(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    company_id = _company_id(request, db, user)
    now = utcnow()
    count = approve_all(db, company_id, user, now)
    send_reminders(db, now, company_id)
    set_flash(request, f"{count} reminders approved.")
    return RedirectResponse("/reminders", status_code=303)


@router.post("/reminders/{reminder_id}/approve")
def approve_and_send(
    reminder_id: int,
    request: Request,
    to_email: str = Form(""),
    subject: str = Form(""),
    body: str = Form(""),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    reminder = _reminder(request, db, user, reminder_id)
    now = utcnow()
    try:
        update_draft(db, reminder, subject, body, to_email)
        approve(db, reminder, user, now)
    except ReminderError as exc:
        db.rollback()
        edits = {reminder_id: {"to_email": to_email, "subject": subject, "body": body}}
        ctx = _reminders_context(request, db, user, errors={reminder_id: str(exc)}, edits=edits)
        return render(request, "reminders.html", ctx, status_code=400)
    send_reminders(db, now, reminder.company_id)
    db.refresh(reminder)
    if reminder.status == ReminderStatus.SENT:
        set_flash(request, f"Reminder for invoice sent to {reminder.to_email}.")
    elif reminder.status == ReminderStatus.APPROVED:
        note = reminder.error_text or "It goes out Monday to Friday, 09:00 to 17:00."
        set_flash(request, f"Reminder approved. {note}")
    else:
        set_flash(request, f"Reminder {reminder.status.lower()}: {reminder.error_text or ''}".strip())
    return RedirectResponse("/reminders", status_code=303)


@router.post("/reminders/{reminder_id}/skip")
def skip_draft(reminder_id: int, request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    reminder = _reminder(request, db, user, reminder_id)
    try:
        skip(db, reminder)
    except ReminderError as exc:
        set_flash(request, str(exc))
    return RedirectResponse("/reminders", status_code=303)


@router.post("/reminders/{reminder_id}/dispute")
def dispute_invoice(
    reminder_id: int, request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)
):
    reminder = _reminder(request, db, user, reminder_id)
    set_disputed(db, db.get(Invoice, reminder.invoice_id), True)
    set_flash(request, "Invoice marked disputed; its reminder was cancelled.")
    return RedirectResponse("/reminders", status_code=303)


# ---------------------------------------------------------------------------
# Public unsubscribe link: GET shows Confirm, POST suppresses (no CSRF; the token is the secret)
# ---------------------------------------------------------------------------


@router.get("/r/{token}")
def unsubscribe_page(token: str, request: Request, db: Session = Depends(get_db)):
    reminder = db.scalar(select(Reminder).where(Reminder.token == token))
    if reminder is None:
        raise HTTPException(status_code=404)
    company = db.get(Company, reminder.company_id)
    return render(request, "unsubscribe.html", {"token": token, "company_name": company.name, "done": False})


@router.post("/r/{token}")
def unsubscribe_confirm(token: str, request: Request, db: Session = Depends(get_db)):
    company = unsubscribe(db, token)
    if company is None:
        raise HTTPException(status_code=404)
    return render(request, "unsubscribe.html", {"token": token, "company_name": company.name, "done": True})
