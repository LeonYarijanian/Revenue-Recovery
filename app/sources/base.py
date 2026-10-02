"""The DataSource interface (Section 6). All money is integer cents."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class WeekData:
    revenue_cents: int
    cogs_cents: int
    opex_cents: int
    other_income_cents: int
    other_expense_cents: int
    net_income_cents: int


@dataclass(frozen=True)
class InvoiceData:
    source_id: str
    doc_number: str
    customer_name: str
    customer_email: str
    txn_date: date
    due_date: date
    total_cents: int
    balance_cents: int


@dataclass(frozen=True)
class BankBalanceData:
    total_cents: int
    account_count: int


class DataSource(ABC):
    @abstractmethod
    def fetch_week(self, week_start: date) -> WeekData:
        """Revenue, COGS, operating expenses, other income, other expenses and net income for one week."""

    @abstractmethod
    def fetch_open_invoices(self) -> list[InvoiceData]:
        """Every invoice with a balance above 0."""

    @abstractmethod
    def fetch_bank_balance(self) -> BankBalanceData:
        """Total current balance of all Bank-type accounts, and the number of accounts."""
