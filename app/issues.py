"""The 7 fixed issue rules from Section 8, in priority order.

Comparisons use the 1-decimal figures the owner reads (Section 15 works the
rules that way, for example 37.5 <= 43.4 - 5.0).
"""

from dataclasses import dataclass
from decimal import Decimal

from app.enums import IssueCode
from app.kpis import Kpis
from app.money import fmt_dollars, q1

NO_RED_FLAGS = "No red flags this week."
MAX_SHOWN = 3


@dataclass(frozen=True)
class IssueRules:
    runway_low_weeks: Decimal = Decimal("8.0")
    ar_severe_min_cents: int = 250000
    ar_aging_pct: Decimal = Decimal("20.0")
    revenue_drop_pct: Decimal = Decimal("15.0")
    margin_target_pct: Decimal = Decimal("40.0")
    dso_high_days: Decimal = Decimal("45.0")
    margin_drop_points: Decimal = Decimal("5.0")

    @classmethod
    def from_settings(cls, settings) -> "IssueRules":
        return cls(
            runway_low_weeks=settings.runway_low_weeks,
            ar_severe_min_cents=settings.ar_severe_min_cents,
            ar_aging_pct=settings.ar_aging_pct,
            revenue_drop_pct=settings.revenue_drop_pct,
            margin_target_pct=settings.margin_target_pct,
            dso_high_days=settings.dso_high_days,
            margin_drop_points=settings.margin_drop_points,
        )


@dataclass(frozen=True)
class Issue:
    code: IssueCode
    line: str
    action: str


def margin_drop_triggers(margin_week_pct, margin_4wk_pct, drop_points) -> bool:
    if margin_week_pct is None or margin_4wk_pct is None:
        return False
    return q1(margin_week_pct) <= q1(margin_4wk_pct) - q1(drop_points)


def evaluate(k: Kpis, rules: IssueRules) -> list[Issue]:
    """Every triggered rule, in priority order."""
    triggered: list[Issue] = []

    if k.runway_weeks is not None and q1(k.runway_weeks) < q1(rules.runway_low_weeks):
        triggered.append(
            Issue(
                IssueCode.RUNWAY_LOW,
                f"Cash covers about {q1(k.runway_weeks)} weeks of costs.",
                "Collect overdue invoices first and hold off on big purchases.",
            )
        )

    severe = [
        inv
        for inv in k.invoices
        if inv.days_past_due >= 61 and inv.balance_cents >= rules.ar_severe_min_cents
    ]
    if severe:
        worst = max(severe, key=lambda inv: (inv.balance_cents, inv.days_past_due))
        triggered.append(
            Issue(
                IssueCode.AR_SEVERE,
                f"{worst.customer_name} owes {fmt_dollars(worst.balance_cents)}, {worst.days_past_due} days past due.",
                "Call them this week.",
            )
        )

    if k.ar_61_plus_pct is not None and q1(k.ar_61_plus_pct) > q1(rules.ar_aging_pct):
        triggered.append(
            Issue(
                IssueCode.AR_AGING,
                f"{q1(k.ar_61_plus_pct)}% of what customers owe you is over 60 days past due.",
                "Review the overdue list below and approve the reminder drafts.",
            )
        )

    if k.revenue_change_pct is not None and q1(k.revenue_change_pct) <= -q1(rules.revenue_drop_pct):
        triggered.append(
            Issue(
                IssueCode.REVENUE_DROP,
                f"Revenue was {abs(q1(k.revenue_change_pct))}% below your 4-week average.",
                "Check whether jobs slipped into this week or new leads dropped.",
            )
        )

    if k.margin_4wk_pct is not None and q1(k.margin_4wk_pct) < q1(rules.margin_target_pct):
        triggered.append(
            Issue(
                IssueCode.MARGIN_LOW,
                f"Gross margin over 4 weeks is {q1(k.margin_4wk_pct)}%, under your {q1(rules.margin_target_pct)}% target.",
                "Check pricing and material costs on your last 10 jobs.",
            )
        )

    if k.dso_days is not None and q1(k.dso_days) > q1(rules.dso_high_days):
        triggered.append(
            Issue(
                IssueCode.DSO_HIGH,
                f"Customers take about {q1(k.dso_days)} days to pay.",
                "Ask for payment when the job is done and shorten due dates.",
            )
        )

    if margin_drop_triggers(k.margin_week_pct, k.margin_4wk_pct, rules.margin_drop_points):
        triggered.append(
            Issue(
                IssueCode.MARGIN_DROP,
                f"Last week's gross margin was {q1(k.margin_week_pct)}%, down from {q1(k.margin_4wk_pct)}% over 4 weeks.",
                "Look at last week's biggest jobs for cost overruns.",
            )
        )

    return triggered


def shown(triggered: list[Issue]) -> list[Issue]:
    return triggered[:MAX_SHOWN]
