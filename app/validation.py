"""Email validation shared by settings, recipients and reminders."""

import email_validator
from email_validator import EmailNotValidError, validate_email

# The spec's demo and test addresses use the .local domain (owner@demo.local,
# owner@test.local), so .local must count as a valid address domain.
if "local" in email_validator.SPECIAL_USE_DOMAIN_NAMES:
    email_validator.SPECIAL_USE_DOMAIN_NAMES.remove("local")


def is_valid_email(value: str) -> bool:
    if not value:
        return False
    try:
        validate_email(value, check_deliverability=False)
    except EmailNotValidError:
        return False
    return True
