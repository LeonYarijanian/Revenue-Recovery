"""The 13 tables from Section 5."""

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    TypeDecorator,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.enums import (
    AccountingMethod,
    ConnectionStatus,
    ReminderStatus,
    SendMode,
)
from app.weeks import db_now


class DecimalString(TypeDecorator):
    """Decimal stored as text, so settings never pass through floats."""

    impl = String(20)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return None if value is None else str(Decimal(value))

    def process_result_value(self, value, dialect):
        return None if value is None else Decimal(value)


class TimestampMixin:
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=db_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=db_now, onupdate=db_now, nullable=False)


def company_fk(unique: bool = False) -> Mapped[int]:
    return mapped_column(ForeignKey("companies.id"), nullable=False, index=True, unique=unique)


class Company(TimestampMixin, Base):
    __tablename__ = "companies"

    name: Mapped[str] = mapped_column(String(120), nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="America/Los_Angeles")
    industry: Mapped[str] = mapped_column(String(60), nullable=False, default="")
    source_type: Mapped[str] = mapped_column(String(8), nullable=False)
    report_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class CompanySettings(TimestampMixin, Base):
    __tablename__ = "company_settings"

    company_id: Mapped[int] = company_fk(unique=True)
    send_mode: Mapped[str] = mapped_column(String(8), nullable=False, default=SendMode.DEMO.value)
    report_send_time: Mapped[str] = mapped_column(String(5), nullable=False, default="07:00")
    report_recipients: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    accounting_method: Mapped[str] = mapped_column(String(8), nullable=False, default=AccountingMethod.ACCRUAL.value)
    multiple_low: Mapped[Decimal] = mapped_column(DecimalString, nullable=False, default=Decimal("2.5"))
    multiple_high: Mapped[Decimal] = mapped_column(DecimalString, nullable=False, default=Decimal("3.5"))
    runway_low_weeks: Mapped[Decimal] = mapped_column(DecimalString, nullable=False, default=Decimal("8.0"))
    ar_severe_min_cents: Mapped[int] = mapped_column(Integer, nullable=False, default=250000)
    ar_aging_pct: Mapped[Decimal] = mapped_column(DecimalString, nullable=False, default=Decimal("20.0"))
    revenue_drop_pct: Mapped[Decimal] = mapped_column(DecimalString, nullable=False, default=Decimal("15.0"))
    margin_target_pct: Mapped[Decimal] = mapped_column(DecimalString, nullable=False, default=Decimal("40.0"))
    dso_high_days: Mapped[Decimal] = mapped_column(DecimalString, nullable=False, default=Decimal("45.0"))
    margin_drop_points: Mapped[Decimal] = mapped_column(DecimalString, nullable=False, default=Decimal("5.0"))
    reminders_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    reminder_min_days_past_due: Mapped[int] = mapped_column(Integer, nullable=False, default=7)
    reminder_max_per_invoice: Mapped[int] = mapped_column(Integer, nullable=False, default=4)
    reminder_reply_to: Mapped[str] = mapped_column(String(254), nullable=False, default="")
    company_phone_e164: Mapped[str] = mapped_column(String(16), nullable=False, default="")
    payment_instructions: Mapped[str] = mapped_column(String(300), nullable=False, default="")


