"""Phase 0: scaffold, database, login, encryption."""

import os
import subprocess
import sys
from datetime import timedelta

from fastapi.routing import APIRoute
from freezegun import freeze_time
from sqlalchemy import inspect

from app.config import get_settings
from app.crypto import decrypt, encrypt
from app.db import get_engine, session_scope
from app.enums import ReportStatus
from app.models import Report
from tests.conftest import TEST_ENV, csrf_from, login
from tests.fixtures import ROOT, W0, R, fresh_database, local
from app.weeks import to_db

SECTION_5_TABLES = {
    "companies",
    "company_settings",
    "users",
    "login_attempts",
    "qbo_connections",
    "sync_runs",
    "weekly_financials",
    "invoices",
    "bank_balances",
    "addbacks",
    "reports",
    "reminders",
    "suppressions",
}


def test_T0_1_migrate_creates_exactly_13_tables():
    fresh_database()
    tables = set(inspect(get_engine()).get_table_names()) - {"alembic_version"}
    assert tables == SECTION_5_TABLES
    assert len(tables) == 13


def test_T0_2_login_right_and_wrong_password(client, base):
    wrong = login(client, "owner@test.local", "not-the-password")
    assert wrong.status_code == 200
    assert "Email or password is incorrect." in wrong.text

    right = login(client, "owner@test.local")
    assert right.status_code == 302
    assert right.headers["location"] == "/"


def test_T0_2_login_email_is_case_insensitive(client, base):
    assert login(client, "Owner@Test.Local").status_code == 302


def test_T0_3_lockout_after_10_failures_for_15_minutes(client, base):
    start = local(2026, 10, 12, 9, 0)
    with freeze_time(start):
        for _ in range(10):
            response = login(client, "owner@test.local", "wrong-password-x")
            assert "Email or password is incorrect." in response.text
        attempt_11 = login(client, "owner@test.local")
        assert attempt_11.status_code == 200
        assert "Too many attempts. Try again in 15 minutes." in attempt_11.text
    with freeze_time(start + timedelta(minutes=15, seconds=1)):
        later = login(client, "owner@test.local")
        assert later.status_code == 302


def test_T0_4_operator_console_requires_operator(client, base):
    login(client, "owner@test.local")
    assert client.get("/operator").status_code == 403
    client.cookies.clear()
    login(client, "operator@test.local")
    assert client.get("/operator").status_code == 200


def _insert_report(company_id: int) -> int:
    with session_scope() as db:
        report = Report(
            company_id=company_id,
            report_date=R,
            week_start=W0,
            status=ReportStatus.GENERATED,
            html="<p>report</p>",
            metrics={},
            issues=[],
            generated_at=to_db(local(2026, 10, 12, 6, 30)),
            recipients=[],
        )
        db.add(report)
        db.commit()
        return report.id


def test_T0_5_other_company_report_is_404(client, base):
    report_id = _insert_report(base.company_id)
    login(client, "other@test.local")
    assert client.get(f"/reports/{report_id}").status_code == 404
    client.cookies.clear()
    login(client, "owner@test.local")
    assert client.get(f"/reports/{report_id}").status_code == 200


def test_T0_6_encrypt_then_decrypt():
    stored = encrypt("abc")
    assert stored != "abc"
    assert "abc" not in stored
    assert decrypt(stored) == "abc"


def _start_app(env_overrides: dict, tmp_path) -> subprocess.CompletedProcess:
    env = {key: value for key, value in os.environ.items() if key not in TEST_ENV}
    env.update(TEST_ENV)
    env.update({"PYTHONPATH": str(ROOT), "DATABASE_URL": f"sqlite:///{tmp_path / 'start.db'}"})
    for key, value in env_overrides.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    return subprocess.run(
        [sys.executable, "-c", "import app.main"], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60
    )


def test_T0_7_start_without_token_encryption_key_exits_1(tmp_path):
    result = _start_app({"TOKEN_ENCRYPTION_KEY": None}, tmp_path)
    assert result.returncode == 1
    assert "TOKEN_ENCRYPTION_KEY" in result.stderr


def test_T0_7_invalid_key_or_short_secret_exits_1(tmp_path):
    assert _start_app({"TOKEN_ENCRYPTION_KEY": "not-a-fernet-key"}, tmp_path).returncode == 1
    assert _start_app({"APP_SECRET_KEY": "too-short"}, tmp_path).returncode == 1
    assert _start_app({}, tmp_path).returncode == 0


def test_T0_8_every_post_without_csrf_token_is_403(client, base):
    from app.routes import auth, operator, pages, qbo, reminders

    login(client, "operator@test.local")
    post_paths = []
    routes = [route for module in (auth, pages, reminders, qbo, operator) for route in module.router.routes]
    for route in routes:
        if isinstance(route, APIRoute) and "POST" in route.methods and not route.path.startswith("/r/"):
            path = route.path
            for name in route.param_convertors:
                path = path.replace("{" + name + "}", "1")
            post_paths.append(path)
    assert len(post_paths) >= 15
    for path in post_paths:
        response = client.post(path, data={"anything": "1"})
        assert response.status_code == 403, path
    # Login itself also needs the token.
    client.cookies.clear()
    response = client.post("/login", data={"email": "owner@test.local", "password": "test-password-123"})
    assert response.status_code == 403
    # A wrong token is refused too; the right one works.
    token = csrf_from(client, "/login")
    assert client.post("/login", data={"email": "owner@test.local", "password": "x", "csrf_token": "bad"}).status_code == 403
    assert (
        client.post(
            "/login", data={"email": "owner@test.local", "password": "test-password-123", "csrf_token": token}
        ).status_code
        == 302
    )


def test_settings_are_read_from_environment():
    assert get_settings().SCHEDULER_ENABLED is False
