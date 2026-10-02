"""The scheduler checks every 60 seconds and runs each company's due steps (Sections 6, 9, 10)."""

import logging
from datetime import UTC, datetime

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import session_scope
from app.enums import ConnectionStatus, SourceType
from app.models import Company, Report
from app.reminders import draft_reminders, send_reminders
from app.report import generate_report, send_reports
from app.sources.qbo_oauth import connection_for
from app.sync import run_sync
from app.weeks import to_local, utcnow

logger = logging.getLogger(__name__)

NIGHTLY_SYNC = "02:00"
MONDAY_SYNC = "06:00"
MONDAY_GENERATE = "06:30"

_scheduler: BackgroundScheduler | None = None


def run_company_steps(db: Session, company: Company, now: datetime) -> None:
    local = to_local(now, company.timezone)
    hhmm = local.strftime("%H:%M")
    monday = local.weekday() == 0

    if company.source_type == SourceType.QBO and (hhmm == NIGHTLY_SYNC or (monday and hhmm == MONDAY_SYNC)):
        connection = connection_for(db, company.id)
        if connection is not None and connection.status == ConnectionStatus.CONNECTED:
            run_sync(db, company.id, now)

    if monday and hhmm >= MONDAY_GENERATE:
        has_report = db.scalar(
            select(Report.id).where(Report.company_id == company.id, Report.report_date == local.date())
        )
        if company.report_enabled and has_report is None:
            # Draft first so the report can say how many reminders are waiting.
            draft_reminders(db, company.id, now)
            generate_report(db, company.id, now)
        elif not company.report_enabled and hhmm == MONDAY_GENERATE:
            draft_reminders(db, company.id, now)

    if monday:
        send_reports(db, now, company.id)
    send_reminders(db, now, company.id)


def tick(now: datetime | None = None) -> None:
    now = now or utcnow()
    with session_scope() as db:
        for company in db.scalars(select(Company).order_by(Company.id)).all():
            try:
                run_company_steps(db, company, now)
            except Exception:
                db.rollback()
                logger.exception("Scheduled steps failed for company %s.", company.id)


def start() -> None:
    global _scheduler
    if _scheduler is not None:
        return
    _scheduler = BackgroundScheduler(timezone=UTC)
    _scheduler.add_job(tick, CronTrigger(second=0, timezone=UTC), max_instances=1, coalesce=True, misfire_grace_time=30)
    _scheduler.start()
    logger.info("Scheduler started.")


def stop() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