class User(TimestampMixin, Base):
    __tablename__ = "users"

    company_id: Mapped[int | None] = mapped_column(ForeignKey("companies.id"), nullable=True, index=True)
    email: Mapped[str] = mapped_column(String(254), nullable=False, unique=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    full_name: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # Approved addition: temporary passwords must be changed at first login (Section 12).
    must_change_password: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class LoginAttempt(TimestampMixin, Base):
    __tablename__ = "login_attempts"

    email: Mapped[str] = mapped_column(String(254), nullable=False, index=True)
    attempted_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    succeeded: Mapped[bool] = mapped_column(Boolean, nullable=False)


class QboConnection(TimestampMixin, Base):
    __tablename__ = "qbo_connections"

    company_id: Mapped[int] = company_fk(unique=True)
    realm_id: Mapped[str] = mapped_column(String(32), nullable=False)
    environment: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=ConnectionStatus.NOT_CONNECTED.value)
    access_token_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    refresh_token_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    access_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    connected_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_refreshed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class SyncRun(TimestampMixin, Base):
    __tablename__ = "sync_runs"

    company_id: Mapped[int] = company_fk()
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(12), nullable=False)
    error_text: Mapped[str | None] = mapped_column(String(500), nullable=True)
    weeks_synced: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    invoices_synced: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class WeeklyFinancial(TimestampMixin, Base):
    __tablename__ = "weekly_financials"
    __table_args__ = (UniqueConstraint("company_id", "week_start"),)

    company_id: Mapped[int] = company_fk()
    week_start: Mapped[date] = mapped_column(Date, nullable=False)
    revenue_cents: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cogs_cents: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    opex_cents: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    other_income_cents: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    other_expense_cents: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    net_income_cents: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    synced_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=db_now)


class Invoice(TimestampMixin, Base):
    __tablename__ = "invoices"
    __table_args__ = (UniqueConstraint("company_id", "source_id"),)

    company_id: Mapped[int] = company_fk()
    source_id: Mapped[str] = mapped_column(String(32), nullable=False)
    doc_number: Mapped[str] = mapped_column(String(40), nullable=False, default="")
    customer_name: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    customer_email: Mapped[str] = mapped_column(String(254), nullable=False, default="")
    txn_date: Mapped[date] = mapped_column(Date, nullable=False)
    due_date: Mapped[date] = mapped_column(Date, nullable=False)
    total_cents: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    balance_cents: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_open: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    is_disputed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    last_synced_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=db_now)


class BankBalance(TimestampMixin, Base):
    __tablename__ = "bank_balances"

    company_id: Mapped[int] = company_fk()
    as_of: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    total_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    account_count: Mapped[int] = mapped_column(Integer, nullable=False)


class Addback(TimestampMixin, Base):
    __tablename__ = "addbacks"

    company_id: Mapped[int] = company_fk()
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    label: Mapped[str] = mapped_column(String(80), nullable=False)
    annual_amount_cents: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    note: Mapped[str] = mapped_column(String(200), nullable=False, default="")


class Report(TimestampMixin, Base):
    __tablename__ = "reports"
    __table_args__ = (UniqueConstraint("company_id", "report_date"),)

    company_id: Mapped[int] = company_fk()
    report_date: Mapped[date] = mapped_column(Date, nullable=False)
    week_start: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String(12), nullable=False)
    html: Mapped[str] = mapped_column(Text, nullable=False)
    metrics: Mapped[dict] = mapped_column(JSON, nullable=False)
    issues: Mapped[list] = mapped_column(JSON, nullable=False)
    data_as_of: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    generated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    recipients: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    error_text: Mapped[str | None] = mapped_column(Text, nullable=True)


class Reminder(TimestampMixin, Base):
    __tablename__ = "reminders"

    company_id: Mapped[int] = company_fk()
    invoice_id: Mapped[int] = mapped_column(ForeignKey("invoices.id"), nullable=False, index=True)
    sequence_number: Mapped[int] = mapped_column(Integer, nullable=False)
    template_level: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(12), nullable=False, default=ReminderStatus.DRAFT.value)
    to_email: Mapped[str] = mapped_column(String(254), nullable=False)
    subject: Mapped[str] = mapped_column(String(200), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    token: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    drafted_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    approved_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    provider_message_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    error_text: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # Approved addition: the balance the draft quoted, for the "Balance changed" check (Section 10).
    balance_cents_at_draft: Mapped[int] = mapped_column(Integer, nullable=False)


class Suppression(TimestampMixin, Base):
    __tablename__ = "suppressions"
    __table_args__ = (UniqueConstraint("company_id", "email"),)

    company_id: Mapped[int] = company_fk()
    email: Mapped[str] = mapped_column(String(254), nullable=False)
    reason: Mapped[str] = mapped_column(String(16), nullable=False)
