"""Phase 2: the 7 issue rules and how the top 3 are chosen."""

from datetime import timedelta
from decimal import Decimal

from sqlalchemy import delete, select

from app.db import session_scope
from app.enums import IssueCode
from app.issues import NO_RED_FLAGS, IssueRules, evaluate, margin_drop_triggers, shown, shown_lines
from app.kpis import compute_kpis
from app.models import BankBalance, CompanySettings, Invoice, WeeklyFinancial
from app.money import q1
from app.weeks import to_db
from tests.fixtures import R, local


def triggered(base):
    with session_scope() as db:
        settings = db.scalar(select(CompanySettings).where(CompanySettings.company_id == base.company_id))
        return evaluate(compute_kpis(db, base.company_id, R), IssueRules.from_settings(settings))


def codes(issues):
    return [issue.code for issue in issues]


def test_T2_1_base_fixture_triggers_five_and_shows_three(base):
    issues = triggered(base)
    assert codes(issues) == [
        IssueCode.RUNWAY_LOW,
        IssueCode.AR_SEVERE,
        IssueCode.AR_AGING,
        IssueCode.REVENUE_DROP,
        IssueCode.MARGIN_DROP,
    ]
    assert codes(shown(issues)) == [IssueCode.RUNWAY_LOW, IssueCode.AR_SEVERE, IssueCode.AR_AGING]


def test_T2_2_exact_issue_lines(base):
    assert [issue.line for issue in shown(triggered(base))] == [
        "Cash covers about 5.7 weeks of costs.",
        "Delgado LLC owes $4,800, 73 days past due.",
        "37.5% of what customers owe you is over 60 days past due.",
    ]


def test_T2_2_exact_action_lines(base):
    assert [issue.action for issue in triggered(base)] == [
        "Collect overdue invoices first and hold off on big purchases.",
        "Call them this week.",
        "Review the overdue list below and approve the reminder drafts.",
        "Check whether jobs slipped into this week or new leads dropped.",
        "Look at last week's biggest jobs for cost overruns.",
    ]


def test_T2_3_ar_severe_below_threshold(base):
    with session_scope() as db:
        db.get(Invoice, base.invoice_ids["INV-104"]).balance_cents = 249999
        db.commit()
        k = compute_kpis(db, base.company_id, R)
    assert q1(k.ar_61_plus_pct) == Decimal("27.0")
    issues = triggered(base)
    assert IssueCode.AR_SEVERE not in codes(issues)
    assert IssueCode.AR_AGING in codes(issues)
    assert shown(issues)[2].line == "Revenue was 20.0% below your 4-week average."


def test_T2_4_ar_severe_tie_goes_to_most_days(base):
    with session_scope() as db:
        db.execute(delete(Invoice))
        for doc, customer, days in (("INV-201", "Seventy Days Co", 70), ("INV-202", "Eighty Days Co", 80)):
            due = R - timedelta(days=days)
            db.add(
                Invoice(
                    company_id=base.company_id,
                    source_id=doc,
                    doc_number=doc,
                    customer_name=customer,
                    customer_email="",
                    txn_date=due,
                    due_date=due,
                    total_cents=500000,
                    balance_cents=500000,
                    is_open=True,
                    last_synced_at=to_db(local(2026, 10, 12, 6)),
                )
            )
        db.commit()
    severe = [issue for issue in triggered(base) if issue.code == IssueCode.AR_SEVERE]
    assert severe[0].line == "Eighty Days Co owes $5,000, 80 days past due."


def test_T2_5_no_red_flags(base):
    with session_scope() as db:
        for row in db.scalars(select(WeeklyFinancial)):
            row.revenue_cents, row.cogs_cents, row.opex_cents = 4000000, 2200000, 1000000
            row.other_expense_cents, row.net_income_cents = 0, 800000
        db.execute(delete(Invoice).where(Invoice.doc_number != "INV-101"))
        db.execute(delete(BankBalance))
        db.add(BankBalance(company_id=base.company_id, as_of=to_db(local(2026, 10, 12, 6)), total_cents=100000000, account_count=1))
        db.commit()
        k = compute_kpis(db, base.company_id, R)
    assert k.runway_weeks == Decimal("31.25")
    issues = triggered(base)
    assert issues == []
    assert shown_lines(issues) == [NO_RED_FLAGS]
    assert NO_RED_FLAGS == "No red flags this week."


def test_T2_6_no_bank_balance_means_no_runway_issue(base):
    with session_scope() as db:
        db.execute(delete(BankBalance))
        db.commit()
    assert IssueCode.RUNWAY_LOW not in codes(triggered(base))


def test_T2_7_runway_threshold_setting(base):
    with session_scope() as db:
        settings = db.scalar(select(CompanySettings).where(CompanySettings.company_id == base.company_id))
        settings.runway_low_weeks = Decimal("5.0")
        db.commit()
    assert IssueCode.RUNWAY_LOW not in codes(triggered(base))


def test_T2_8_margin_drop_boundary():
    assert margin_drop_triggers(Decimal("37.4"), Decimal("42.4"), Decimal("5.0")) is True
    assert margin_drop_triggers(Decimal("37.5"), Decimal("42.4"), Decimal("5.0")) is False
