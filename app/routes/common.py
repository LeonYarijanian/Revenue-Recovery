"""Template rendering and the shared page context (top bar)."""

from decimal import Decimal
from pathlib import Path

from fastapi import Request
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth import active_company, csrf_token
from app.enums import ReminderStatus, Role, SendMode
from app.models import Company, CompanySettings, Reminder, User
from app.money import fmt_dollars, fmt_dollars_cents
from app.weeks import fmt_date, fmt_local_datetime

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.filters["dollars"] = fmt_dollars
templates.env.filters["dollars_cents"] = fmt_dollars_cents
templates.env.filters["plain_dollars"] = lambda cents: f"{Decimal(cents) / 100:.2f}"
templates.env.filters["date"] = lambda d: fmt_date(d) if d else ""
templates.env.globals["fmt_local"] = lambda dt, tz: fmt_local_datetime(dt, tz) if dt else ""


def render(request: Request, name: str, context: dict | None = None, status_code: int = 200):
    ctx = {"csrf_token": csrf_token(request)}
    ctx.update(context or {})
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)


def set_flash(request: Request, message: str) -> None:
    request.session["flash"] = message


def pop_flash(request: Request) -> str | None:
    return request.session.pop("flash", None)


def page_context(request: Request, db: Session, user: User, company: Company | None = None) -> dict:
    """Top bar: company name or switcher, links, reminder counter and mode badge."""
    company = company or active_company(request, user, db)
    is_operator = user.role == Role.OPERATOR
    companies = db.scalars(select(Company).order_by(Company.name)).all() if is_operator else []
    drafts = 0
    send_mode = SendMode.DEMO.value
    if company is not None:
        drafts = db.scalar(
            select(func.count(Reminder.id)).where(
                Reminder.company_id == company.id, Reminder.status == ReminderStatus.DRAFT
            )
        )
        settings = db.scalar(select(CompanySettings).where(CompanySettings.company_id == company.id))
        send_mode = settings.send_mode if settings else SendMode.DEMO.value
    return {
        "user": user,
        "company": company,
        "companies": companies,
        "is_operator": is_operator,
        "drafts_count": drafts,
        "send_mode": send_mode,
    }
