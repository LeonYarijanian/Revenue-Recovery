"""Every Section 7 formula, computed from the synced tables.

Values are Decimal or integer cents; None means "No data". Rounding happens
only for display (and for the issue-rule comparisons, which use the same
1-decimal figures the owner reads).
"""

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.enums import AgingBucket
from app.models import Addback, BankBalance, CompanySettings, Invoice, WeeklyFinancial
from app.money import NO_DATA, fmt_1dp, fmt_dollars, fmt_signed_1dp, q1, round_cents
from app.weeks import report_week_start, week_start_n

NOT_MEANINGFUL = "Not meaningful: earnings are zero or negative."

BUCKET_LABELS = {
    AgingBucket.CURRENT: "Current",
    AgingBucket.D1_30: "1-30 days",
    AgingBucket.D31_60: "31-60 days",
    AgingBucket.D61_90: "61-90 days",
    AgingBucket.D91_PLUS: "91+ days",
}


@dataclass(frozen=True)
class WeekRow:
    week_start: date
    revenue_cents: int
    cogs_cents: int
    opex_cents: int
    other_expense_cents: int
    net_income_cents: int

    @property
    def total_costs_cents(self) -> int:
        return self.cogs_cents + self.opex_cents + self.other_expense_cents


@dataclass(frozen=True)
class InvoiceLine:
    id: int
    doc_number: str
    customer_name: str
    customer_email: str
    due_date: date
    balance_cents: int
    days_past_due: int
    bucket: AgingBucket
    is_disputed: bool


@dataclass
class Kpis:
    report_date: date
    week_start: date
    revenue_week: int | None = None
    revenue_avg_4: Decimal | None = None
    revenue_change_pct: Decimal | None = None
    margin_week_pct: Decimal | None = None
    margin_4wk_pct: Decimal | None = None
    net_income_week: int | None = None
    ar_total: int = 0
    invoices: list[InvoiceLine] = field(default_factory=list)
    aging: dict = field(default_factory=dict)
    ar_61_plus_pct: Decimal | None = None
    dso_days: Decimal | None = None
    cash_on_hand: int | None = None
    weekly_burn: Decimal | None = None
    runway_weeks: Decimal | None = None
    ttm_weeks: int = 0
    ttm_net_income: Decimal | None = None
    ttm_annualized: bool = False
    addbacks_total: int = 0
    sde: Decimal | None = None
    value_low: Decimal | None = None
    value_high: Decimal | None = None
    multiple_low: Decimal = Decimal("2.5")
    multiple_high: Decimal = Decimal("3.5")
    chart: list[tuple[date, int | None]] = field(default_factory=list)
    top_overdue: list[InvoiceLine] = field(default_factory=list)

    @property
    def value_not_meaningful(self) -> bool:
        return self.sde is not None and self.sde <= 0

    @property
    def ttm_label(self) -> str:
        return f"annualized from {self.ttm_weeks} weeks" if self.ttm_annualized else ""


def days_past_due(report_date: date, due_date: date) -> int:
    return (report_date - due_date).days


def aging_bucket(days: int) -> AgingBucket:
    if days <= 0:
        return AgingBucket.CURRENT
    if days <= 30:
        return AgingBucket.D1_30
    if days <= 60:
        return AgingBucket.D31_60
    if days <= 90:
        return AgingBucket.D61_90
    return AgingBucket.D91_PLUS


def _avg(values: list) -> Decimal | None:
    return Decimal(sum(values)) / len(values) if values else None


