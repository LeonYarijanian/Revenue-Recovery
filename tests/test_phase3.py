"""Phase 3: data sources, QuickBooks OAuth and sync. Time is frozen at 2026-10-12 10:00 UTC unless stated."""

import base64
import json
from datetime import UTC, date, datetime
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from freezegun import freeze_time
from itsdangerous import TimestampSigner
from sqlalchemy import func, select

from app.crypto import decrypt
from app.db import session_scope
from app.enums import ConnectionStatus, SyncStatus
from app.models import Invoice, QboConnection, WeeklyFinancial
from app.sources import qbo as qbo_module
from app.sources import qbo_oauth
from app.sources.base import WeekData
from app.sources.demo import DemoDataSource
from app.sources.qbo import QboDataSource, parse_profit_and_loss
from app.sync import run_sync
from app.weeks import to_db
from tests.conftest import TEST_SECRET, login
from tests.fake_intuit import FakeIntuit, invoice_page, load, make_qbo_company

NOW = datetime(2026, 10, 12, 10, 0, tzinfo=UTC)


@pytest.fixture
def frozen():
    with freeze_time(NOW):
        yield NOW


@pytest.fixture
def intuit(monkeypatch):
    return FakeIntuit().install(monkeypatch)


def session_data(client) -> dict:
    signed = client.cookies.get("kpi_session").encode()
    payload = TimestampSigner(TEST_SECRET).unsign(signed)
    return json.loads(base64.b64decode(payload))


def connection(company_id: int) -> QboConnection:
    with session_scope() as db:
        return db.scalar(select(QboConnection).where(QboConnection.company_id == company_id))


def sync(company_id: int, now=NOW):
    with session_scope() as db:
        return run_sync(db, company_id, now)


def test_T3_1_demo_source_is_deterministic_and_offline(monkeypatch):
    calls = []
    original_send = httpx.Client.send
    monkeypatch.setattr(httpx.Client, "send", lambda self, *a, **k: calls.append(a) or original_send(self, *a, **k))
    first, second = DemoDataSource(date(2026, 10, 12)), DemoDataSource(date(2026, 10, 12))
    assert [first.fetch_week(ws) for ws in first.week_starts] == [second.fetch_week(ws) for ws in second.week_starts]
    assert first.fetch_open_invoices() == second.fetch_open_invoices()
    assert first.fetch_bank_balance() == second.fetch_bank_balance()
    assert len(first.week_starts) == 60
    assert calls == []


def test_T3_2_connect_redirects_to_intuit_with_state(client, base, frozen):
    company_id = make_qbo_company(NOW)
    login(client, "operator@test.local")
    response = client.get(f"/qbo/connect?company_id={company_id}")
    assert response.status_code == 302
    location = urlparse(response.headers["location"])
    assert f"{location.scheme}://{location.netloc}{location.path}" == qbo_oauth.AUTHORIZATION_URL
    params = parse_qs(location.query)
    assert params["client_id"] == ["test-client-id"]
    assert params["response_type"] == ["code"]
    assert params["scope"] == ["com.intuit.quickbooks.accounting"]
    assert params["redirect_uri"] == ["http://localhost:8000/qbo/callback"]
    state = params["state"][0]
    assert len(state) >= 24
    session = session_data(client)
    assert session["qbo_state"] == state
    assert session["qbo_company_id"] == company_id


def test_T3_3_callback_with_wrong_state_is_400(client, base, frozen, intuit):
    company_id = make_qbo_company(NOW, name="Fresh Co")
    with session_scope() as db:
        db.delete(db.scalar(select(QboConnection).where(QboConnection.company_id == company_id)))
        db.commit()
    login(client, "operator@test.local")
    client.get(f"/qbo/connect?company_id={company_id}")
    response = client.get("/qbo/callback?code=abc&realmId=123&state=not-the-state")
    assert response.status_code == 400
    assert connection(company_id) is None
    assert intuit.requests == []


