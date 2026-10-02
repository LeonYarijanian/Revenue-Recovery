"""Operator console, company page (tabs) and New company (Section 11)."""

from zoneinfo import available_timezones

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth import hash_password, require_operator, temporary_password
from app.config import validate_company_settings, validate_recipients
from app.db import get_db
from app.enums import ConnectionStatus, ReminderStatus, ReportStatus, Role, SendMode, SourceType, SyncStatus
from app.issues import IssueRules, evaluate
from app.kpis import compute_kpis, display
from app.mailer import live_precondition_failures
from app.models import Company, CompanySettings, Reminder, Report, SyncRun, User
from app.report import preview_report, send_report
from app.routes.common import page_context, pop_flash, render, set_flash
from app.sources.qbo_oauth import connection_for
from app.sync import run_sync
from app.validation import is_valid_email
from app.weeks import local_date, report_date_for, utcnow

router = APIRouter()

TABS = ["overview", "connection", "report", "settings", "recipients", "users"]
SETTING_FIELDS = [
    "send_mode",
    "report_send_time",
    "accounting_method",
    "multiple_low",
    "multiple_high",
    "runway_low_weeks",
    "ar_severe_min_cents",
    "ar_aging_pct",
    "revenue_drop_pct",
    "margin_target_pct",
    "dso_high_days",
    "margin_drop_points",
    "reminders_enabled",
    "reminder_min_days_past_due",
    "reminder_max_per_invoice",
    "reminder_reply_to",
    "company_phone_e164",
    "payment_instructions",
]


def _settings(db: Session, company_id: int) -> CompanySettings:
    return db.scalar(select(CompanySettings).where(CompanySettings.company_id == company_id))


@router.get("/operator")
def console(request: Request, user: User = Depends(require_operator), db: Session = Depends(get_db)):
    rows = []
    for company in db.scalars(select(Company).order_by(Company.name)).all():
        connection = connection_for(db, company.id)
        last_sync = db.scalar(
            select(SyncRun).where(SyncRun.company_id == company.id).order_by(SyncRun.started_at.desc(), SyncRun.id.desc())
        )
        last_report = db.scalar(
            select(Report).where(Report.company_id == company.id).order_by(Report.report_date.desc())
        )
        drafts = db.scalar(
            select(func.count(Reminder.id)).where(
                Reminder.company_id == company.id, Reminder.status == ReminderStatus.DRAFT
            )
        )
        status = connection.status if connection else ConnectionStatus.NOT_CONNECTED.value
        red = (
            status == ConnectionStatus.NEEDS_RECONNECT
            or (last_report is not None and last_report.status in (ReportStatus.HELD, ReportStatus.FAILED))
            or (last_sync is not None and last_sync.status == SyncStatus.FAILED)
        )
        rows.append(
            {
                "company": company,
                "environment": connection.environment if connection else "",
                "connection_status": status,
                "last_sync": last_sync,
                "last_report": last_report,
                "issue_count": len(last_report.issues) if last_report else None,
                "drafts": drafts,
                "red": red,
            }
        )
    ctx = page_context(request, db, user)
    ctx.update(rows=rows, flash=pop_flash(request))
    return render(request, "operator.html", ctx)


# ---------------------------------------------------------------------------
# New company
# ---------------------------------------------------------------------------


def _new_company_form(request: Request, db: Session, user: User, values: dict, error: str | None, status: int = 200):
    ctx = page_context(request, db, user)
    ctx.update(values=values, error=error, timezones=sorted(available_timezones()), source_types=list(SourceType))
    return render(request, "company_new.html", ctx, status_code=status)


@router.get("/companies/new")
def new_company_page(request: Request, user: User = Depends(require_operator), db: Session = Depends(get_db)):
    values = {"timezone": "America/Los_Angeles", "source_type": SourceType.QBO.value}
    return _new_company_form(request, db, user, values, None)