def calculate(
    report_date: date,
    weeks: dict[date, WeekRow],
    invoices: list[InvoiceLine],
    cash_on_hand: int | None,
    addbacks_total: int,
    multiple_low: Decimal,
    multiple_high: Decimal,
) -> Kpis:
    w = [week_start_n(report_date, n) for n in range(52)]
    k = Kpis(report_date=report_date, week_start=w[0], multiple_low=multiple_low, multiple_high=multiple_high)

    def existing(ns):
        return [weeks[w[n]] for n in ns if w[n] in weeks]

    w0 = weeks.get(w[0])
    if w0 is not None:
        k.revenue_week = w0.revenue_cents
        k.net_income_week = w0.net_income_cents

    avg4 = _avg([row.revenue_cents for row in existing(range(1, 5))])
    k.revenue_avg_4 = avg4 if avg4 is not None and avg4 > 0 else None

    if k.revenue_week is not None and k.revenue_avg_4 is not None:
        k.revenue_change_pct = (Decimal(k.revenue_week) - k.revenue_avg_4) / k.revenue_avg_4 * 100

    if w0 is not None and w0.revenue_cents > 0:
        k.margin_week_pct = Decimal(w0.revenue_cents - w0.cogs_cents) / w0.revenue_cents * 100

    four = existing(range(0, 4))
    revenue_4 = sum(row.revenue_cents for row in four)
    if revenue_4 > 0:
        k.margin_4wk_pct = Decimal(revenue_4 - sum(row.cogs_cents for row in four)) / revenue_4 * 100

    # Receivables
    k.invoices = sorted(invoices, key=lambda inv: (-inv.days_past_due, -inv.balance_cents))
    k.ar_total = sum(inv.balance_cents for inv in invoices)
    k.aging = {bucket: {"count": 0, "amount": 0} for bucket in AgingBucket}
    for inv in invoices:
        k.aging[inv.bucket]["count"] += 1
        k.aging[inv.bucket]["amount"] += inv.balance_cents
    if k.ar_total > 0:
        over_60 = k.aging[AgingBucket.D61_90]["amount"] + k.aging[AgingBucket.D91_PLUS]["amount"]
        k.ar_61_plus_pct = Decimal(over_60) / k.ar_total * 100
    k.top_overdue = [inv for inv in k.invoices if inv.days_past_due >= 1][:5]

    thirteen = existing(range(0, 13))
    revenue_13 = sum(row.revenue_cents for row in thirteen)
    if revenue_13 > 0:
        k.dso_days = Decimal(k.ar_total) / revenue_13 * (len(thirteen) * 7)

    # Cash
    k.cash_on_hand = cash_on_hand
    burn = _avg([row.total_costs_cents for row in existing(range(0, 8))])
    k.weekly_burn = burn if burn is not None and burn > 0 else None
    if k.cash_on_hand is not None and k.weekly_burn is not None:
        k.runway_weeks = Decimal(k.cash_on_hand) / k.weekly_burn

    # Value
    ttm = existing(range(0, 52))
    k.ttm_weeks = len(ttm)
    if k.ttm_weeks >= 13:
        total = Decimal(sum(row.net_income_cents for row in ttm))
        if k.ttm_weeks < 52:
            total = total * 52 / k.ttm_weeks
            k.ttm_annualized = True
        k.ttm_net_income = total
    k.addbacks_total = addbacks_total
    if k.ttm_net_income is not None:
        k.sde = k.ttm_net_income + addbacks_total
        if k.sde > 0:
            k.value_low = k.sde * multiple_low
            k.value_high = k.sde * multiple_high

    k.chart = [(w[n], weeks[w[n]].revenue_cents if w[n] in weeks else None) for n in range(7, -1, -1)]
    return k


def load_invoice_lines(db: Session, company_id: int, report_date: date) -> list[InvoiceLine]:
    rows = db.scalars(
        select(Invoice).where(Invoice.company_id == company_id, Invoice.is_open.is_(True), Invoice.balance_cents > 0)
    ).all()
    lines = []
    for inv in rows:
        days = days_past_due(report_date, inv.due_date)
        lines.append(
            InvoiceLine(
                id=inv.id,
                doc_number=inv.doc_number,
                customer_name=inv.customer_name,
                customer_email=inv.customer_email,
                due_date=inv.due_date,
                balance_cents=inv.balance_cents,
                days_past_due=days,
                bucket=aging_bucket(days),
                is_disputed=inv.is_disputed,
            )
        )
    return lines


def compute_kpis(db: Session, company_id: int, report_date: date) -> Kpis:
    w0 = report_week_start(report_date)
    oldest = week_start_n(report_date, 51)
    rows = db.scalars(
        select(WeeklyFinancial).where(
            WeeklyFinancial.company_id == company_id,
            WeeklyFinancial.week_start >= oldest,
            WeeklyFinancial.week_start <= w0,
        )
    ).all()
    weeks = {
        row.week_start: WeekRow(
            week_start=row.week_start,
            revenue_cents=row.revenue_cents,
            cogs_cents=row.cogs_cents,
            opex_cents=row.opex_cents,
            other_expense_cents=row.other_expense_cents,
            net_income_cents=row.net_income_cents,
        )
        for row in rows
    }
    newest_bank = db.scalar(
        select(BankBalance)
        .where(BankBalance.company_id == company_id)
        .order_by(BankBalance.as_of.desc(), BankBalance.id.desc())
    )
    addbacks_total = db.scalar(
        select(func.coalesce(func.sum(Addback.annual_amount_cents), 0)).where(Addback.company_id == company_id)
    )
    settings = db.scalar(select(CompanySettings).where(CompanySettings.company_id == company_id))
    return calculate(
        report_date=report_date,
        weeks=weeks,
        invoices=load_invoice_lines(db, company_id, report_date),
        cash_on_hand=newest_bank.total_cents if newest_bank else None,
        addbacks_total=int(addbacks_total),
        multiple_low=settings.multiple_low if settings else Decimal("2.5"),
        multiple_high=settings.multiple_high if settings else Decimal("3.5"),
    )


