"""Beyond the numbered acceptance tests: scheduler steps, new company flow, sessions."""

import re
from datetime import UTC, datetime, timedelta

from freezegun import freeze_time
from sqlalchemy import func, select

from app.db import session_scope
from app.enums import ReminderStatus, ReportStatus, SyncStatus
from app.models import Company, Reminder, Report, SyncRun, User
from app.scheduler import tick
from tests.conftest import csrf_from, login, post_form
from tests.fake_intuit import FakeIntuit, make_qbo_company
from tests.fixtures import local


def report_status(company_id: int):
    with session_scope() as db:
        report = db.scalar(select(Report).where(Report.company_id == company_id))
        return report.status if report else None


def test_scheduler_monday_draft_generate_and_send(base):
    tick(local(2026, 10, 12, 6, 29))
    assert report_status(base.company_id) is None
    tick(local(2026, 10, 12, 6, 30))
    assert report_status(base.company_id) == ReportStatus.GENERATED
    with session_scope() as db:
        drafts = db.scalar(select(func.count(Reminder.id)).where(Reminder.status == ReminderStatus.DRAFT))
        report = db.scalar(select(Report).where(Report.company_id == base.company_id))
    assert drafts == 3
    assert report.metrics["drafts_count"] == 3
    assert "3 payment reminders are ready for you to review." in report.html
    tick(local(2026, 10, 12, 6, 45))
    assert report_status(base.company_id) == ReportStatus.GENERATED
    tick(local(2026, 10, 12, 7, 0))
    assert report_status(base.company_id) == ReportStatus.SENT


def test_scheduler_skips_reports_on_other_days(base):
    tick(local(2026, 10, 13, 6, 30))
    assert report_status(base.company_id) is None


def test_scheduler_nightly_sync_for_connected_qbo_companies(base, monkeypatch):
    intuit = FakeIntuit().install(monkeypatch)
    now = local(2026, 10, 13, 2, 0)
    with freeze_time(now):
        company_id = make_qbo_company(now.astimezone(UTC))
        tick(now)
    with session_scope() as db:
        runs = db.scalars(select(SyncRun).where(SyncRun.company_id == company_id)).all()
    assert [run.status for run in runs] == [SyncStatus.SUCCEEDED]
    assert len(intuit.pnl_requests()) == 60
    tick(local(2026, 10, 13, 2, 1))
    with session_scope() as db:
        assert db.scalar(select(func.count(SyncRun.id)).where(SyncRun.company_id == company_id)) == 1


def test_new_company_owner_must_change_temporary_password(client, base):
    login(client, "operator@test.local")
    response = post_form(
        client,
        "/companies",
        {
            "name": "Sunset Plumbing",
            "timezone": "America/Denver",
            "industry": "Plumbing",
            "source_type": "QBO",
            "owner_name": "Pat Rivera",
            "owner_email": "Pat@Sunset.Example.com",
        },
        csrf_page="/companies/new",
    )
    assert response.status_code == 303
    page = client.get(response.headers["location"]).text
    password = re.search(r"Temporary password for pat@sunset.example.com: (\S+) ", page).group(1)
    with session_scope() as db:
        company = db.scalar(select(Company).where(Company.name == "Sunset Plumbing"))
        user = db.scalar(select(User).where(User.email == "pat@sunset.example.com"))
        assert (company.timezone, company.source_type) == ("America/Denver", "QBO")
        assert user.must_change_password and user.company_id == company.id

    client.cookies.clear()
    assert login(client, "pat@sunset.example.com", password).status_code == 302
    assert client.get("/").headers["location"] == "/account"
    assert client.get("/invoices").headers["location"] == "/account"
    short = post_form(
        client, "/account", {"current_password": password, "new_password": "short", "confirm_password": "short"}
    )
    assert short.status_code == 400 and "at least 12 characters" in short.text
    changed = post_form(
        client,
        "/account",
        {"current_password": password, "new_password": "a-longer-password", "confirm_password": "a-longer-password"},
    )
    assert changed.status_code == 200 and "Password changed." in changed.text
    assert client.get("/").status_code == 200


def test_session_logs_out_after_12_idle_hours(client, base):
    start = datetime(2026, 10, 12, 16, 0, tzinfo=UTC)
    with freeze_time(start):
        login(client, "owner@test.local")
        assert client.get("/invoices").status_code == 200
    with freeze_time(start + timedelta(hours=11, minutes=59)):
        assert client.get("/invoices").status_code == 200
    with freeze_time(start + timedelta(hours=24)):
        response = client.get("/invoices")
        assert response.status_code == 302 and response.headers["location"] == "/login"


def test_session_cookie_flags(client, base):
    response = login(client, "owner@test.local")
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie
    assert "secure" not in cookie  # PUBLIC_BASE_URL is http:// in tests


def test_logout(client, base):
    login(client, "owner@test.local")
    token = csrf_from(client, "/invoices")
    assert client.post("/logout", data={"csrf_token": token}).status_code == 302
    assert client.get("/invoices").status_code == 302


def test_operator_switches_company_and_records_stay_isolated(client, base):
    login(client, "operator@test.local")
    token = csrf_from(client, "/invoices")
    client.post("/switch-company", data={"company_id": base.other_company_id, "csrf_token": token})
    assert "Baker Home" not in client.get("/invoices").text
    token = csrf_from(client, "/invoices")
    client.post("/switch-company", data={"company_id": base.company_id, "csrf_token": token})
    assert "Baker Home" in client.get("/invoices").text


def test_owner_marks_invoice_disputed_from_invoices_page(client, base):
    login(client, "owner@test.local")
    invoice_id = base.invoice_ids["INV-104"]
    assert post_form(client, f"/invoices/{invoice_id}/dispute", {}, csrf_page="/invoices").status_code == 303
    assert "(disputed)" in client.get("/invoices").text
    assert post_form(client, f"/invoices/{invoice_id}/undispute", {}, csrf_page="/invoices").status_code == 303
    other_owner_invoice = post_form(client, "/invoices/999999/dispute", {}, csrf_page="/invoices")
    assert other_owner_invoice.status_code == 404


def test_addbacks_validation(client, base):
    login(client, "owner@test.local")
    bad = post_form(client, "/addbacks", {"category": "OWNER_PAY", "label": "Pay", "amount": "-5"}, "/addbacks")
    assert bad.status_code == 400 and "0 or more" in bad.text
    good = post_form(client, "/addbacks", {"category": "INTEREST", "label": "Loan", "amount": "$8,000.00"}, "/addbacks")
    assert good.status_code == 303
    assert "$8,000" in client.get("/addbacks").text
