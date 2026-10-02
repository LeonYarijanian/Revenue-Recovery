"""Server settings (.env) and company settings validation (Section 13)."""

import re
import sys
from decimal import Decimal, InvalidOperation
from functools import lru_cache

from cryptography.fernet import Fernet
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.enums import AccountingMethod, SendMode


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    APP_SECRET_KEY: str = ""
    TOKEN_ENCRYPTION_KEY: str = ""
    DATABASE_URL: str = "sqlite:///./kpi.db"
    SCHEDULER_ENABLED: bool = True
    PUBLIC_BASE_URL: str = "http://localhost:8000"
    LOG_LEVEL: str = "INFO"
    QBO_CLIENT_ID: str = ""
    QBO_CLIENT_SECRET: str = ""
    QBO_REDIRECT_URI: str = "http://localhost:8000/qbo/callback"
    QBO_ENVIRONMENT: str = "sandbox"
    ALLOW_LIVE_EMAIL: bool = False
    POSTMARK_SERVER_TOKEN: str = ""
    REPORT_FROM_EMAIL: str = ""
    REPORT_FROM_NAME: str = ""
    OPERATOR_EMAIL: str = ""
    SENDER_DOMAIN_VERIFIED: bool = False


@lru_cache
def get_settings() -> Settings:
    return Settings()


def startup_errors(settings: Settings) -> list[str]:
    errors = []
    if len(settings.APP_SECRET_KEY) < 32:
        errors.append("APP_SECRET_KEY is missing or shorter than 32 characters.")
    try:
        Fernet(settings.TOKEN_ENCRYPTION_KEY.encode())
    except (ValueError, TypeError):
        errors.append("TOKEN_ENCRYPTION_KEY is missing or not a valid Fernet key.")
    return errors


def exit_if_invalid(settings: Settings) -> None:
    errors = startup_errors(settings)
    if errors:
        for error in errors:
            print(f"Error: {error}", file=sys.stderr)
        sys.exit(1)


# ---------------------------------------------------------------------------
# company_settings fields
# ---------------------------------------------------------------------------

SETTING_DEFAULTS = {
    "send_mode": SendMode.DEMO.value,
    "report_send_time": "07:00",
    "report_recipients": [],
    "accounting_method": AccountingMethod.ACCRUAL.value,
    "multiple_low": Decimal("2.5"),
    "multiple_high": Decimal("3.5"),
    "runway_low_weeks": Decimal("8.0"),
    "ar_severe_min_cents": 250000,
    "ar_aging_pct": Decimal("20.0"),
    "revenue_drop_pct": Decimal("15.0"),
    "margin_target_pct": Decimal("40.0"),
    "dso_high_days": Decimal("45.0"),
    "margin_drop_points": Decimal("5.0"),
    "reminders_enabled": True,
    "reminder_min_days_past_due": 7,
    "reminder_max_per_invoice": 4,
    "reminder_reply_to": "",
    "company_phone_e164": "",
    "payment_instructions": "",
}

DECIMAL_RANGES = {
    "multiple_low": (Decimal("1.0"), Decimal("6.0")),
    "multiple_high": (Decimal("1.0"), Decimal("8.0")),
    "runway_low_weeks": (Decimal("1.0"), Decimal("52.0")),
    "ar_aging_pct": (Decimal("5.0"), Decimal("80.0")),
    "revenue_drop_pct": (Decimal("5.0"), Decimal("50.0")),
    "margin_target_pct": (Decimal("5.0"), Decimal("90.0")),
    "dso_high_days": (Decimal("10.0"), Decimal("180.0")),
    "margin_drop_points": (Decimal("1.0"), Decimal("30.0")),
}

INTEGER_RANGES = {
    "ar_severe_min_cents": (0, 100000000),
    "reminder_min_days_past_due": (1, 60),
    "reminder_max_per_invoice": (1, 6),
}

def _parse_decimal(raw) -> Decimal | None:
    try:
        value = Decimal(str(raw or "").strip())
    except InvalidOperation:
        return None
    return value if value.is_finite() else None


