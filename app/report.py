"""Builds, stores and sends the weekly report (Section 9).

Every number comes from kpis.py (Section 7) or issues.py (Section 8). The same
Jinja2 template renders the email and the in-app view.
"""

import logging
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.enums import ReminderStatus, ReportStatus, SendMode, SyncStatus
from app.issues import NO_RED_FLAGS, IssueRules, evaluate, shown
from app.kpis import NOT_MEANINGFUL, compute_kpis, to_metrics
from app.mailer import (
    DEMO_FROM_EMAIL,
    DEMO_FROM_NAME,
    DemoMailer,
    OutgoingEmail,
    PostmarkMailer,
    live_precondition_failures,
    sender,
)
from app.models import Company, CompanySettings, Reminder, Report, SyncRun
from app.money import NO_DATA, fmt_dollars, fmt_signed_1dp, q1
from app.sync import last_successful_sync
from app.weeks import (
    as_utc,
    fmt_date,
    fmt_local_datetime,
    fmt_month_day,
    local_date,
    report_date_for,
    report_week_start,
    to_db,
    to_local,
    week_label,
)

logger = logging.getLogger(__name__)

HOLD_AFTER = timedelta(days=8)
TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
_env = Environment(loader=FileSystemLoader(str(TEMPLATES_DIR)), autoescape=select_autoescape(["html"]))


# ---------------------------------------------------------------------------
# Metrics: every computed number, stored in reports.metrics
# ---------------------------------------------------------------------------


def build_metrics(
    db: Session,
    company: Company,
    report_date: date,
    data_as_of: datetime | None,
    sync_failed: bool,
) -> tuple[dict, list[str]]:
    settings = _settings(db, company.id)
    kpis = compute_kpis(db, company.id, report_date)
    triggered = evaluate(kpis, IssueRules.from_settings(settings))
    drafts = db.scalar(
        select(func.count(Reminder.id)).where(
            Reminder.company_id == company.id, Reminder.status == ReminderStatus.DRAFT
        )
    )
    metrics = to_metrics(kpis)
    metrics.update(
        {
            "issues_shown": [{"code": i.code.value, "line": i.line, "action": i.action} for i in shown(triggered)],
            "drafts_count": int(drafts),
            "data_as_of": as_utc(data_as_of).isoformat() if data_as_of else None,
            "sync_failed": sync_failed,
            "timezone": company.timezone,
        }
    )
    return metrics, [issue.code.value for issue in triggered]


# ---------------------------------------------------------------------------
# View: display strings for the template, built only from stored metrics
# ---------------------------------------------------------------------------


def _pct(value: str | None) -> str:
    return NO_DATA if value is None else f"{value}%"


def _signed_pct(value: str | None) -> str:
    return NO_DATA if value is None else f"{fmt_signed_1dp(Decimal(value))}%"


def _bar_width(revenue: int, maximum: int) -> str:
    width = max(q1(Decimal(revenue) / maximum * 100), Decimal("1"))
    return str(int(width)) if width == width.to_integral_value() else str(width)


