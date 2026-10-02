"""Phase 4: the weekly report in DEMO mode."""

import re
from datetime import timedelta
from email import message_from_bytes
from pathlib import Path

from sqlalchemy import delete, func, select

from app.db import session_scope
from app.enums import ReportStatus, SyncStatus
from app.models import Company, Report, SyncRun
from app.report import build_view, generate_report, render_text, report_subject, send_reports
from app.weeks import to_db
from tests.fixtures import R, local
from tests.test_phase1 import SPEC_ADDBACKS, add_addbacks, replace_weeks

GENERATE_AT = local(2026, 10, 12, 6, 30)
SEND_AT = local(2026, 10, 12, 7, 0)


def generate(base, now=GENERATE_AT):
    with session_scope() as db:
        return generate_report(db, base.company_id, now)


def report_count(base) -> int:
    with session_scope() as db:
        return db.scalar(select(func.count(Report.id)).where(Report.company_id == base.company_id))


def replace_sync_runs(base, *runs):
    """runs: (status, started_local, finished_local or None)"""
    with session_scope() as db:
        db.execute(delete(SyncRun).where(SyncRun.company_id == base.company_id))
        for status, started, finished in runs:
            db.add(
                SyncRun(
                    company_id=base.company_id,
                    started_at=to_db(started),
                    finished_at=to_db(finished) if finished else None,
                    status=status,
                    error_text="QuickBooks returned HTTP 503." if status == SyncStatus.FAILED else None,
                )
            )
        db.commit()


def test_T4_1_generate_creates_one_generated_report(base):
    report = generate(base)
    assert report_count(base) == 1
    assert report.status == ReportStatus.GENERATED
    assert report.report_date == R
    assert report.week_start.isoformat() == "2026-10-05"
    assert report.metrics["revenue_week"] == 3200000
    assert report.metrics["runway_weeks"] == "5.7"
    assert report.issues == ["RUNWAY_LOW", "AR_SEVERE", "AR_AGING", "REVENUE_DROP", "MARGIN_DROP"]
    assert report.data_as_of == to_db(local(2026, 10, 12, 6, 0))


def test_T4_2_generate_again_same_day_does_nothing(base):
    first = generate(base)
    second = generate(base, GENERATE_AT + timedelta(hours=2))
    assert report_count(base) == 1
    assert second.id == first.id


def test_T4_3_subject(base):
    report = generate(base)
    assert report_subject("Test HVAC", report.metrics) == "Test HVAC weekly report: revenue $32,000 (-20.0% vs avg)"


def test_T4_4_html_body(base):
    html = generate(base).html
    assert "Test HVAC: week of Oct 5 to Oct 11, 2026" in html
    for line in (
        "Cash covers about 5.7 weeks of costs.",
        "Delgado LLC owes $4,800, 73 days past due.",
        "37.5% of what customers owe you is over 60 days past due.",
    ):
        assert line in html
    assert "-20.0% vs 4-week average of $40,000" in html
    assert "<script" not in html.lower()
    assert "<img" not in html.lower()
    assert "max-width:640px" in html
    assert "A rough estimate from your books, not an appraisal." in html
    assert "Data from QuickBooks as of Oct 12, 2026 06:00 PDT. Questions? Reply to this email." in html
    # Only the first 3 issues are shown.
    assert "Revenue was 20.0% below your 4-week average." not in html


def test_T4_4_plain_text_version_has_the_same_sections(base):
    report = generate(base)
    text = render_text(build_view("Test HVAC", report.metrics))
    assert "Test HVAC: week of Oct 5 to Oct 11, 2026" in text
    assert "Delgado LLC owes $4,800, 73 days past due." in text
    assert "-20.0% vs 4-week average of $40,000" in text
    assert "<" not in text


