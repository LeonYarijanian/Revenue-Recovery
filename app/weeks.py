"""Week boundaries, report dates and time-zone helpers.

Timestamps are stored as naive UTC datetimes. Company-local time is used only
for display, week boundaries and scheduling.
"""

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

DEFAULT_TZ = "America/Los_Angeles"


def utcnow() -> datetime:
    return datetime.now(UTC)


def as_utc(value: datetime) -> datetime:
    """Aware UTC datetime. Naive values are treated as UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def to_db(value: datetime) -> datetime:
    """Naive UTC datetime for storage."""
    return as_utc(value).replace(tzinfo=None)


def db_now() -> datetime:
    return to_db(utcnow())


def to_local(value: datetime, tz_name: str) -> datetime:
    return as_utc(value).astimezone(ZoneInfo(tz_name or DEFAULT_TZ))


def local_date(value: datetime, tz_name: str) -> date:
    return to_local(value, tz_name).date()


def local_datetime(year, month, day, hour=0, minute=0, second=0, tz_name: str = DEFAULT_TZ) -> datetime:
    return datetime(year, month, day, hour, minute, second, tzinfo=ZoneInfo(tz_name))


def monday_on_or_before(day: date) -> date:
    return day - timedelta(days=day.weekday())


def report_date_for(day: date) -> date:
    """The most recent Monday on or before `day`."""
    return monday_on_or_before(day)


def require_monday(report_date: date) -> None:
    if report_date.weekday() != 0:
        raise ValueError(f"Report date {report_date.isoformat()} is not a Monday.")


def week_start_n(report_date: date, n: int) -> date:
    """W0 is the report week (R minus 7 days); Wn is n weeks before it."""
    require_monday(report_date)
    return report_date - timedelta(days=7 * (n + 1))


def report_week_start(report_date: date) -> date:
    return week_start_n(report_date, 0)


def last_complete_week_starts(today: date, count: int) -> list[date]:
    """The `count` most recent complete weeks before `today`, oldest first."""
    newest = monday_on_or_before(today) - timedelta(days=7)
    return [newest - timedelta(days=7 * i) for i in range(count - 1, -1, -1)]


def fmt_date(day: date) -> str:
    """Month abbreviation, day without a leading zero, year: Jul 31, 2026."""
    return f"{day:%b} {day.day}, {day.year}"


def fmt_month_day(day: date) -> str:
    return f"{day:%b} {day.day}"


def week_label(week_start: date) -> str:
    """Oct 5 to Oct 11, 2026."""
    sunday = week_start + timedelta(days=6)
    if week_start.year != sunday.year:
        return f"{fmt_date(week_start)} to {fmt_date(sunday)}"
    return f"{fmt_month_day(week_start)} to {fmt_date(sunday)}"


def fmt_local_datetime(value: datetime, tz_name: str) -> str:
    """Oct 12, 2026 06:00 PDT."""
    local = to_local(value, tz_name)
    return f"{fmt_date(local.date())} {local:%H:%M} {local.tzname()}"