def test_T3_4_callback_saves_encrypted_tokens_and_expiry(client, base, frozen, intuit):
    company_id = make_qbo_company(NOW, name="Fresh Co")
    with session_scope() as db:
        db.delete(db.scalar(select(QboConnection).where(QboConnection.company_id == company_id)))
        db.commit()
    login(client, "operator@test.local")
    client.get(f"/qbo/connect?company_id={company_id}")
    state = session_data(client)["qbo_state"]
    response = client.get(f"/qbo/callback?code=auth-code-1&realmId=9341453050&state={state}")
    assert response.status_code == 302
    saved = connection(company_id)
    assert saved.status == ConnectionStatus.CONNECTED
    assert saved.realm_id == "9341453050"
    assert saved.access_expires_at == datetime(2026, 10, 12, 11, 0)
    token = load("token_response.json")
    assert saved.access_token_enc != token["access_token"]
    assert saved.refresh_token_enc != token["refresh_token"]
    assert token["access_token"] not in saved.access_token_enc
    assert decrypt(saved.access_token_enc) == token["access_token"]
    assert decrypt(saved.refresh_token_enc) == token["refresh_token"]
    exchange = intuit.posts_to(qbo_oauth.TOKEN_URL)[0]
    assert parse_qs(exchange.content.decode()) == {
        "grant_type": ["authorization_code"],
        "code": ["auth-code-1"],
        "redirect_uri": ["http://localhost:8000/qbo/callback"],
    }
    assert exchange.headers["authorization"].startswith("Basic ")
    # The callback runs the first sync.
    assert len(intuit.pnl_requests()) == 60


def test_T3_5_refresh_when_access_token_expires_within_5_minutes(base, intuit):
    company_id = make_qbo_company(NOW)  # access token expires at 11:00 UTC
    with freeze_time("2026-10-12 10:56:00"), session_scope() as db:
        conn = db.scalar(select(QboConnection).where(QboConnection.company_id == company_id))
        source = QboDataSource(db, conn, datetime(2026, 10, 12, 10, 56, tzinfo=UTC))
        balance = source.fetch_bank_balance()
    assert balance.total_cents == 2000000 and balance.account_count == 2
    assert intuit.refreshes == 1
    assert intuit.requests[0].method == "POST" and str(intuit.requests[0].url) == qbo_oauth.TOKEN_URL
    assert parse_qs(intuit.requests[0].content.decode())["refresh_token"] == ["refresh-0"]
    assert intuit.requests[1].url.host == "sandbox-quickbooks.api.intuit.com"
    assert intuit.requests[1].headers["authorization"] == "Bearer access-1"
    saved = connection(company_id)
    assert decrypt(saved.refresh_token_enc) == "refresh-1"
    assert saved.access_expires_at == datetime(2026, 10, 12, 11, 56)


def test_T3_5_no_refresh_when_token_is_fresh(base, intuit, frozen):
    company_id = make_qbo_company(NOW)
    with session_scope() as db:
        conn = db.scalar(select(QboConnection).where(QboConnection.company_id == company_id))
        QboDataSource(db, conn, NOW).fetch_bank_balance()
    assert intuit.refreshes == 0


def test_T3_6_api_401_refreshes_once_and_retries_once(base, intuit, frozen):
    company_id = make_qbo_company(NOW)
    intuit.api_401_remaining = 1
    run = sync(company_id)
    assert run.status == SyncStatus.SUCCEEDED
    assert intuit.refreshes == 1
    first = intuit.api_requests()[0]
    same_call = [r for r in intuit.api_requests() if r.url == first.url]
    assert len(same_call) == 2
    assert same_call[0].headers["authorization"] == "Bearer access-0"
    assert same_call[1].headers["authorization"] == "Bearer access-1"


def test_T3_7_failed_refresh_needs_reconnect(base, intuit, frozen):
    company_id = make_qbo_company(NOW)
    intuit.api_401_remaining = 1
    intuit.refresh_fails = True
    run = sync(company_id)
    assert run.status == SyncStatus.FAILED
    assert "invalid_grant" in run.error_text
    assert connection(company_id).status == ConnectionStatus.NEEDS_RECONNECT


