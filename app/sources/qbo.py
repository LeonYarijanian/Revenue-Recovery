"""QboDataSource: profit and loss, open invoices and bank balances (read only)."""

import json
import time
from datetime import date, datetime, timedelta
from decimal import Decimal

import httpx
from sqlalchemy.orm import Session

from app.crypto import decrypt
from app.enums import AccountingMethod, QboEnvironment
from app.models import QboConnection
from app.money import to_cents
from app.sources import qbo_oauth
from app.sources.base import BankBalanceData, DataSource, InvoiceData, WeekData

PAGE_SIZE = 1000
MIN_REQUEST_INTERVAL = 0.15  # at most 1 request every 150 ms per company
SLEEP = time.sleep  # tests replace this

PNL_GROUPS = {
    "Income": "revenue_cents",
    "COGS": "cogs_cents",
    "Expenses": "opex_cents",
    "OtherIncome": "other_income_cents",
    "OtherExpenses": "other_expense_cents",
    "NetIncome": "net_income_cents",
}
METHOD_PARAM = {AccountingMethod.ACCRUAL: "Accrual", AccountingMethod.CASH: "Cash"}


class QboApiError(Exception):
    pass


def parse_profit_and_loss(payload: dict) -> WeekData:
    """Top-level Rows.Row entries by group; each total is the last Summary.ColData value."""
    values = dict.fromkeys(PNL_GROUPS.values(), 0)
    for row in (payload.get("Rows") or {}).get("Row") or []:
        field = PNL_GROUPS.get(row.get("group"))
        if field is None:
            continue
        columns = (row.get("Summary") or {}).get("ColData") or []
        values[field] = to_cents(columns[-1].get("value")) if columns else 0
    return WeekData(**values)


def parse_invoices(payload: dict) -> list[InvoiceData]:
    invoices = []
    for item in (payload.get("QueryResponse") or {}).get("Invoice") or []:
        txn_date = date.fromisoformat(item["TxnDate"])
        due = item.get("DueDate")
        invoices.append(
            InvoiceData(
                source_id=str(item["Id"]),
                doc_number=str(item.get("DocNumber") or "")[:40],
                customer_name=str((item.get("CustomerRef") or {}).get("name") or "")[:120],
                customer_email=str((item.get("BillEmail") or {}).get("Address") or "").strip().lower()[:254],
                txn_date=txn_date,
                due_date=date.fromisoformat(due) if due else txn_date,
                total_cents=to_cents(item.get("TotalAmt")),
                balance_cents=to_cents(item.get("Balance")),
            )
        )
    return invoices


def parse_bank_accounts(payload: dict) -> BankBalanceData:
    accounts = (payload.get("QueryResponse") or {}).get("Account") or []
    return BankBalanceData(
        total_cents=sum(to_cents(account.get("CurrentBalance")) for account in accounts),
        account_count=len(accounts),
    )


class QboDataSource(DataSource):
    def __init__(
        self,
        db: Session,
        connection: QboConnection,
        now: datetime,
        accounting_method: str = AccountingMethod.ACCRUAL,
        client: httpx.Client | None = None,
    ):
        self.db = db
        self.connection = connection
        self.now = now
        self.method = METHOD_PARAM[AccountingMethod(accounting_method)]
        self.client = client or qbo_oauth.http_client()
        self.base = qbo_oauth.API_BASE[QboEnvironment(connection.environment)]
        self._last_request: float | None = None

    def _pace(self) -> None:
        if self._last_request is not None:
            wait = MIN_REQUEST_INTERVAL - (time.monotonic() - self._last_request)
            if wait > 0:
                SLEEP(wait)
        self._last_request = time.monotonic()

    def _send(self, path: str, params: dict, token: str) -> httpx.Response:
        self._pace()
        return self.client.get(
            self.base + path,
            params=params,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        )

    def _get(self, path: str, params: dict) -> dict:
        token = qbo_oauth.ensure_access_token(self.db, self.connection, self.client, self.now)
        response = self._send(path, params, token)
        if response.status_code == 401:
            qbo_oauth.refresh(self.db, self.connection, self.client, self.now)
            response = self._send(path, params, decrypt(self.connection.access_token_enc))
        if response.status_code != 200:
            raise QboApiError(f"QuickBooks returned HTTP {response.status_code}.")
        return json.loads(response.text, parse_float=Decimal)

    def _path(self, name: str) -> str:
        return f"/v3/company/{self.connection.realm_id}/{name}"

    def fetch_week(self, week_start: date) -> WeekData:
        payload = self._get(
            self._path("reports/ProfitAndLoss"),
            {
                "start_date": week_start.isoformat(),
                "end_date": (week_start + timedelta(days=6)).isoformat(),
                "accounting_method": self.method,
            },
        )
        return parse_profit_and_loss(payload)

    def fetch_open_invoices(self) -> list[InvoiceData]:
        invoices: list[InvoiceData] = []
        start = 1
        while True:
            query = f"select * from Invoice where Balance > '0' STARTPOSITION {start} MAXRESULTS {PAGE_SIZE}"
            page = parse_invoices(self._get(self._path("query"), {"query": query}))
            invoices.extend(page)
            if len(page) < PAGE_SIZE:
                return invoices
            start += PAGE_SIZE

    def fetch_bank_balance(self) -> BankBalanceData:
        query = "select * from Account where AccountType = 'Bank'"
        return parse_bank_accounts(self._get(self._path("query"), {"query": query}))