@router.post("/companies")
def create_company(
    request: Request,
    name: str = Form(""),
    timezone: str = Form("America/Los_Angeles"),
    industry: str = Form(""),
    source_type: str = Form(SourceType.QBO.value),
    owner_name: str = Form(""),
    owner_email: str = Form(""),
    user: User = Depends(require_operator),
    db: Session = Depends(get_db),
):
    values = {
        "name": name.strip(),
        "timezone": timezone.strip(),
        "industry": industry.strip(),
        "source_type": source_type.strip().upper(),
        "owner_name": owner_name.strip(),
        "owner_email": owner_email.strip().lower(),
    }
    error = None
    if not values["name"] or len(values["name"]) > 120:
        error = "Name is required and must be 120 characters or fewer."
    elif values["timezone"] not in available_timezones():
        error = "Choose a valid time zone."
    elif len(values["industry"]) > 60:
        error = "Industry must be 60 characters or fewer."
    elif values["source_type"] not in {s.value for s in SourceType}:
        error = "Source type must be DEMO or QBO."
    elif not values["owner_name"] or len(values["owner_name"]) > 120:
        error = "Owner name is required."
    elif not is_valid_email(values["owner_email"]):
        error = "Enter a valid owner email."
    elif db.scalar(select(User.id).where(User.email == values["owner_email"])):
        error = "A user with that email already exists."
    if error:
        return _new_company_form(request, db, user, values, error, status=400)

    company = Company(
        name=values["name"],
        timezone=values["timezone"],
        industry=values["industry"],
        source_type=values["source_type"],
        report_enabled=True,
    )
    db.add(company)
    db.flush()
    db.add(CompanySettings(company_id=company.id))
    password = temporary_password()
    db.add(
        User(
            company_id=company.id,
            email=values["owner_email"],
            password_hash=hash_password(password),
            full_name=values["owner_name"],
            role=Role.OWNER,
            is_active=True,
            must_change_password=True,
        )
    )
    db.commit()
    request.session["company_id"] = company.id
    set_flash(
        request,
        f"Company created. Temporary password for {values['owner_email']}: {password} "
        "(shown once; the owner must change it at first login).",
    )
    return RedirectResponse(f"/companies/{company.id}?tab=users", status_code=303)


# ---------------------------------------------------------------------------
# Company page and its tabs
# ---------------------------------------------------------------------------


def _company_or_404(db: Session, company_id: int) -> Company:
    company = db.get(Company, company_id)
    if company is None:
        raise HTTPException(status_code=404)
    return company


def _company_context(
    request: Request, db: Session, user: User, company: Company, tab: str, **extra
) -> dict:
    request.session["company_id"] = company.id
    ctx = page_context(request, db, user, company)
    settings = _settings(db, company.id)
    ctx.update(
        tab=tab if tab in TABS else "overview",
        tabs=TABS,
        settings=settings,
        connection=connection_for(db, company.id),
        flash=pop_flash(request),
        errors=[],
        form=None,
    )
    if ctx["tab"] == "overview":
        report_date = report_date_for(local_date(utcnow(), company.timezone))
        kpis = compute_kpis(db, company.id, report_date)
        ctx.update(
            report_date=report_date,
            figures=display(kpis),
            triggered=evaluate(kpis, IssueRules.from_settings(settings)),
        )
    elif ctx["tab"] == "connection":
        ctx["sync_runs"] = db.scalars(
            select(SyncRun).where(SyncRun.company_id == company.id).order_by(SyncRun.started_at.desc()).limit(20)
        ).all()
    elif ctx["tab"] == "report":
        ctx["reports"] = db.scalars(
            select(Report).where(Report.company_id == company.id).order_by(Report.report_date.desc())
        ).all()
    elif ctx["tab"] == "users":
        ctx["users"] = db.scalars(select(User).where(User.company_id == company.id).order_by(User.id)).all()
    ctx.update(extra)
    return ctx


@router.get("/companies/{company_id}")
def company_page(
    company_id: int,
    request: Request,
    tab: str = "overview",
    user: User = Depends(require_operator),
    db: Session = Depends(get_db),
):
    company = _company_or_404(db, company_id)
    return render(request, "company.html", _company_context(request, db, user, company, tab))


@router.post("/companies/{company_id}/sync")
def sync_now(company_id: int, request: Request, user: User = Depends(require_operator), db: Session = Depends(get_db)):
    company = _company_or_404(db, company_id)
    run = run_sync(db, company.id, utcnow())
    if run is None:
        set_flash(request, "A sync is already running.")
    else:
        set_flash(request, f"Sync {run.status.lower()}." + (f" {run.error_text}" if run.error_text else ""))
    return RedirectResponse(f"/companies/{company.id}?tab=connection", status_code=303)


@router.get("/companies/{company_id}/preview")
def preview(company_id: int, request: Request, user: User = Depends(require_operator), db: Session = Depends(get_db)):
    company = _company_or_404(db, company_id)
    html = preview_report(db, company.id, utcnow())
    ctx = _company_context(request, db, user, company, "report", preview_html=html, previewing=True)
    return render(request, "company.html", ctx)


def _report_action(db: Session, company: Company, report_id: int) -> Report:
    report = db.get(Report, report_id)
    if report is None or report.company_id != company.id:
        raise HTTPException(status_code=404)
    return report