def build_view(company_name: str, metrics: dict) -> dict:
    tz = metrics.get("timezone") or "America/Los_Angeles"
    week_start = date.fromisoformat(metrics["week_start"])
    data_as_of = datetime.fromisoformat(metrics["data_as_of"]) if metrics.get("data_as_of") else None
    data_as_of_text = fmt_local_datetime(data_as_of, tz) if data_as_of else NO_DATA

    chart_values = [row["revenue"] for row in metrics["chart"] if row["revenue"] is not None]
    maximum = max([value for value in chart_values if value > 0], default=0)
    chart = []
    for row in metrics["chart"]:
        revenue = row["revenue"]
        if revenue is None:
            amount, width = "n/a", None
        else:
            amount = fmt_dollars(revenue)
            width = _bar_width(revenue, maximum) if revenue > 0 and maximum > 0 else None
        chart.append({"label": fmt_month_day(date.fromisoformat(row["week_start"])), "amount": amount, "width": width})

    if metrics["sde"] is None:
        sde_line, range_line = f"Earnings (SDE): {NO_DATA}", f"Rough range: {NO_DATA}"
    else:
        sde_line = f"Earnings (SDE): {fmt_dollars(metrics['sde'])}"
        if metrics["ttm_annualized"]:
            sde_line += f" (annualized from {metrics['ttm_weeks']} weeks)"
        if metrics["sde"] <= 0:
            range_line = NOT_MEANINGFUL
        else:
            range_line = (
                f"Rough range: {fmt_dollars(metrics['value_low'])} to {fmt_dollars(metrics['value_high'])} "
                f"({metrics['multiple_low']}x to {metrics['multiple_high']}x earnings)"
            )

    aging = [
        {"label": row["label"], "count": row["count"], "amount": fmt_dollars(row["amount"])} for row in metrics["aging"]
    ]
    drafts = metrics.get("drafts_count", 0)
    base_url = get_settings().PUBLIC_BASE_URL.rstrip("/")
    return {
        "title": f"{company_name}: week of {week_label(week_start)}",
        "banner": (
            f"Data as of {data_as_of_text}. This week's sync failed, so numbers may be out of date."
            if metrics.get("sync_failed")
            else None
        ),
        "tiles": [
            {
                "name": "Revenue",
                "big": fmt_dollars(metrics["revenue_week"]),
                "small": f"{_signed_pct(metrics['revenue_change_pct'])} vs 4-week average of "
                f"{fmt_dollars(metrics['revenue_avg_4'])}",
            },
            {
                "name": "Gross margin",
                "big": _pct(metrics["margin_week_pct"]),
                "small": f"4-week: {_pct(metrics['margin_4wk_pct'])}",
            },
            {
                "name": "Cash",
                "big": fmt_dollars(metrics["cash_on_hand"]),
                "small": NO_DATA if metrics["runway_weeks"] is None else f"{metrics['runway_weeks']} weeks of costs",
            },
            {
                "name": "Owed to you",
                "big": fmt_dollars(metrics["ar_total"]),
                "small": NO_DATA
                if metrics["ar_61_plus_pct"] is None
                else f"{metrics['ar_61_plus_pct']}% over 60 days late",
            },
        ],
        "issues": metrics["issues_shown"],
        "no_red_flags": NO_RED_FLAGS,
        "aging": aging,
        "aging_total": {
            "count": sum(row["count"] for row in metrics["aging"]),
            "amount": fmt_dollars(sum(row["amount"] for row in metrics["aging"])),
        },
        "top_overdue": [
            {
                "customer": row["customer"],
                "doc_number": row["doc_number"],
                "balance": fmt_dollars(row["balance"]),
                "days": row["days"],
            }
            for row in metrics["top_overdue"]
        ],
        "chart": chart,
        "sde_line": sde_line,
        "range_line": range_line,
        "reminders_line": f"{drafts} payment reminders are ready for you to review." if drafts else None,
        "reminders_url": f"{base_url}/reminders",
        "footer": f"Data from QuickBooks as of {data_as_of_text}. Questions? Reply to this email.",
    }


def render_html(view: dict) -> str:
    html = _env.get_template("report_email.html").render(**view)
    # The template uses double-quoted attributes only, so apostrophes need no escaping.
    return html.replace("&#39;", "'")


def render_text(view: dict) -> str:
    return _env.get_template("report_email.txt").render(**view)


def report_subject(company_name: str, metrics: dict) -> str:
    if metrics["revenue_week"] is None:
        return f"{company_name} weekly report: week of {fmt_date(date.fromisoformat(metrics['week_start']))}"
    change = metrics["revenue_change_pct"]
    change_text = NO_DATA if change is None else f"{fmt_signed_1dp(Decimal(change))}%"
    return f"{company_name} weekly report: revenue {fmt_dollars(metrics['revenue_week'])} ({change_text} vs avg)"


# ---------------------------------------------------------------------------
# Generate, preview and send
# ---------------------------------------------------------------------------


def _settings(db: Session, company_id: int) -> CompanySettings:
    return db.scalar(select(CompanySettings).where(CompanySettings.company_id == company_id))


def _sync_failed_today(db: Session, company: Company, now: datetime) -> bool:
    newest = db.scalar(
        select(SyncRun).where(SyncRun.company_id == company.id).order_by(SyncRun.started_at.desc(), SyncRun.id.desc())
    )
    return (
        newest is not None
        and newest.status == SyncStatus.FAILED
        and local_date(newest.started_at, company.timezone) == local_date(now, company.timezone)
    )


