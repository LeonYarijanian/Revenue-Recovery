"""Sync: pull source data, then write it in 1 all-or-nothing transaction (Section 6)."""

import logging
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.enums import ConnectionStatus, ReminderStatus, SourceType, SyncStatus
from app.models import (
    BankBalance,
    Company,
    CompanySettings,
    Invoice,
    Reminder,
    SyncRun,
    WeeklyFinancial,
)
from app.sources.base import DataSource
from app.sources.demo import DemoDataSource
from app.sources.qbo import QboDataSource
from app.sources.qbo_oauth import connection_for
from app.weeks import last_complete_week_starts, local_date, to_db

logger = logging.getLogger(__name__)

FIRST_SYNC_WEEKS = 60
LATER_SYNC_WEEKS = 6
INVOICE_CLOSED_NOTE = "Invoice no longer open"


class SyncError(Exception):
    pass


def source_for(db: Session, company: Company, now: datetime) -> DataSource:
    if company.source_type == SourceType.DEMO:
        return DemoDataSource(local_date(now, company.timezone))
    connection = connection_for(db, company.id)
    if connection is None or connection.status != ConnectionStatus.CONNECTED or not connection.refresh_token_enc:
        raise SyncError("QuickBooks is not connected.")
    settings = db.scalar(select(CompanySettings).where(CompanySettings.company_id == company.id))
    return QboDataSource(db, connection, now, settings.accounting_method)


def is_running(db: Session, company_id: int) -> bool:
    return (
        db.scalar(
            select(SyncRun.id).where(SyncRun.company_id == company_id, SyncRun.status == SyncStatus.RUNNING).limit(1)
        )
        is not None
    )


def last_successful_sync(db: Session, company_id: int) -> SyncRun | None:
    return db.scalar(
        select(SyncRun)
        .where(SyncRun.company_id == company_id, SyncRun.status == SyncStatus.SUCCEEDED)
        .order_by(SyncRun.finished_at.desc(), SyncRun.id.desc())
    )


def run_sync(db: Session, company_id: int, now: datetime, source: DataSource | None = None) -> SyncRun | None:
    """Run 1 sync. Returns the sync run, or None when one is already RUNNING."""
    company = db.get(Company, company_id)
    if company is None:
        raise SyncError(f"Company {company_id} does not exist.")
    if is_running(db, company_id):
        return None
    first_sync = last_successful_sync(db, company_id) is None
    run = SyncRun(company_id=company_id, started_at=to_db(now), status=SyncStatus.RUNNING)
    db.add(run)
    db.commit()
    run_id = run.id

    try:
        source = source or source_for(db, company, now)
        week_starts = last_complete_week_starts(
            local_date(now, company.timezone), FIRST_SYNC_WEEKS if first_sync else LATER_SYNC_WEEKS
        )
        weeks = [(week_start, source.fetch_week(week_start)) for week_start in week_starts]
        invoices = source.fetch_open_invoices()
        bank = source.fetch_bank_balance()

        _write_weeks(db, company_id, now, weeks)
        invoice_count = _write_invoices(db, company_id, now, invoices)
        db.add(
            BankBalance(company_id=company_id, as_of=to_db(now), total_cents=bank.total_cents, account_count=bank.account_count)
        )
        run.status = SyncStatus.SUCCEEDED
        run.finished_at = to_db(now)
        run.weeks_synced = len(weeks)
        run.invoices_synced = invoice_count
        db.commit()
    except Exception as exc:  # all or nothing: any error rolls everything back
        db.rollback()
        run = db.get(SyncRun, run_id)
        run.status = SyncStatus.FAILED
        run.finished_at = to_db(now)
        run.error_text = f"{type(exc).__name__}: {exc}"[:500]
        db.commit()
        logger.warning("Sync %s failed for company %s (%s).", run_id, company_id, type(exc).__name__)
    return run


def _write_weeks(db: Session, company_id: int, now: datetime, weeks) -> None:
    existing = {
        row.week_start: row
        for row in db.scalars(
            select(WeeklyFinancial).where(
                WeeklyFinancial.company_id == company_id,
                WeeklyFinancial.week_start.in_([week_start for week_start, _ in weeks]),
            )
        )
    }
    for week_start, data in weeks:
        row = existing.get(week_start)
        if row is None:
            row = WeeklyFinancial(company_id=company_id, week_start=week_start)
            db.add(row)
        row.revenue_cents = data.revenue_cents
        row.cogs_cents = data.cogs_cents
        row.opex_cents = data.opex_cents
        row.other_income_cents = data.other_income_cents
        row.other_expense_cents = data.other_expense_cents
        row.net_income_cents = data.net_income_cents
        row.synced_at = to_db(now)


def _write_invoices(db: Session, company_id: int, now: datetime, invoices) -> int:
    existing = {inv.source_id: inv for inv in db.scalars(select(Invoice).where(Invoice.company_id == company_id))}
    fetched = {data.source_id: data for data in invoices}
    for source_id, data in fetched.items():
        invoice = existing.get(source_id)
        if invoice is None:
            invoice = Invoice(company_id=company_id, source_id=source_id, is_disputed=False)
            db.add(invoice)
        invoice.doc_number = data.doc_number
        invoice.customer_name = data.customer_name
        invoice.customer_email = data.customer_email
        invoice.txn_date = data.txn_date
        invoice.due_date = data.due_date
        invoice.total_cents = data.total_cents
        invoice.balance_cents = data.balance_cents
        invoice.is_open = data.balance_cents > 0
        invoice.last_synced_at = to_db(now)

    # A previously open invoice that is not returned was paid or voided.
    closed_ids = []
    for source_id, invoice in existing.items():
        if invoice.is_open and source_id not in fetched:
            invoice.is_open = False
            invoice.balance_cents = 0
            invoice.last_synced_at = to_db(now)
            closed_ids.append(invoice.id)
    if closed_ids:
        db.execute(
            update(Reminder)
            .where(
                Reminder.invoice_id.in_(closed_ids),
                Reminder.status.in_([ReminderStatus.DRAFT, ReminderStatus.APPROVED]),
            )
            .values(status=ReminderStatus.CANCELLED, error_text=INVOICE_CLOSED_NOTE, updated_at=to_db(now))
        )
    return len(fetched)
