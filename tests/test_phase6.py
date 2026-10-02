"""Phase 6: LIVE mode, with Postmark mocked."""

import json
from email.utils import parseaddr

import httpx
import pytest
from sqlalchemy import select

from app import mailer
from app.config import SETTING_DEFAULTS, get_settings
from app.crypto import encrypt
from app.db import session_scope
from app.enums import ConnectionStatus, QboEnvironment, ReminderStatus, ReportStatus, SendMode, SourceType
from app.mailer import live_precondition_failures
from app.models import Company, CompanySettings, QboConnection, Reminder, Report, User
from app.reminders import approve, draft_reminders, send_reminders
from app.report import generate_report, send_reports
from app.weeks import to_db
from tests.conftest import login, post_form
from tests.fixtures import local

LIVE_ENV = {
    "ALLOW_LIVE_EMAIL": "true",
    "POSTMARK_SERVER_TOKEN": "pm-server-token",
    "REPORT_FROM_EMAIL": "reports@yourdomain.com",
    "REPORT_FROM_NAME": "Your Brand Reports",
    "OPERATOR_EMAIL": "you@yourdomain.com",
    "SENDER_DOMAIN_VERIFIED": "true",
    "PUBLIC_BASE_URL": "https://kpi.yourdomain.com",
}


def live_env(monkeypatch, **overrides):
    for name, value in {**LIVE_ENV, **overrides}.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()


class FakePostmark:
    def __init__(self, error_code: int = 0):
        self.error_code = error_code
        self.posts: list[dict] = []
        self.headers: list[httpx.Headers] = []

    def install(self, monkeypatch) -> "FakePostmark":
        monkeypatch.setattr(mailer, "TRANSPORT", httpx.MockTransport(self.handler))
        return self

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert str(request.url) == mailer.POSTMARK_URL and request.method == "POST"
        self.posts.append(json.loads(request.content))
        self.headers.append(request.headers)
        if self.error_code == 0:
            return httpx.Response(200, json={"ErrorCode": 0, "Message": "OK", "MessageID": f"pm-{len(self.posts)}"})
        return httpx.Response(422, json={"ErrorCode": self.error_code, "Message": "Invalid 'To' address."})


def make_ready(base, environment=QboEnvironment.PRODUCTION, recipients=("owner@test.local", "cfo@test.local")):
    """Turn Test HVAC into a QBO company with a connection and report recipients."""
    with session_scope() as db:
        db.get(Company, base.company_id).source_type = SourceType.QBO
        db.add(
            QboConnection(
                company_id=base.company_id,
                realm_id="9341453050",
                environment=environment,
                status=ConnectionStatus.CONNECTED,
                access_token_enc=encrypt("access"),
                refresh_token_enc=encrypt("refresh"),
                access_expires_at=to_db(local(2026, 10, 12, 8, 0)),
            )
        )
        settings = db.scalar(select(CompanySettings).where(CompanySettings.company_id == base.company_id))
        settings.report_recipients = list(recipients)
        db.commit()


def set_mode(base, mode: SendMode):
    with session_scope() as db:
        db.scalar(select(CompanySettings).where(CompanySettings.company_id == base.company_id)).send_mode = mode
        db.commit()


def current_settings(base) -> CompanySettings:
    with session_scope() as db:
        return db.scalar(select(CompanySettings).where(CompanySettings.company_id == base.company_id))


def settings_form(**overrides) -> dict:
    form = {name: str(value) for name, value in SETTING_DEFAULTS.items() if name != "report_recipients"}
    form.update(
        reminders_enabled="on",
        report_enabled="on",
        company_phone_e164="+18185550100",
        reminder_reply_to="owner@test.local",
    )
    form.update(overrides)
    return {key: value for key, value in form.items() if value is not None}


def save_settings(client, base, **overrides):
    page = f"/companies/{base.company_id}?tab=settings"
    return post_form(client, f"/companies/{base.company_id}/settings", settings_form(**overrides), csrf_page=page)


def failures_for(base):
    with session_scope() as db:
        return live_precondition_failures(db, db.get(Company, base.company_id))