def build_report(db: Session, company: Company, now: datetime, report_date: date | None = None) -> dict | None:
    """Everything a report needs, without saving. None when the company has never synced."""
    report_date = report_date or report_date_for(local_date(now, company.timezone))
    last_ok = last_successful_sync(db, company.id)
    if last_ok is None:
        return None
    data_as_of = as_utc(last_ok.finished_at)
    age = as_utc(now) - data_as_of
    held = age > HOLD_AFTER
    sync_failed = not held and _sync_failed_today(db, company, now)
    metrics, issue_codes = build_metrics(db, company, report_date, data_as_of, sync_failed)
    return {
        "report_date": report_date,
        "metrics": metrics,
        "issues": issue_codes,
        "html": render_html(build_view(company.name, metrics)),
        "data_as_of": data_as_of,
        "held": held,
        "age_days": age.days,
    }


def generate_report(db: Session, company_id: int, now: datetime, report_date: date | None = None) -> Report | None:
    """Create 1 report for the company's report date. Does nothing if one already exists."""
    company = db.get(Company, company_id)
    if company is None or not company.report_enabled:
        return None
    report_date = report_date or report_date_for(local_date(now, company.timezone))
    existing = db.scalar(select(Report).where(Report.company_id == company_id, Report.report_date == report_date))
    if existing is not None:
        return existing
    built = build_report(db, company, now, report_date)
    if built is None:
        return None
    report = Report(
        company_id=company_id,
        report_date=report_date,
        week_start=report_week_start(report_date),
        status=ReportStatus.HELD if built["held"] else ReportStatus.GENERATED,
        html=built["html"],
        metrics=built["metrics"],
        issues=built["issues"],
        data_as_of=to_db(built["data_as_of"]),
        generated_at=to_db(now),
        recipients=[],
        error_text=f"Held: data is {built['age_days']} days old" if built["held"] else None,
    )
    db.add(report)
    db.commit()
    return report


def preview_report(db: Session, company_id: int, now: datetime) -> str | None:
    company = db.get(Company, company_id)
    built = build_report(db, company, now) if company else None
    return built["html"] if built else None


def send_report(db: Session, report: Report, now: datetime) -> Report:
    """Send 1 report through the company's mailer (Send now, Resend, or the 07:00 step)."""
    company = db.get(Company, report.company_id)
    settings = _settings(db, company.id)
    app_settings = get_settings()
    recipients = list(settings.report_recipients or [])
    view = build_view(company.name, report.metrics)
    subject = report_subject(company.name, report.metrics)
    text = render_text(view)

    if settings.send_mode == SendMode.LIVE:
        failures = live_precondition_failures(db, company, recipients)
        if failures:
            report.status = ReportStatus.FAILED
            report.error_text = "LIVE preconditions failed: " + " ".join(failures)
            db.commit()
            return report
        email = OutgoingEmail(
            from_addr=sender(app_settings.REPORT_FROM_NAME, app_settings.REPORT_FROM_EMAIL),
            to=recipients,
            reply_to=app_settings.OPERATOR_EMAIL,
            subject=subject,
            text_body=text,
            html_body=report.html,
        )
        result = PostmarkMailer().send(email)
    else:
        email = OutgoingEmail(
            from_addr=sender(
                app_settings.REPORT_FROM_NAME or DEMO_FROM_NAME, app_settings.REPORT_FROM_EMAIL or DEMO_FROM_EMAIL
            ),
            to=recipients,
            reply_to=app_settings.OPERATOR_EMAIL,
            subject=subject,
            text_body=text,
            html_body=report.html,
        )
        result = DemoMailer().send(email, f"report-{company.id}-{report.report_date.isoformat()}")

    report.recipients = recipients
    if result.ok:
        report.status = ReportStatus.SENT
        report.sent_at = to_db(now)
        report.error_text = None
    else:
        report.status = ReportStatus.FAILED
        report.error_text = result.error
    db.commit()
    return report


def send_reports(db: Session, now: datetime, company_id: int | None = None) -> list[Report]:
    """The send step: every GENERATED report for today, once local time reaches report_send_time."""
    query = select(Company).where(Company.report_enabled.is_(True))
    if company_id is not None:
        query = query.where(Company.id == company_id)
    sent = []
    for company in db.scalars(query).all():
        local = to_local(now, company.timezone)
        settings = _settings(db, company.id)
        if local.strftime("%H:%M") < settings.report_send_time:
            continue
        reports = db.scalars(
            select(Report).where(
                Report.company_id == company.id,
                Report.report_date == local.date(),
                Report.status == ReportStatus.GENERATED,
            )
        ).all()
        for report in reports:
            sent.append(send_report(db, report, now))
    return sent