def _cents_or_none(value) -> int | None:
    return None if value is None else round_cents(value)


def _q1_or_none(value) -> str | None:
    return None if value is None else str(q1(value))


def display(k: Kpis) -> dict[str, str]:
    """Every figure as the owner reads it."""
    if k.sde is None:
        value_low = value_high = NO_DATA
    elif k.value_not_meaningful:
        value_low = value_high = NOT_MEANINGFUL
    else:
        value_low, value_high = fmt_dollars(k.value_low), fmt_dollars(k.value_high)
    return {
        "revenue_week": fmt_dollars(k.revenue_week),
        "revenue_avg_4": fmt_dollars(k.revenue_avg_4),
        "revenue_change_pct": fmt_signed_1dp(k.revenue_change_pct),
        "margin_week_pct": fmt_1dp(k.margin_week_pct),
        "margin_4wk_pct": fmt_1dp(k.margin_4wk_pct),
        "net_income_week": fmt_dollars(k.net_income_week),
        "ar_total": fmt_dollars(k.ar_total),
        "ar_61_plus_pct": fmt_1dp(k.ar_61_plus_pct),
        "dso_days": fmt_1dp(k.dso_days),
        "cash_on_hand": fmt_dollars(k.cash_on_hand),
        "weekly_burn": fmt_dollars(k.weekly_burn),
        "runway_weeks": fmt_1dp(k.runway_weeks),
        "ttm_net_income": fmt_dollars(k.ttm_net_income),
        "ttm_label": k.ttm_label,
        "addbacks_total": fmt_dollars(k.addbacks_total),
        "sde": fmt_dollars(k.sde),
        "value_low": value_low,
        "value_high": value_high,
        "multiple_low": fmt_1dp(k.multiple_low),
        "multiple_high": fmt_1dp(k.multiple_high),
    }


def to_metrics(k: Kpis) -> dict:
    """JSON-safe record of every computed number (cents as integers, ratios as 1-decimal strings)."""
    return {
        "report_date": k.report_date.isoformat(),
        "week_start": k.week_start.isoformat(),
        "revenue_week": k.revenue_week,
        "revenue_avg_4": _cents_or_none(k.revenue_avg_4),
        "revenue_change_pct": _q1_or_none(k.revenue_change_pct),
        "margin_week_pct": _q1_or_none(k.margin_week_pct),
        "margin_4wk_pct": _q1_or_none(k.margin_4wk_pct),
        "net_income_week": k.net_income_week,
        "ar_total": k.ar_total,
        "ar_61_plus_pct": _q1_or_none(k.ar_61_plus_pct),
        "dso_days": _q1_or_none(k.dso_days),
        "cash_on_hand": k.cash_on_hand,
        "weekly_burn": _cents_or_none(k.weekly_burn),
        "runway_weeks": _q1_or_none(k.runway_weeks),
        "ttm_weeks": k.ttm_weeks,
        "ttm_net_income": _cents_or_none(k.ttm_net_income),
        "ttm_annualized": k.ttm_annualized,
        "addbacks_total": k.addbacks_total,
        "sde": _cents_or_none(k.sde),
        "value_low": _cents_or_none(k.value_low),
        "value_high": _cents_or_none(k.value_high),
        "multiple_low": str(q1(k.multiple_low)),
        "multiple_high": str(q1(k.multiple_high)),
        "aging": [
            {"bucket": bucket.value, "label": BUCKET_LABELS[bucket], **k.aging[bucket]} for bucket in AgingBucket
        ],
        "top_overdue": [
            {
                "customer": inv.customer_name,
                "doc_number": inv.doc_number,
                "balance": inv.balance_cents,
                "days": inv.days_past_due,
            }
            for inv in k.top_overdue
        ],
        "chart": [{"week_start": ws.isoformat(), "revenue": revenue} for ws, revenue in k.chart],
    }
