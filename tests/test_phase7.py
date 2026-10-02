"""Phase 7: demo seed and the pitch walkthrough."""

import re
from datetime import date, timedelta
from pathlib import Path

import pytest
from freezegun import freeze_time
from sqlalchemy import func, select

from app.db import session_scope
from app.enums import AddbackCategory, ReminderStatus
from app.kpis import compute_kpis
from app.models import Addback, Company, Invoice, Reminder, Report, User, WeeklyFinancial
from app.seed import main as seed_main
from app.sources.demo import DEMO_COMPANY_NAME
from app.weeks import local_date, report_date_for
from tests.conftest import csrf_from, login
from tests.fixtures import fresh_database, local

SEED_AT = local(2026, 10, 12, 10, 0)  # a Monday, inside the reminder send window
DELGADO_LINE = "Delgado Property Mgmt owes $4,800, 73 days past due."


def counts() -> dict:
    with session_scope() as db:
        return {
            "companies": db.scalar(select(func.count(Company.id))),
            "users": db.scalar(select(func.count(User.id))),
            "weeks": db.scalar(select(func.count(WeeklyFinancial.id))),
            "open_invoices": db.scalar(select(func.count(Invoice.id)).where(Invoice.is_open.is_(True))),
            "reports": db.scalar(select(func.count(Report.id))),
            "drafts": db.scalar(select(func.count(Reminder.id)).where(Reminder.status == ReminderStatus.DRAFT)),
        }


def seed(when=SEED_AT) -> int:
    with freeze_time(when):
        return seed_main(["--demo"])


@pytest.fixture
def seeded():
    fresh_database()
    assert seed() == 0
    with session_scope() as db:
        return db.scalar(select(Company).where(Company.name == DEMO_COMPANY_NAME)).id


def check_demo_report(expected_report_date: date) -> None:
    with session_scope() as db:
        report = db.scalar(select(Report))
    assert report.report_date == expected_report_date
    assert [issue["code"] for issue in report.metrics["issues_shown"]] == ["RUNWAY_LOW", "AR_SEVERE", "AR_AGING"]
    assert report.metrics["issues_shown"][1]["line"] == DELGADO_LINE
    assert "REVENUE_DROP" in report.issues
    assert 5.5 <= float(report.metrics["runway_weeks"]) <= 6.0
    assert 25 <= float(report.metrics["ar_61_plus_pct"]) <= 35
    assert report.metrics["revenue_change_pct"] == "-18.0"
    assert DELGADO_LINE in report.html
    assert "6 payment reminders are ready for you to review." in report.html


def test_T7_1_demo_seed_on_a_fresh_database(seeded, capsys):
    assert counts() == {"companies": 1, "users": 2, "weeks": 60, "open_invoices": 14, "reports": 1, "drafts": 6}
    check_demo_report(date(2026, 10, 12))
    with session_scope() as db:
        no_email = db.scalar(select(Invoice).where(Invoice.customer_email == ""))
        disputed = db.scalar(select(Invoice).where(Invoice.is_disputed.is_(True)))
        drafted = set(db.scalars(select(Reminder.invoice_id)).all())
        users = {u.email: (u.role, u.full_name, u.must_change_password) for u in db.scalars(select(User))}
    assert no_email.id not in drafted and disputed.id not in drafted
    assert users == {
        "operator@demo.local": ("OPERATOR", "Demo Operator", False),
        "owner@demo.local": ("OWNER", "Demo Owner", False),
    }


@pytest.mark.parametrize(
    "seed_day",
    [date(2026, 1, 7), date(2026, 2, 28), date(2026, 4, 16), date(2026, 6, 1), date(2026, 7, 19), date(2026, 9, 30), date(2026, 12, 25), date(2027, 3, 3)],
)
def test_T7_1_demo_report_is_the_same_whatever_the_seed_day(seed_day):
    fresh_database()
    assert seed(local(seed_day.year, seed_day.month, seed_day.day, 11, 0)) == 0
    assert counts()["drafts"] == 6
    check_demo_report(report_date_for(seed_day))


def test_T7_2_seeding_twice_exits_1_and_changes_nothing(seeded, capsys):
    before = counts()
    assert seed(SEED_AT + timedelta(hours=1)) == 1
    assert "Demo company already exists." in capsys.readouterr().out
    assert counts() == before


