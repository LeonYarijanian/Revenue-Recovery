"""Enums from Section 5. Stored as strings."""

from enum import StrEnum


class Role(StrEnum):
    OPERATOR = "OPERATOR"
    OWNER = "OWNER"


class SourceType(StrEnum):
    DEMO = "DEMO"
    QBO = "QBO"


class QboEnvironment(StrEnum):
    SANDBOX = "SANDBOX"
    PRODUCTION = "PRODUCTION"


class ConnectionStatus(StrEnum):
    NOT_CONNECTED = "NOT_CONNECTED"
    CONNECTED = "CONNECTED"
    NEEDS_RECONNECT = "NEEDS_RECONNECT"
    DISCONNECTED = "DISCONNECTED"


class SyncStatus(StrEnum):
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class AgingBucket(StrEnum):
    CURRENT = "CURRENT"
    D1_30 = "D1_30"
    D31_60 = "D31_60"
    D61_90 = "D61_90"
    D91_PLUS = "D91_PLUS"


class AddbackCategory(StrEnum):
    OWNER_PAY = "OWNER_PAY"
    INTEREST = "INTEREST"
    DEPRECIATION_AMORTIZATION = "DEPRECIATION_AMORTIZATION"
    ONE_TIME = "ONE_TIME"
    OTHER = "OTHER"


class ReportStatus(StrEnum):
    GENERATED = "GENERATED"
    SENT = "SENT"
    HELD = "HELD"
    FAILED = "FAILED"


class ReminderStatus(StrEnum):
    DRAFT = "DRAFT"
    APPROVED = "APPROVED"
    SENT = "SENT"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"


class IssueCode(StrEnum):
    RUNWAY_LOW = "RUNWAY_LOW"
    AR_SEVERE = "AR_SEVERE"
    AR_AGING = "AR_AGING"
    REVENUE_DROP = "REVENUE_DROP"
    MARGIN_LOW = "MARGIN_LOW"
    DSO_HIGH = "DSO_HIGH"
    MARGIN_DROP = "MARGIN_DROP"


class SendMode(StrEnum):
    DEMO = "DEMO"
    LIVE = "LIVE"


class AccountingMethod(StrEnum):
    ACCRUAL = "ACCRUAL"
    CASH = "CASH"


class SuppressionReason(StrEnum):
    UNSUBSCRIBED = "UNSUBSCRIBED"
    HARD_BOUNCE = "HARD_BOUNCE"
    MANUAL = "MANUAL"
