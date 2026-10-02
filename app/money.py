"""Integer-cent money and display rounding. Never floats, never round()."""

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

NO_DATA = "No data"
ONE = Decimal("1")
TENTH = Decimal("0.1")
CENT = Decimal("0.01")


def to_cents(value) -> int:
    """QuickBooks decimal string (or Decimal/int) to integer cents: "1234.56" -> 123456."""
    if value is None or value == "":
        return 0
    amount = value if isinstance(value, Decimal) else Decimal(str(value))
    return int((amount.quantize(CENT, rounding=ROUND_HALF_UP) * 100).to_integral_value())


def parse_dollars(text: str) -> int:
    """Form input in dollars ("120,000" or "$4,800.00") to cents. Raises ValueError."""
    cleaned = (text or "").replace("$", "").replace(",", "").strip()
    try:
        amount = Decimal(cleaned)
    except InvalidOperation as exc:
        raise ValueError("Enter an amount in dollars, for example 120000.") from exc
    if not amount.is_finite():
        raise ValueError("Enter an amount in dollars, for example 120000.")
    return to_cents(amount)


def round_cents(value) -> int:
    """Decimal cents (for example an average) to whole cents, half up."""
    return int(Decimal(value).quantize(ONE, rounding=ROUND_HALF_UP))


def q1(value) -> Decimal:
    """1 decimal place, half up."""
    return Decimal(value).quantize(TENTH, rounding=ROUND_HALF_UP)


def fmt_1dp(value) -> str:
    return NO_DATA if value is None else str(q1(value))


def fmt_signed_1dp(value) -> str:
    if value is None:
        return NO_DATA
    rounded = q1(value)
    if rounded > 0:
        return f"+{rounded}"
    if rounded == 0:
        return "0.0"
    return str(rounded)


def fmt_dollars(cents) -> str:
    """Whole dollars with commas, half up from cents: 3249950 -> $32,500."""
    if cents is None:
        return NO_DATA
    dollars = int((Decimal(cents) / 100).quantize(ONE, rounding=ROUND_HALF_UP))
    sign = "-" if dollars < 0 else ""
    return f"{sign}${abs(dollars):,}"


def fmt_dollars_cents(cents: int) -> str:
    """Dollars with commas and 2 decimals: 480000 -> $4,800.00."""
    amount = (Decimal(cents) / 100).quantize(CENT, rounding=ROUND_HALF_UP)
    sign = "-" if amount < 0 else ""
    return f"{sign}${abs(amount):,.2f}"
