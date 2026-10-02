"""A recorded-response stand-in for Intuit's OAuth and QuickBooks APIs (httpx.MockTransport)."""

import copy
import json
import re
from datetime import datetime, timedelta
from urllib.parse import parse_qs

import httpx

from app.crypto import encrypt
from app.db import session_scope
from app.enums import ConnectionStatus, QboEnvironment, Role, SourceType
from app.models import Company, CompanySettings, QboConnection
from app.sources import qbo_oauth
from app.weeks import to_db
from tests.fixtures import ROOT, make_user

FIXTURES = ROOT / "tests" / "qbo_fixtures"
API_HOSTS = {"sandbox-quickbooks.api.intuit.com", "quickbooks.api.intuit.com"}


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def invoice_page(count: int, first_id: int = 1000) -> dict:
    template = load("invoices_page1.json")["QueryResponse"]["Invoice"][0]
    invoices = []
    for i in range(count):
        invoice = copy.deepcopy(template)
        invoice["Id"] = str(first_id + i)
        invoice["DocNumber"] = f"P-{first_id + i}"
        invoices.append(invoice)
    return {"QueryResponse": {"Invoice": invoices, "startPosition": 1, "maxResults": count}}


class FakeIntuit:
    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.refreshes = 0
        self.api_401_remaining = 0
        self.refresh_fails = False
        self.bank_error_status: int | None = None
        self.invoice_pages = [load("invoices_page1.json")]
        self.pnl = load("pnl_week.json")

    def install(self, monkeypatch) -> "FakeIntuit":
        monkeypatch.setattr(qbo_oauth, "TRANSPORT", httpx.MockTransport(self.handler))
        return self

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url.copy_with(query=None))
        if url == qbo_oauth.TOKEN_URL:
            form = parse_qs(request.content.decode())
            token = load("token_response.json")
            if form.get("grant_type") == ["refresh_token"]:
                self.refreshes += 1
                if self.refresh_fails:
                    return httpx.Response(400, json={"error": "invalid_grant"})
                token["access_token"] = f"access-{self.refreshes}"
                token["refresh_token"] = f"refresh-{self.refreshes}"
            return httpx.Response(200, json=token)
        if url == qbo_oauth.REVOKE_URL:
            return httpx.Response(200)
        if request.url.host in API_HOSTS:
            if self.api_401_remaining > 0:
                self.api_401_remaining -= 1
                return httpx.Response(401, json={"fault": {"type": "AUTHENTICATION"}})
            path = request.url.path
            if path.endswith("/reports/ProfitAndLoss"):
                return httpx.Response(200, json=self.pnl)
            if path.endswith("/query"):
                query = request.url.params["query"]
                if "from Invoice" in query:
                    start = int(re.search(r"STARTPOSITION (\d+)", query).group(1))
                    index = (start - 1) // 1000
                    page = self.invoice_pages[index] if index < len(self.invoice_pages) else {"QueryResponse": {}}
                    return httpx.Response(200, json=page)
                if "from Account" in query:
                    if self.bank_error_status:
                        return httpx.Response(self.bank_error_status, json={"Fault": {"type": "SystemFault"}})
                    return httpx.Response(200, json=load("accounts_bank.json"))
        return httpx.Response(404)

    # -- what happened ----------------------------------------------------
    def api_requests(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.host in API_HOSTS]

    def pnl_requests(self) -> list[httpx.Request]:
        return [r for r in self.api_requests() if r.url.path.endswith("/reports/ProfitAndLoss")]

    def invoice_queries(self) -> list[str]:
        return [
            r.url.params["query"]
            for r in self.api_requests()
            if r.url.path.endswith("/query") and "from Invoice" in r.url.params["query"]
        ]

    def posts_to(self, url: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == "POST" and str(r.url.copy_with(query=None)) == url]

    def reset(self) -> None:
        self.requests.clear()


def make_qbo_company(
    now: datetime,
    name: str = "Books Co",
    environment: QboEnvironment = QboEnvironment.SANDBOX,
    expires_in: timedelta = timedelta(hours=1),
    with_owner: str | None = None,
) -> int:
    """A QBO company with a CONNECTED connection holding tokens access-0 / refresh-0."""
    with session_scope() as db:
        company = Company(name=name, timezone="America/Los_Angeles", industry="HVAC", source_type=SourceType.QBO)
        db.add(company)
        db.flush()
        db.add(CompanySettings(company_id=company.id))
        db.add(
            QboConnection(
                company_id=company.id,
                realm_id="9341453050",
                environment=environment,
                status=ConnectionStatus.CONNECTED,
                access_token_enc=encrypt("access-0"),
                refresh_token_enc=encrypt("refresh-0"),
                access_expires_at=to_db(now + expires_in),
                connected_at=to_db(now - timedelta(days=1)),
                last_refreshed_at=to_db(now),
            )
        )
        if with_owner:
            make_user(db, with_owner, Role.OWNER, company.id)
        db.commit()
        return company.id