E164_RE = re.compile(r"^\+[1-9]\d{1,14}$")
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
MAX_RECIPIENTS = 5


def validate_company_settings(form: dict) -> tuple[dict, list[str]]:
    """Validate the Settings tab form. Returns (clean values, errors).

    `form` holds raw strings; a missing checkbox means false.
    """
    from app.reminders import banned_word_error
    from app.validation import is_valid_email

    clean: dict = {}
    errors: list[str] = []

    send_mode = (form.get("send_mode") or SendMode.DEMO.value).strip().upper()
    if send_mode not in {m.value for m in SendMode}:
        errors.append("send_mode must be DEMO or LIVE.")
    clean["send_mode"] = send_mode

    send_time = (form.get("report_send_time") or "").strip()
    if not TIME_RE.match(send_time) or not ("07:00" <= send_time <= "10:00"):
        errors.append("report_send_time must be from 07:00 to 10:00.")
    clean["report_send_time"] = send_time

    method = (form.get("accounting_method") or "").strip().upper()
    if method not in {m.value for m in AccountingMethod}:
        errors.append("accounting_method must be ACCRUAL or CASH.")
    clean["accounting_method"] = method

    for name, (low, high) in DECIMAL_RANGES.items():
        value = _parse_decimal(form.get(name))
        clean[name] = value
        ok = value is not None and low <= value <= high
        if name == "multiple_high":
            multiple_low = clean.get("multiple_low")
            if ok and multiple_low is not None and value < multiple_low:
                ok = False
            if not ok:
                errors.append(f"multiple_high must be from {low} to {high}, and at least multiple_low.")
        elif not ok:
            errors.append(f"{name} must be from {low} to {high}.")

    for name, (low, high) in INTEGER_RANGES.items():
        raw = (form.get(name) or "").strip()
        value = int(raw) if re.fullmatch(r"-?\d+", raw) else None
        if value is None or not (low <= value <= high):
            errors.append(f"{name} must be a whole number from {low} to {high}.")
        clean[name] = value

    reply_to = (form.get("reminder_reply_to") or "").strip().lower()
    if reply_to and not is_valid_email(reply_to):
        errors.append("reminder_reply_to must be a valid email.")
    clean["reminder_reply_to"] = reply_to

    phone = (form.get("company_phone_e164") or "").strip()
    if phone and not E164_RE.match(phone):
        errors.append("company_phone_e164 must be empty or a valid E.164 number, for example +18185550100.")
    clean["company_phone_e164"] = phone

    instructions = (form.get("payment_instructions") or "").strip()
    if len(instructions) > 300:
        errors.append("payment_instructions must be 300 characters or fewer.")
    banned = banned_word_error(instructions)
    if banned:
        errors.append(f"payment_instructions: {banned}")
    clean["payment_instructions"] = instructions

    enabled = str(form.get("reminders_enabled", "")).lower() in {"on", "true", "1", "yes"}
    if enabled and (not phone or not reply_to):
        errors.append("reminders_enabled requires company_phone_e164 and reminder_reply_to.")
    clean["reminders_enabled"] = enabled

    return clean, errors


def validate_recipients(raw: str | list[str]) -> tuple[list[str], list[str]]:
    """Validate report_recipients: 0 to 5 valid emails, one per line or comma-separated."""
    from app.validation import is_valid_email

    if isinstance(raw, str):
        items = [part.strip().lower() for part in re.split(r"[\s,;]+", raw) if part.strip()]
    else:
        items = [part.strip().lower() for part in raw if part.strip()]
    unique = list(dict.fromkeys(items))
    errors = []
    if len(unique) > MAX_RECIPIENTS:
        errors.append(f"report_recipients must be 0 to {MAX_RECIPIENTS} valid emails.")
    invalid = [email for email in unique if not is_valid_email(email)]
    if invalid:
        errors.append(f"report_recipients must be 0 to {MAX_RECIPIENTS} valid emails. Not valid: {', '.join(invalid)}")
    return unique, errors
