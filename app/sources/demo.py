"""DemoDataSource: fixed fake HVAC books (Section 14), no network calls.

Every date is placed relative to the anchor day (the seed day, or the day a
demo sync runs), and everything random uses random.Random(42), so the same
anchor always gives the same data.
"""

import random
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from app.sources.base import BankBalanceData, DataSource, InvoiceData, WeekData
from app.weeks import last_complete_week_starts, monday_on_or_before

DEMO_COMPANY_NAME = "Glendale Comfort Air (Demo)"
WEEK_COUNT = 60
BASE_REVENUE_CENTS = Decimal(3800000)
OPEX_CENTS = 1050000
OTHER_EXPENSE_CENTS = 40000
TARGET_RUNWAY_WEEKS = Decimal("5.8")
BANK_ACCOUNT_COUNT = 2

# (doc number, customer, email, days past due on the report date, balance in cents)
DEMO_INVOICES = [
    ("INV-2198", "Brand Blvd Retail", "ap@brandblvd.example.com", 96, 415000),
    ("INV-2207", "Delgado Property Mgmt", "ap@example.com", 73, 480000),
    ("INV-2212", "Verdugo Medical Group", "billing@verdugomed.example.com", 64, 365000),
    ("INV-2215", "Kim Family", "kim.family@example.com", 52, 142000),
    ("INV-2219", "Atwater Cafe", "owner@atwatercafe.example.com", 38, 98000),
    ("INV-2224", "Glenoaks Dentistry", "office@glenoaksdental.example.com", 21, 260000),
    ("INV-2226", "Lopez Residence", "", 12, 75000),
    ("INV-2228", "Chevy Chase Condos", "board@chevychasecondos.example.com", 9, 230000),
    ("INV-2231", "Hillside Dental", "frontdesk@hillsidedental.example.com", -12, 420000),
    ("INV-2233", "Maple Court HOA", "manager@maplecourt.example.com", -20, 385000),
    ("INV-2234", "Sunrise Bakery", "hello@sunrisebakery.example.com", -9, 310000),
    ("INV-2236", "Pasadena Office Park", "facilities@pasadenaop.example.com", -26, 560000),
    ("INV-2237", "Rivera Residence", "rivera.home@example.com", -15, 185000),
    ("INV-2238", "Oak Street Apartments", "office@oakstreetapts.example.com", -30, 290000),
]
DEMO_NO_EMAIL_DOC = "INV-2226"
DEMO_DISPUTED_DOC = "INV-2228"


def _cents(value: Decimal) -> int:
    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def season_factor(week_start: date) -> Decimal:
    if week_start.month in (6, 7, 8, 9):
        return Decimal("1.35")
    if week_start.month in (12, 1, 2):
        return Decimal("1.10")
    return Decimal("0.90")


def _week(revenue: int, cogs: int) -> WeekData:
    return WeekData(
        revenue_cents=revenue,
        cogs_cents=cogs,
        opex_cents=OPEX_CENTS,
        other_income_cents=0,
        other_expense_cents=OTHER_EXPENSE_CENTS,
        net_income_cents=revenue - cogs - OPEX_CENTS - OTHER_EXPENSE_CENTS,
    )


class DemoDataSource(DataSource):
    def __init__(self, anchor: date):
        self.anchor = anchor
        self.report_date = monday_on_or_before(anchor)
        self.week_starts = last_complete_week_starts(anchor, WEEK_COUNT)
        self._weeks = self._build_weeks()

    def _build_weeks(self) -> dict[date, WeekData]:
        rng = random.Random(42)
        weeks: dict[date, WeekData] = {}
        for week_start in self.week_starts[:-1]:
            noise = Decimal(rng.randint(-800, 800)) / 10000
            revenue = _cents(BASE_REVENUE_CENTS * season_factor(week_start) * (1 + noise))
            cogs_share = Decimal(5500 + rng.randint(-300, 300)) / 10000
            weeks[week_start] = _week(revenue, _cents(revenue * cogs_share))
        # Last week (W0) overrides the pattern.
        w0 = self.week_starts[-1]
        previous_four = self.week_starts[-5:-1]
        average = Decimal(sum(weeks[ws].revenue_cents for ws in previous_four)) / 4
        revenue0 = _cents(average * Decimal("0.82"))
        weeks[w0] = _week(revenue0, _cents(revenue0 * Decimal("0.62")))
        return weeks

    def fetch_week(self, week_start: date) -> WeekData:
        return self._weeks.get(week_start) or _week(0, 0)

    def fetch_open_invoices(self) -> list[InvoiceData]:
        invoices = []
        for doc_number, customer, email, days, balance in DEMO_INVOICES:
            due = self.report_date - timedelta(days=days)
            invoices.append(
                InvoiceData(
                    source_id=f"demo-{doc_number}",
                    doc_number=doc_number,
                    customer_name=customer,
                    customer_email=email,
                    txn_date=due - timedelta(days=30),
                    due_date=due,
                    total_cents=balance,
                    balance_cents=balance,
                )
            )
        return invoices

    def fetch_bank_balance(self) -> BankBalanceData:
        recent = [self._weeks[ws] for ws in self.week_starts[-8:]]
        burn = Decimal(sum(w.cogs_cents + w.opex_cents + w.other_expense_cents for w in recent)) / len(recent)
        return BankBalanceData(total_cents=_cents(burn * TARGET_RUNWAY_WEEKS), account_count=BANK_ACCOUNT_COUNT)