def test_T6_1_live_refused_without_allow_live_email(client, base, monkeypatch):
    make_ready(base)
    live_env(monkeypatch, ALLOW_LIVE_EMAIL="false")
    login(client, "operator@test.local")
    response = save_settings(client, base, send_mode="LIVE")
    assert response.status_code == 400
    assert "1. ALLOW_LIVE_EMAIL=true is not set in .env." in response.text
    assert current_settings(base).send_mode == SendMode.DEMO


def test_T6_1_every_failure_is_listed(base):
    failures = failures_for(base)
    assert [failure[:2] for failure in failures] == ["1.", "2.", "3.", "4.", "5.", "6."]


def test_T6_2_demo_source_refused_on_precondition_6(client, base, monkeypatch):
    with session_scope() as db:
        settings = db.scalar(select(CompanySettings).where(CompanySettings.company_id == base.company_id))
        settings.report_recipients = ["owner@test.local"]
        db.commit()
    live_env(monkeypatch)
    failures = failures_for(base)
    assert len(failures) == 1 and failures[0].startswith("6.")
    login(client, "operator@test.local")
    response = save_settings(client, base, send_mode="LIVE")
    assert response.status_code == 400
    assert "6. The company&#39;s source must be QBO with a PRODUCTION QuickBooks connection." in response.text
    assert current_settings(base).send_mode == SendMode.DEMO


def test_T6_3_sandbox_connection_refused_on_precondition_6(base, monkeypatch):
    make_ready(base, environment=QboEnvironment.SANDBOX)
    live_env(monkeypatch)
    failures = failures_for(base)
    assert len(failures) == 1 and failures[0].startswith("6.")


def test_T6_4_live_report_goes_to_postmark(client, base, monkeypatch):
    make_ready(base)
    live_env(monkeypatch)
    postmark = FakePostmark().install(monkeypatch)
    login(client, "operator@test.local")
    assert save_settings(client, base, send_mode="LIVE").status_code == 303
    assert current_settings(base).send_mode == SendMode.LIVE

    with session_scope() as db:
        report = generate_report(db, base.company_id, local(2026, 10, 12, 6, 30))
        send_reports(db, local(2026, 10, 12, 7, 0))
        report = db.get(Report, report.id)
    assert len(postmark.posts) == 2
    assert sorted(post["To"] for post in postmark.posts) == ["cfo@test.local", "owner@test.local"]
    for post, headers in zip(postmark.posts, postmark.headers, strict=True):
        assert post["MessageStream"] == "outbound"
        assert post["ReplyTo"] == "you@yourdomain.com"
        assert post["From"] == "Your Brand Reports <reports@yourdomain.com>"
        assert post["HtmlBody"] == report.html
        assert "Delgado LLC owes $4,800, 73 days past due." in post["TextBody"]
        assert post["Subject"] == "Test HVAC weekly report: revenue $32,000 (-20.0% vs avg)"
        assert headers["X-Postmark-Server-Token"] == "pm-server-token"
    assert report.status == ReportStatus.SENT
    assert report.recipients == ["owner@test.local", "cfo@test.local"]


def test_T6_5_postmark_error_marks_report_failed(base, monkeypatch):
    make_ready(base, recipients=("owner@test.local",))
    set_mode(base, SendMode.LIVE)
    live_env(monkeypatch)
    FakePostmark(error_code=300).install(monkeypatch)
    with session_scope() as db:
        report = generate_report(db, base.company_id, local(2026, 10, 12, 6, 30))
        send_reports(db, local(2026, 10, 12, 7, 0))
        report = db.get(Report, report.id)
    assert report.status == ReportStatus.FAILED
    assert "300" in report.error_text


def test_T6_5_live_never_sends_when_a_precondition_breaks_later(base, monkeypatch):
    make_ready(base)
    set_mode(base, SendMode.LIVE)
    live_env(monkeypatch, ALLOW_LIVE_EMAIL="false")
    postmark = FakePostmark().install(monkeypatch)
    with session_scope() as db:
        report = generate_report(db, base.company_id, local(2026, 10, 12, 6, 30))
        send_reports(db, local(2026, 10, 12, 7, 0))
        report = db.get(Report, report.id)
    assert postmark.posts == []
    assert report.status == ReportStatus.FAILED
    assert "ALLOW_LIVE_EMAIL" in report.error_text