def test_T4_5_revenue_chart(base):
    report = generate(base)
    chart = {row["label"]: row for row in build_view("Test HVAC", report.metrics)["chart"]}
    assert list(chart) == ["Aug 17", "Aug 24", "Aug 31", "Sep 7", "Sep 14", "Sep 21", "Sep 28", "Oct 5"]
    for label in ("Aug 17", "Aug 24", "Aug 31"):
        assert chart[label]["amount"] == "n/a" and chart[label]["width"] is None
    assert chart["Sep 14"]["width"] == "100"
    assert chart["Oct 5"]["width"] == "72.7"
    assert "width:100%" in report.html
    assert "width:72.7%" in report.html


def test_T4_6_send_at_7_writes_outbox(base, capsys):
    report = generate(base)
    with session_scope() as db:
        assert send_reports(db, SEND_AT - timedelta(minutes=1)) == []
        send_reports(db, SEND_AT)
        report = db.get(Report, report.id)
        assert report.status == ReportStatus.SENT
        assert report.sent_at == to_db(SEND_AT)
    html_file = Path("outbox") / f"report-{base.company_id}-2026-10-12.html"
    eml_file = Path("outbox") / f"report-{base.company_id}-2026-10-12.eml"
    assert html_file.exists() and eml_file.exists()
    message = message_from_bytes(eml_file.read_bytes())
    assert message["Subject"] == "Test HVAC weekly report: revenue $32,000 (-20.0% vs avg)"
    assert "[DEMO EMAIL] to=(none) subject=Test HVAC weekly report" in capsys.readouterr().out


def test_T4_6_demo_print_masks_recipients(base, capsys):
    from app.models import CompanySettings

    with session_scope() as db:
        settings = db.scalar(select(CompanySettings).where(CompanySettings.company_id == base.company_id))
        settings.report_recipients = ["owner@test.local"]
        db.commit()
    generate(base)
    with session_scope() as db:
        send_reports(db, SEND_AT)
    out = capsys.readouterr().out
    assert "to=o***@test.local" in out
    assert "owner@test.local" not in out


def test_T4_7_stale_data_is_held_and_never_sent(base):
    replace_sync_runs(base, (SyncStatus.SUCCEEDED, local(2026, 10, 3, 5, 59), local(2026, 10, 3, 6, 0)))
    report = generate(base)
    assert report.status == ReportStatus.HELD
    assert report.error_text == "Held: data is 9 days old"
    with session_scope() as db:
        send_reports(db, SEND_AT)
        assert db.get(Report, report.id).status == ReportStatus.HELD
    assert not (Path("outbox") / f"report-{base.company_id}-2026-10-12.html").exists()


def test_T4_8_failed_sync_with_recent_data_gets_banner(base):
    replace_sync_runs(
        base,
        (SyncStatus.SUCCEEDED, local(2026, 10, 10, 1, 59), local(2026, 10, 10, 2, 0)),
        (SyncStatus.FAILED, local(2026, 10, 12, 6, 0), local(2026, 10, 12, 6, 1)),
    )
    report = generate(base)
    assert report.status == ReportStatus.GENERATED
    assert "This week's sync failed" in report.html
    assert "Data as of Oct 10, 2026 02:00 PDT. This week's sync failed, so numbers may be out of date." in report.html


def test_T4_8_no_banner_when_sync_succeeded(base):
    assert "sync failed" not in generate(base).html


def test_T4_9_report_disabled_means_no_report(base):
    with session_scope() as db:
        db.get(Company, base.company_id).report_enabled = False
        db.commit()
    assert generate(base) is None
    assert report_count(base) == 0


def test_T4_10_value_range_line(base):
    replace_weeks(base.company_id, [300000] * 48 + [900000] * 4)
    add_addbacks(base.company_id, SPEC_ADDBACKS)
    html = generate(base).html
    assert "Rough range: $825,000 to $1,155,000 (2.5x to 3.5x earnings)" in html
    assert "Earnings (SDE): $330,000" in html


def test_report_uses_inline_styles_only(base):
    html = generate(base).html
    assert "<style" not in html.lower()
    assert "<link" not in html.lower()
    assert not re.search(r'class="', html)
