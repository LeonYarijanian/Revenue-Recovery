"""python -m app.seed --demo: the offline pitch company (Section 14)."""

import argparse
import sys
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import hash_password
from app.config import get_settings
from app.db import init_engine, session_scope
from app.enums import AddbackCategory, Role, SendMode, SourceType, SyncStatus
from app.models import Addback, Company, CompanySettings, Invoice, User
from app.reminders import draft_reminders
from app.report import generate_report
from app.sources.demo import DEMO_COMPANY_NAME, DEMO_DISPUTED_DOC
from app.sync import run_sync
from app.weeks import utcnow

DEMO_PASSWORD = "demo-password-123"
DEMO_OPERATOR = ("operator@demo.local", "Demo Operator")
DEMO_OWNER = ("owner@demo.local", "Demo Owner")
DEMO_ADDBACKS = [
    (AddbackCategory.OWNER_PAY, "Owner pay", 12000000),
    (AddbackCategory.INTEREST, "Interest", 800000),
    (AddbackCategory.DEPRECIATION_AMORTIZATION, "Depreciation and amortization", 1500000),
    (AddbackCategory.ONE_TIME, "One-time", 700000),
]


def seed_demo(db: Session, now: datetime) -> int:
    if db.scalar(select(Company.id).where(Company.name == DEMO_COMPANY_NAME)) is not None:
        print("Demo company already exists.")
        return 1
    taken = db.scalars(select(User.email).where(User.email.in_([DEMO_OPERATOR[0], DEMO_OWNER[0]]))).all()
    if taken:
        print(f"Demo users already exist: {', '.join(taken)}.")
        return 1

    company = Company(
        name=DEMO_COMPANY_NAME,
        timezone="America/Los_Angeles",
        industry="HVAC",
        source_type=SourceType.DEMO,
        report_enabled=True,
    )
    db.add(company)
    db.flush()
    db.add(
        CompanySettings(
            company_id=company.id,
            send_mode=SendMode.DEMO,
            report_recipients=[DEMO_OWNER[0]],
            company_phone_e164="+18185550100",
            reminder_reply_to=DEMO_OWNER[0],
            payment_instructions="Pay online at example.com/pay or call us.",
        )
    )
    password_hash = hash_password(DEMO_PASSWORD)
    db.add(
        User(
            company_id=None,
            email=DEMO_OPERATOR[0],
            full_name=DEMO_OPERATOR[1],
            role=Role.OPERATOR,
            password_hash=password_hash,
            is_active=True,
            must_change_password=False,
        )
    )
    db.add(
        User(
            company_id=company.id,
            email=DEMO_OWNER[0],
            full_name=DEMO_OWNER[1],
            role=Role.OWNER,
            password_hash=password_hash,
            is_active=True,
            must_change_password=False,
        )
    )
    for category, label, cents in DEMO_ADDBACKS:
        db.add(Addback(company_id=company.id, category=category, label=label, annual_amount_cents=cents, note=""))
    db.commit()

    run = run_sync(db, company.id, now)
    if run is None or run.status != SyncStatus.SUCCEEDED:
        print(f"Demo sync failed: {run.error_text if run else 'already running'}")
        return 1
    disputed = db.scalar(
        select(Invoice).where(Invoice.company_id == company.id, Invoice.doc_number == DEMO_DISPUTED_DOC)
    )
    disputed.is_disputed = True
    db.commit()

    drafts = draft_reminders(db, company.id, now)
    report = generate_report(db, company.id, now)
    print(
        f"Demo company created: {DEMO_COMPANY_NAME} (id {company.id}). "
        f"Report for {report.report_date.isoformat()}, {len(drafts)} reminder drafts. "
        f"Log in as {DEMO_OWNER[0]} or {DEMO_OPERATOR[0]} with password {DEMO_PASSWORD}."
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.seed", description="Seed the demo company.")
    parser.add_argument("--demo", action="store_true", help="build the Glendale Comfort Air demo company")
    args = parser.parse_args(argv)
    if not args.demo:
        parser.print_help()
        return 2
    init_engine(get_settings().DATABASE_URL)
    with session_scope() as db:
        return seed_demo(db, utcnow())


if __name__ == "__main__":
    sys.exit(main())