def test_T6_6_live_reminder_from_company_with_reply_to(base, monkeypatch):
    make_ready(base)
    set_mode(base, SendMode.LIVE)
    live_env(monkeypatch)
    postmark = FakePostmark().install(monkeypatch)
    with session_scope() as db:
        draft_reminders(db, base.company_id, local(2026, 10, 12, 6, 30))
        target = db.scalar(select(Reminder).where(Reminder.invoice_id == base.invoice_ids["INV-104"]))
        approve(db, target, db.get(User, base.owner_id), local(2026, 10, 12, 8, 0))
        send_reminders(db, local(2026, 10, 12, 9, 0))
        target = db.get(Reminder, target.id)
    assert target.status == ReminderStatus.SENT
    assert target.provider_message_id == "pm-1"
    assert len(postmark.posts) == 1
    post = postmark.posts[0]
    name, address = parseaddr(post["From"])
    assert (name, address) == ("Test HVAC", "reports@yourdomain.com")
    assert post["ReplyTo"] == "owner@test.local"
    assert post["To"] == "ap@delgado.example.com"
    assert post["MessageStream"] == "outbound"
    assert post["TextBody"].endswith(f"https://kpi.yourdomain.com/r/{target.token}")


def test_T6_7_out_of_range_settings_rejected(client, base):
    login(client, "operator@test.local")
    response = save_settings(
        client,
        base,
        multiple_low="2.5",
        multiple_high="2.0",
        report_send_time="06:00",
        payment_instructions="No late fee if paid this week.",
    )
    assert response.status_code == 400
    assert "multiple_high must be from 1.0 to 8.0, and at least multiple_low." in response.text
    assert "report_send_time must be from 07:00 to 10:00." in response.text
    assert "payment_instructions: Remove &#34;late fee&#34;. Reminders must stay polite and must not threaten." in response.text
    page = f"/companies/{base.company_id}?tab=recipients"
    six = "\n".join(f"person{i}@test.local" for i in range(6))
    recipients = post_form(client, f"/companies/{base.company_id}/recipients", {"recipients": six}, csrf_page=page)
    assert recipients.status_code == 400
    assert "report_recipients must be 0 to 5 valid emails." in recipients.text
    saved = current_settings(base)
    assert (saved.multiple_high, saved.report_send_time, saved.payment_instructions, saved.report_recipients) == (
        SETTING_DEFAULTS["multiple_high"],
        "07:00",
        "",
        [],
    )


@pytest.mark.parametrize(
    "field, value",
    [
        ("multiple_low", "6.5"),
        ("runway_low_weeks", "0.5"),
        ("ar_severe_min_cents", "100000001"),
        ("ar_aging_pct", "81"),
        ("revenue_drop_pct", "4.9"),
        ("margin_target_pct", "91"),
        ("dso_high_days", "9"),
        ("margin_drop_points", "31"),
        ("reminder_min_days_past_due", "0"),
        ("reminder_max_per_invoice", "7"),
        ("company_phone_e164", "818-555-0100"),
        ("reminder_reply_to", "not-an-email"),
    ],
)
def test_T6_7_each_range_is_enforced(client, base, field, value):
    login(client, "operator@test.local")
    response = save_settings(client, base, **{field: value})
    assert response.status_code == 400
    assert field in response.text


def test_T6_7_valid_settings_are_saved(client, base):
    login(client, "operator@test.local")
    response = save_settings(client, base, multiple_low="3.0", multiple_high="4.5", report_send_time="08:15")
    assert response.status_code == 303
    saved = current_settings(base)
    assert (str(saved.multiple_low), str(saved.multiple_high), saved.report_send_time) == ("3.0", "4.5", "08:15")


def test_T6_8_reminders_need_phone(client, base):
    login(client, "operator@test.local")
    response = save_settings(client, base, reminders_enabled="on", company_phone_e164="")
    assert response.status_code == 400
    assert "reminders_enabled requires company_phone_e164 and reminder_reply_to." in response.text
    assert current_settings(base).company_phone_e164 == "+18185550100"


def test_owner_cannot_change_settings(client, base):
    login(client, "owner@test.local")
    response = post_form(client, f"/companies/{base.company_id}/settings", settings_form(send_mode="LIVE"))
    assert response.status_code == 403
