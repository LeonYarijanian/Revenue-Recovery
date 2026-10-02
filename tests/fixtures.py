"""build_base_fixture(): the Section 15 base data, in a fresh SQLite database."""

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

from alembic import command
from alembic.config import Config

from app.auth import hash_password
from app.config import get_settings
from app.db import init_engine, session_scope
from app.enums import Role, SourceType, SyncStatus
from app.models import BankBalance, Company, CompanySettings, Invoice, SyncRun, User, WeeklyFinancial
from app.weeks import local_datetime, to_db

ROOT = Path(__file__).resolve().parent.parent
R = date(2026, 10, 12)
W0 = date(2026, 10, 5)
PASSWORD = "test-password-123"
_PASSWORD_HASH = hash_password(PASSWORD)

WEEKS = [
    # week_start, revenue, cogs, opex, other expenses, net income
    (date(2026, 9, 7), 4000000, 2200000, 1000000, 0, 800000),
    (date(2026, 9, 14), 4400000, 2400000, 1000000, 0, 1000000),
    (date(2026, 9, 21), 3600000, 2000000, 1000000, 0, 600000),
    (date(2026, 9, 28), 4000000, 2200000, 1000000, 0, 800000),
    (date(2026, 10, 5), 3200000, 2000000, 1000000, 0, 200000),
]

INVOICES = [
    # doc number, customer, email, due date, balance
    ("INV-101", "Acme Property", "ap@acme.example.com", date(2026, 10, 20), 500000),
    ("INV-102", "Baker Home", "pay@baker.example.com", date(2026, 9, 30), 300000),
    ("INV-103", "Chen Dental", "office@chen.example.com", date(2026, 8, 28), 200000),
    ("INV-104", "Delgado LLC", "ap@delgado.example.com", date(2026, 7, 31), 480000),
    ("INV-105", "Evans Family", "", date(2026, 6, 1), 120000),
]


def local(year, month, day, hour=0, minute=0, second=0) -> datetime:
    """America/Los_Angeles wall-clock time (October 2026 is PDT, UTC-7)."""
    return local_datetime(year, month, day, hour, minute, second)


@dataclass
class BaseFixture:
    url: str
    company_id: int
    other_company_id: int
    operator_id: int
    owner_id: int
    other_owner_id: int
    invoice_ids: dict[str, int] = field(default_factory=dict)


def migrate(url: str) -> None:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", url)
    config.attributes["configure_logger"] = False
    command.upgrade(config, "head")


def fresh_database() -> str:
    url = get_settings().DATABASE_URL
    migrate(url)
    init_engine(url)
    return url


def make_user(db, email: str, role: Role, company_id: int | None, name: str = "") -> User:
    user = User(
        email=email,
        password_hash=_PASSWORD_HASH,
        full_name=name or email.split("@")[0].title(),
        role=role,
        company_id=company_id,
        is_active=True,
        must_change_password=False,
    )
    db.add(user)
    db.flush()
    return user


def build_base_fixture() -> BaseFixture:
    url = fresh_database()
    with session_scope() as db:
        company = Company(name="Test HVAC", timezone="America/Los_Angeles", industry="HVAC", source_type=SourceType.DEMO)
        other = Company(name="Other Co", timezone="America/Los_Angeles", industry="Plumbing", source_type=SourceType.DEMO)
        db.add_all([company, other])
        db.flush()
        db.add(
            CompanySettings(
                company_id=company.id, company_phone_e164="+18185550100", reminder_reply_to="owner@test.local"
            )
        )
        db.add(CompanySettings(company_id=other.id))
        operator = make_user(db, "operator@test.local", Role.OPERATOR, None, "Test Operator")
        owner = make_user(db, "owner@test.local", Role.OWNER, company.id, "Test Owner")
        other_owner = make_user(db, "other@test.local", Role.OWNER, other.id, "Other Owner")

        finished = local(2026, 10, 12, 6, 0)
        db.add(
            SyncRun(
                company_id=company.id,
                started_at=to_db(finished - timedelta(minutes=1)),
                finished_at=to_db(finished),
                status=SyncStatus.SUCCEEDED,
                weeks_synced=5,
                invoices_synced=5,
            )
        )
        db.add(BankBalance(company_id=company.id, as_of=to_db(finished), total_cents=18000000, account_count=1))
        for week_start, revenue, cogs, opex, other_exp, net in WEEKS:
            db.add(
                WeeklyFinancial(
                    company_id=company.id,
                    week_start=week_start,
                    revenue_cents=revenue,
                    cogs_cents=cogs,
                    opex_cents=opex,
                    other_income_cents=0,
                    other_expense_cents=other_exp,
                    net_income_cents=net,
                    synced_at=to_db(finished),
                )
            )
        invoice_rows = {}
        for doc, customer, email, due, balance in INVOICES:
            invoice = Invoice(
                company_id=company.id,
                source_id=re.sub(r"\D", "", doc),
                doc_number=doc,
                customer_name=customer,
                customer_email=email,
                txn_date=due - timedelta(days=30),
                due_date=due,
                total_cents=balance,
                balance_cents=balance,
                is_open=True,
                is_disputed=False,
                last_synced_at=to_db(finished),
            )
            db.add(invoice)
            invoice_rows[doc] = invoice
        db.commit()
        return BaseFixture(
            url=url,
            company_id=company.id,
            other_company_id=other.id,
            operator_id=operator.id,
            owner_id=owner.id,
            other_owner_id=other_owner.id,
            invoice_ids={doc: inv.id for doc, inv in invoice_rows.items()},
        )