def test_T3_7_failed_proactive_refresh_needs_reconnect(base, intuit):
    company_id = make_qbo_company(NOW)
    intuit.refresh_fails = True
    later = datetime(2026, 10, 12, 10, 58, tzinfo=UTC)
    with freeze_time(later):
        run = sync(company_id, later)
    assert run.status == SyncStatus.FAILED
    assert connection(company_id).status == ConnectionStatus.NEEDS_RECONNECT


def test_T3_8_parse_profit_and_loss_fixture():
    assert parse_profit_and_loss(load("pnl_week.json")) == WeekData(
        revenue_cents=3200000,
        cogs_cents=2000000,
        opex_cents=1000000,
        other_income_cents=0,
        other_expense_cents=0,
        net_income_cents=200000,
    )


def test_T3_9_invoice_paging(base, intuit, frozen):
    company_id = make_qbo_company(NOW)
    intuit.invoice_pages = [invoice_page(1000, first_id=1), invoice_page(3, first_id=5001)]
    run = sync(company_id)
    assert run.status == SyncStatus.SUCCEEDED
    queries = intuit.invoice_queries()
    assert len(queries) == 2
    assert "STARTPOSITION 1 MAXRESULTS 1000" in queries[0]
    assert "STARTPOSITION 1001 MAXRESULTS 1000" in queries[1]
    assert queries[0] == "select * from Invoice where Balance > '0' STARTPOSITION 1 MAXRESULTS 1000"
    with session_scope() as db:
        assert db.scalar(select(func.count(Invoice.id)).where(Invoice.company_id == company_id)) == 1003
    assert run.invoices_synced == 1003


def test_T3_9_invoice_fields_are_parsed(base, intuit, frozen):
    company_id = make_qbo_company(NOW)
    sync(company_id)
    with session_scope() as db:
        rows = {i.source_id: i for i in db.scalars(select(Invoice).where(Invoice.company_id == company_id))}
    assert rows["130"].doc_number == "1037"
    assert rows["130"].customer_name == "Sonnenschein Family Store"
    assert rows["130"].customer_email == "familiystore@intuit.com"
    assert rows["130"].balance_cents == 36207
    assert rows["131"].total_cents == 125000 and rows["131"].balance_cents == 25000
    assert rows["132"].customer_email == ""
    assert rows["132"].due_date == date(2026, 7, 1)


def test_T3_10_missing_invoice_closes_but_keeps_disputed(base, intuit, frozen):
    company_id = make_qbo_company(NOW)
    with session_scope() as db:
        db.add(
            Invoice(
                company_id=company_id,
                source_id="999",
                doc_number="OLD-1",
                customer_name="Paid Since",
                customer_email="paid@example.com",
                txn_date=date(2026, 8, 1),
                due_date=date(2026, 9, 1),
                total_cents=50000,
                balance_cents=50000,
                is_open=True,
                is_disputed=True,
                last_synced_at=to_db(NOW),
            )
        )
        db.commit()
    assert sync(company_id).status == SyncStatus.SUCCEEDED
    with session_scope() as db:
        old = db.scalar(select(Invoice).where(Invoice.company_id == company_id, Invoice.source_id == "999"))
        assert (old.is_open, old.balance_cents, old.is_disputed) == (False, 0, True)


def test_T3_11_bank_error_rolls_back_everything(base, intuit, frozen):
    company_id = make_qbo_company(NOW)
    intuit.bank_error_status = 500
    run = sync(company_id)
    assert run.status == SyncStatus.FAILED
    assert len(intuit.pnl_requests()) == 60  # the profit-and-loss calls succeeded first
    with session_scope() as db:
        assert db.scalar(select(func.count(WeeklyFinancial.id)).where(WeeklyFinancial.company_id == company_id)) == 0
        assert db.scalar(select(func.count(Invoice.id)).where(Invoice.company_id == company_id)) == 0