@router.post("/companies/{company_id}/reports/{report_id}/send")
def send_now(
    company_id: int, report_id: int, request: Request, user: User = Depends(require_operator), db: Session = Depends(get_db)
):
    company = _company_or_404(db, company_id)
    report = _report_action(db, company, report_id)
    if report.status not in (ReportStatus.GENERATED, ReportStatus.HELD):
        set_flash(request, "Send now is only for GENERATED or HELD reports.")
    else:
        send_report(db, report, utcnow())
        set_flash(request, f"Report {report.status.lower()}." + (f" {report.error_text}" if report.error_text else ""))
    return RedirectResponse(f"/companies/{company.id}?tab=report", status_code=303)


@router.post("/companies/{company_id}/reports/{report_id}/resend")
def resend(
    company_id: int, report_id: int, request: Request, user: User = Depends(require_operator), db: Session = Depends(get_db)
):
    company = _company_or_404(db, company_id)
    report = _report_action(db, company, report_id)
    if report.status != ReportStatus.SENT:
        set_flash(request, "Resend is only for SENT reports.")
    else:
        send_report(db, report, utcnow())
        set_flash(request, f"Report {report.status.lower()}." + (f" {report.error_text}" if report.error_text else ""))
    return RedirectResponse(f"/companies/{company.id}?tab=report", status_code=303)


def save_settings(db: Session, company: Company, form: dict) -> list[str]:
    """Validate and save the Settings tab. Nothing is saved when anything fails."""
    clean, errors = validate_company_settings(form)
    settings = _settings(db, company.id)
    if clean.get("send_mode") == SendMode.LIVE and not errors:
        failures = live_precondition_failures(db, company, settings.report_recipients)
        if failures:
            errors.append("LIVE refused; staying in DEMO. " + " ".join(failures))
    if errors:
        return errors
    for name, value in clean.items():
        setattr(settings, name, value)
    company.report_enabled = str(form.get("report_enabled", "")).lower() in {"on", "true", "1", "yes"}
    db.commit()
    return []


@router.post("/companies/{company_id}/settings")
async def update_settings(
    company_id: int, request: Request, user: User = Depends(require_operator), db: Session = Depends(get_db)
):
    company = _company_or_404(db, company_id)
    form = dict(await request.form())
    errors = save_settings(db, company, form)
    if errors:
        db.rollback()
        ctx = _company_context(request, db, user, company, "settings")
        ctx.update(errors=errors, form=form)
        return render(request, "company.html", ctx, status_code=400)
    set_flash(request, "Settings saved.")
    return RedirectResponse(f"/companies/{company.id}?tab=settings", status_code=303)


@router.post("/companies/{company_id}/recipients")
def update_recipients(
    company_id: int,
    request: Request,
    recipients: str = Form(""),
    user: User = Depends(require_operator),
    db: Session = Depends(get_db),
):
    company = _company_or_404(db, company_id)
    emails, errors = validate_recipients(recipients)
    settings = _settings(db, company.id)
    if not errors and not emails and settings.send_mode == SendMode.LIVE:
        errors.append("report_recipients: LIVE needs at least 1 recipient.")
    if errors:
        ctx = _company_context(request, db, user, company, "recipients")
        ctx.update(errors=errors, form={"recipients": recipients})
        return render(request, "company.html", ctx, status_code=400)
    settings.report_recipients = emails
    db.commit()
    set_flash(request, "Recipients saved.")
    return RedirectResponse(f"/companies/{company.id}?tab=recipients", status_code=303)


@router.post("/companies/{company_id}/users")
def add_user(
    company_id: int,
    request: Request,
    full_name: str = Form(""),
    email: str = Form(""),
    user: User = Depends(require_operator),
    db: Session = Depends(get_db),
):
    company = _company_or_404(db, company_id)
    email = email.strip().lower()
    error = None
    if not full_name.strip():
        error = "Name is required."
    elif not is_valid_email(email):
        error = "Enter a valid email."
    elif db.scalar(select(User.id).where(User.email == email)):
        error = "A user with that email already exists."
    if error:
        ctx = _company_context(request, db, user, company, "users")
        ctx.update(errors=[error], form={"full_name": full_name, "email": email})
        return render(request, "company.html", ctx, status_code=400)
    password = temporary_password()
    db.add(
        User(
            company_id=company.id,
            email=email,
            password_hash=hash_password(password),
            full_name=full_name.strip()[:120],
            role=Role.OWNER,
            is_active=True,
            must_change_password=True,
        )
    )
    db.commit()
    set_flash(request, f"User added. Temporary password for {email}: {password} (shown once).")
    return RedirectResponse(f"/companies/{company.id}?tab=users", status_code=303)