def test_T7_3_owner_pay_raise_moves_value_range(seeded):
    report_date = report_date_for(local_date(SEED_AT, "America/Los_Angeles"))
    with session_scope() as db:
        before = compute_kpis(db, seeded, report_date)
        owner_pay = db.scalar(select(Addback).where(Addback.category == AddbackCategory.OWNER_PAY))
        assert owner_pay.annual_amount_cents == 12000000
        owner_pay.annual_amount_cents += 3000000
        db.commit()
        after = compute_kpis(db, seeded, report_date)
    assert after.sde - before.sde == 3000000
    assert after.value_low - before.value_low == 7500000
    assert after.value_high - before.value_high == 10500000


def test_T7_4_approve_and_send_delgado_in_demo(seeded, app_factory):
    from fastapi.testclient import TestClient

    with freeze_time(SEED_AT + timedelta(minutes=5)), TestClient(app_factory(), follow_redirects=False) as client:
        assert login(client, "owner@demo.local", "demo-password-123").status_code == 302
        assert "Delgado Property Mgmt" in client.get("/reminders").text
        reminder_id = _delgado_id()
        with session_scope() as db:
            reminder = db.get(Reminder, reminder_id)
            assert reminder.template_level == 3
            body = reminder.body.replace("so we can settle it together", "so we can sort it out together")
        token = csrf_from(client, "/reminders")
        response = client.post(
            f"/reminders/{reminder_id}/approve",
            data={"to_email": reminder.to_email, "subject": reminder.subject, "body": body, "csrf_token": token},
        )
        assert response.status_code == 303
        follow = client.get("/reminders").text
        assert "SENT" in follow
    with session_scope() as db:
        sent = db.get(Reminder, reminder_id)
    assert sent.status == ReminderStatus.SENT
    assert "sort it out together" in sent.body
    eml = Path("outbox") / f"reminder-{seeded}-{reminder_id}.eml"
    assert eml.exists()
    assert "Delgado Property Mgmt" in eml.read_text()


def _delgado_id() -> int:
    with session_scope() as db:
        invoice = db.scalar(select(Invoice).where(Invoice.customer_name == "Delgado Property Mgmt"))
        return db.scalar(select(Reminder.id).where(Reminder.invoice_id == invoice.id))


def test_pitch_walkthrough_pages_render(seeded, app_factory):
    """Steps 1-6 of the pitch as the owner, then the operator's pages, with no error page."""
    from fastapi.testclient import TestClient

    with freeze_time(SEED_AT + timedelta(minutes=10)), TestClient(app_factory(), follow_redirects=False) as client:
        login(client, "owner@demo.local", "demo-password-123")
        home = client.get("/")
        assert home.status_code == 200
        assert "Delgado Property Mgmt owes $4,800, 73 days past due." in home.text  # inside the report srcdoc
        assert "-18.0% vs 4-week average" in home.text
        assert "Reminders to review: 6" in home.text
        for path in ("/invoices", "/reminders", "/addbacks", "/account"):
            assert client.get(path).status_code == 200, path
        assert client.get("/operator").status_code == 403

        addbacks = client.get("/addbacks").text
        owner_pay_id = re.search(r'action="/addbacks/(\d+)"', addbacks).group(1)
        token = csrf_from(client, "/addbacks")
        saved = client.post(
            f"/addbacks/{owner_pay_id}",
            data={"category": "OWNER_PAY", "label": "Owner pay", "amount": "150,000", "note": "", "csrf_token": token},
        )
        assert saved.status_code == 303
        assert "Roughly what your business would sell for today, from your own books." in client.get("/addbacks").text

        client.cookies.clear()
        login(client, "operator@demo.local", "demo-password-123")
        assert client.get("/operator").status_code == 200
        company_id = _company_id()
        for tab in ("overview", "connection", "report", "settings", "recipients", "users"):
            assert client.get(f"/companies/{company_id}?tab={tab}").status_code == 200, tab
        preview = client.get(f"/companies/{company_id}/preview")
        assert preview.status_code == 200 and "Preview (not saved, not sent)" in preview.text
        assert client.get("/companies/new").status_code == 200


def _company_id() -> int:
    with session_scope() as db:
        return db.scalar(select(Company.id).where(Company.name == DEMO_COMPANY_NAME))