def test_T3_11_failed_later_sync_keeps_existing_rows_unchanged(base, intuit, frozen):
    company_id = make_qbo_company(NOW)
    assert sync(company_id).status == SyncStatus.SUCCEEDED
    with session_scope() as db:
        before = db.scalar(select(func.count(WeeklyFinancial.id)).where(WeeklyFinancial.company_id == company_id))
    intuit.bank_error_status = 503
    intuit.pnl = {"Rows": {"Row": []}}
    assert sync(company_id).status == SyncStatus.FAILED
    with session_scope() as db:
        rows = db.scalars(select(WeeklyFinancial).where(WeeklyFinancial.company_id == company_id)).all()
    assert len(rows) == before == 60
    assert all(row.revenue_cents == 3200000 for row in rows)


def test_T3_12_first_sync_60_weeks_then_6(base, intuit, frozen):
    company_id = make_qbo_company(NOW)
    first = sync(company_id)
    assert len(intuit.pnl_requests()) == 60
    assert first.weeks_synced == 60
    params = intuit.pnl_requests()[-1].url.params
    assert (params["start_date"], params["end_date"], params["accounting_method"]) == (
        "2026-10-05",
        "2026-10-11",
        "Accrual",
    )
    intuit.reset()
    second = sync(company_id)
    assert len(intuit.pnl_requests()) == 6
    assert second.weeks_synced == 6


def test_T3_12_pace_is_150_ms_per_company(base, intuit, frozen, monkeypatch):
    waits = []
    monkeypatch.setattr(qbo_module, "SLEEP", waits.append)
    company_id = make_qbo_company(NOW)
    sync(company_id)
    assert len(waits) == len(intuit.api_requests()) - 1
    assert all(0 < wait <= 0.15 for wait in waits)


def test_T3_13_no_writes_to_quickbooks(client, base, intuit, frozen):
    company_id = make_qbo_company(NOW, name="Fresh Co")
    with session_scope() as db:
        db.delete(db.scalar(select(QboConnection).where(QboConnection.company_id == company_id)))
        db.commit()
    login(client, "operator@test.local")
    client.get(f"/qbo/connect?company_id={company_id}")
    state = session_data(client)["qbo_state"]
    client.get(f"/qbo/callback?code=c&realmId=9341453050&state={state}")
    intuit.api_401_remaining = 1
    sync(company_id)
    with freeze_time("2026-10-12 10:58:00"):
        sync(company_id, datetime(2026, 10, 12, 10, 58, tzinfo=UTC))
    page = client.get(f"/companies/{company_id}?tab=connection").text
    token = page.split('name="csrf_token" value="')[1].split('"')[0]
    client.post("/qbo/disconnect", data={"company_id": company_id, "csrf_token": token})

    assert intuit.api_requests(), "the flow should have called the API"
    writes = [r for r in intuit.api_requests() if r.method in {"POST", "PUT", "DELETE"}]
    assert writes == []
    assert all(r.method == "GET" for r in intuit.api_requests())
    allowed_posts = {qbo_oauth.TOKEN_URL, qbo_oauth.REVOKE_URL}
    assert {str(r.url.copy_with(query=None)) for r in intuit.requests if r.method != "GET"} <= allowed_posts


def test_T3_14_disconnect_revokes_and_deletes_tokens(client, base, intuit, frozen):
    company_id = make_qbo_company(NOW)
    login(client, "operator@test.local")
    page = client.get(f"/companies/{company_id}?tab=connection").text
    token = page.split('name="csrf_token" value="')[1].split('"')[0]
    response = client.post("/qbo/disconnect", data={"company_id": company_id, "csrf_token": token})
    assert response.status_code == 303
    revokes = intuit.posts_to(qbo_oauth.REVOKE_URL)
    assert len(revokes) == 1
    assert json.loads(revokes[0].content) == {"token": "refresh-0"}
    saved = connection(company_id)
    assert saved.access_token_enc is None and saved.refresh_token_enc is None
    assert saved.status == ConnectionStatus.DISCONNECTED


def test_owner_can_connect_only_own_company(client, base, frozen):
    company_id = make_qbo_company(NOW, with_owner="books-owner@test.local")
    login(client, "owner@test.local")
    assert client.get(f"/qbo/connect?company_id={company_id}").status_code == 404
    client.cookies.clear()
    login(client, "books-owner@test.local")
    assert client.get("/qbo/connect").status_code == 302
