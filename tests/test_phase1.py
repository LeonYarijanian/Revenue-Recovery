"""Phase 1: weeks and KPI formulas, worked through with the Section 15 numbers."""

from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import delete

from app.db import session_scope
from app.enums import AddbackCategory, AgingBucket
from app.kpis import NOT_MEANINGFUL, compute_kpis, display
from app.models import Addback, Invoice, WeeklyFinancial
from app.money import NO_DATA, fmt_dollars, q1, round_cents
from app.weeks import report_week_start, week_label, week_start_n
from tests.fixtures import W0, R


def kpis(base):
    with session_scope() as db:
        return compute_kpis(db, base.company_id, R)


def replace_weeks(company_id: int, net_incomes: list[int]) -> None:
    """Replace every week with len(net_incomes) weeks ending at W0 (newest last)."""
    with session_scope() as db:
        db.execute(delete(WeeklyFinancial).where(WeeklyFinancial.company_id == company_id))
        count = len(net_incomes)
        for i, net in enumerate(net_incomes):
            db.add(
                WeeklyFinancial(
                    company_id=company_id,
                    week_start=W0 - timedelta(weeks=count - 1 - i),
                    revenue_cents=4000000,
                    cogs_cents=2000000,
                    opex_cents=1000000,
                    other_income_cents=0,
                    other_expense_cents=1000000 - net,
                    net_income_cents=net,
                )
            )
        db.commit()


def add_addbacks(company_id: int, amounts: list[int]) -> None:
    with session_scope() as db:
        for amount, category in zip(amounts, AddbackCategory, strict=False):
            db.add(Addback(company_id=company_id, category=category, label=category.title(), annual_amount_cents=amount))
        db.commit()


SPEC_ADDBACKS = [12000000, 800000, 1500000, 700000]


def test_T1_1_week_helpers():
    assert week_start_n(R, 0) == date(2026, 10, 5)
    assert report_week_start(R) == date(2026, 10, 5)
    assert week_start_n(R, 1) == date(2026, 9, 28)
    assert week_label(date(2026, 10, 5)) == "Oct 5 to Oct 11, 2026"
    with pytest.raises(ValueError):
        report_week_start(date(2026, 10, 13))


def test_T1_2_revenue(base):
    k = kpis(base)
    assert k.revenue_week == 3200000
    assert round_cents(k.revenue_avg_4) == 4000000
    assert q1(k.revenue_change_pct) == Decimal("-20.0")
    assert display(k)["revenue_change_pct"] == "-20.0"


def test_T1_3_margins(base):
    k = kpis(base)
    assert q1(k.margin_week_pct) == Decimal("37.5")
    assert q1(k.margin_4wk_pct) == Decimal("43.4")


def test_T1_4_receivables(base):
    k = kpis(base)
    assert {bucket: k.aging[bucket]["count"] for bucket in AgingBucket} == {bucket: 1 for bucket in AgingBucket}
    assert k.ar_total == 1600000
    assert q1(k.ar_61_plus_pct) == Decimal("37.5")
    days = {inv.doc_number: (inv.days_past_due, inv.bucket) for inv in k.invoices}
    assert days == {
        "INV-101": (-8, AgingBucket.CURRENT),
        "INV-102": (12, AgingBucket.D1_30),
        "INV-103": (45, AgingBucket.D31_60),
        "INV-104": (73, AgingBucket.D61_90),
        "INV-105": (133, AgingBucket.D91_PLUS),
    }


def test_T1_5_dso(base):
    assert q1(kpis(base).dso_days) == Decimal("2.9")


def test_T1_6_burn_and_runway(base):
    k = kpis(base)
    assert round_cents(k.weekly_burn) == 3160000
    assert q1(k.runway_weeks) == Decimal("5.7")


def test_T1_7_ttm_with_only_5_weeks(base):
    k = kpis(base)
    assert k.ttm_weeks == 5
    assert k.ttm_net_income is None
    assert k.sde is None
    shown = display(k)
    assert shown["ttm_net_income"] == NO_DATA
    assert shown["sde"] == NO_DATA


def test_T1_8_sde_and_value(base):
    replace_weeks(base.company_id, [300000] * 48 + [900000] * 4)
    add_addbacks(base.company_id, SPEC_ADDBACKS)
    k = kpis(base)
    assert k.ttm_weeks == 52
    assert k.ttm_net_income == 18000000
    assert k.addbacks_total == 15000000
    assert k.sde == 33000000
    assert k.value_low == 82500000
    assert k.value_high == 115500000
    assert not k.ttm_annualized


def test_T1_9_annualized(base):
    weeks = [346154] * 25 + [346150]
    assert sum(weeks) == 9000000
    replace_weeks(base.company_id, weeks)
    k = kpis(base)
    assert k.ttm_weeks == 26
    assert k.ttm_net_income == 18000000
    assert k.ttm_label == "annualized from 26 weeks"


def test_T1_10_negative_sde(base):
    weeks = [-384615] * 51 + [-384635]
    assert sum(weeks) == -20000000
    replace_weeks(base.company_id, weeks)
    add_addbacks(base.company_id, SPEC_ADDBACKS)
    k = kpis(base)
    assert k.sde == -5000000
    shown = display(k)
    assert shown["value_low"] == NOT_MEANINGFUL
    assert shown["value_high"] == NOT_MEANINGFUL
    assert NOT_MEANINGFUL == "Not meaningful: earnings are zero or negative."


def test_T1_11_whole_dollar_display():
    assert fmt_dollars(3249950) == "$32,500"
    assert fmt_dollars(3249949) == "$32,499"


def test_T1_12_missing_data(base):
    with session_scope() as db:
        db.execute(delete(WeeklyFinancial).where(WeeklyFinancial.week_start == W0))
        db.execute(delete(Invoice))
        db.commit()
    shown = display(kpis(base))
    assert shown["revenue_week"] == NO_DATA
    assert shown["revenue_change_pct"] == NO_DATA
    assert shown["ar_total"] == "$0"
    assert shown["ar_61_plus_pct"] == NO_DATA
