"""Phase 5: reminders. Drafting runs at Monday 2026-10-12 06:30 local unless stated."""

from datetime import date, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app import reminders as reminders_module
from app.db import session_scope
from app.enums import ReminderStatus, SyncStatus
from app.models import Invoice, Reminder, Suppression, SyncRun, User
from app.reminders import (
    BALANCE_CHANGED,
    SYNC_NEEDED,
    ReminderError,
    approve,
    draft_reminders,
    send_reminders,
    set_disputed,
    update_draft,
)
from app.sources.base import BankBalanceData, DataSource, InvoiceData, WeekData
from app.sync import run_sync
from app.weeks import to_db
from tests.fixtures import local
from tests.test_phase4 import replace_sync_runs

DRAFT_AT = local(2026, 10, 12, 6, 30)


def draft(base, now=DRAFT_AT) -> list[Reminder]:
    with session_scope() as db:
        return draft_reminders(db, base.company_id, now)


def by_doc(base) -> dict[str, list[Reminder]]:
    with session_scope() as db:
        rows = db.execute(
            select(Invoice.doc_number, Reminder).join(Invoice, Invoice.id == Reminder.invoice_id).order_by(Reminder.id)
        ).all()
    result: dict[str, list[Reminder]] = {}
    for doc, reminder in rows:
        result.setdefault(doc, []).append(reminder)
    return result


def add_sent(base, doc: str, sent_local, count: int = 1) -> None:
    with session_scope() as db:
        invoice = db.get(Invoice, base.invoice_ids[doc])
        for i in range(count):
            db.add(
                Reminder(
                    company_id=base.company_id,
                    invoice_id=invoice.id,
                    sequence_number=i + 1,
                    template_level=1,
                    status=ReminderStatus.SENT,
                    to_email=invoice.customer_email,
                    subject="s",
                    body="b",
                    token=f"{doc}-{i}".ljust(32, "x"),
                    drafted_at=to_db(sent_local - timedelta(hours=2)),
                    sent_at=to_db(sent_local - timedelta(weeks=count - 1 - i)),
                    balance_cents_at_draft=invoice.balance_cents,
                )
            )
        db.commit()


def reminder(reminder_id: int) -> Reminder:
    with session_scope() as db:
        return db.get(Reminder, reminder_id)


def approve_doc(base, doc: str, now) -> int:
    with session_scope() as db:
        target = next(r for r in by_doc(base)[doc] if r.status == ReminderStatus.DRAFT)
        approve(db, db.get(Reminder, target.id), db.get(User, base.owner_id), now)
        return target.id


def send(now, company_id=None):
    with session_scope() as db:
        return send_reminders(db, now, company_id)


def test_T5_1_base_fixture_drafts(base):
    drafts = draft(base)
    assert len(drafts) == 3
    found = {doc: (rs[0].template_level, rs[0].sequence_number) for doc, rs in by_doc(base).items()}
    assert found == {"INV-102": (1, 1), "INV-103": (2, 1), "INV-104": (3, 1)}
    assert "12 days" not in reminder(by_doc(base)["INV-102"][0].id).body  # level 1 does not state days
    assert reminder(by_doc(base)["INV-103"][0].id).subject == "Invoice INV-103 is 45 days past due"
    assert reminder(by_doc(base)["INV-104"][0].id).subject == "Please call us about invoice INV-104"
    assert all(r.status == ReminderStatus.DRAFT and len(r.token) == 32 for r in drafts)


def test_T5_2_disputed_invoice_gets_no_draft(base):
    with session_scope() as db:
        set_disputed(db, db.get(Invoice, base.invoice_ids["INV-103"]), True)
    assert len(draft(base)) == 2
    assert "INV-103" not in by_doc(base)


def test_T5_3_last_sent_must_be_7_days_ago(base):
    add_sent(base, "INV-102", local(2026, 10, 8, 10, 0))
    draft(base)
    assert [r.status for r in by_doc(base)["INV-102"]] == [ReminderStatus.SENT]


def test_T5_3_seven_days_later_drafts_sequence_2(base):
    add_sent(base, "INV-102", local(2026, 10, 5, 10, 0))
    draft(base)
    new = [r for r in by_doc(base)["INV-102"] if r.status == ReminderStatus.DRAFT]
    assert len(new) == 1
    assert new[0].sequence_number == 2


def test_T5_4_at_most_4_sent_reminders(base):
    add_sent(base, "INV-104", local(2026, 9, 21, 10, 0), count=4)
    draft(base)
    assert [r.status for r in by_doc(base)["INV-104"]] == [ReminderStatus.SENT] * 4


def test_T5_5_level_3_body(base):
    draft(base)
    body = reminder(by_doc(base)["INV-104"][0].id).body
    assert body.startswith("Hi Delgado LLC, invoice INV-104 for $4,800.00 is 73 days past due.")
    assert "Please reply or call us at +18185550100 so we can settle it together." in body
    assert body.endswith(f"To stop payment reminders by email: http://localhost:8000/r/{by_doc(base)['INV-104'][0].token}")
    assert "\n\nThank you,\nTest HVAC\n\n--\n" in body


def test_T5_5_level_1_body_with_payment_instructions(base):
    from app.models import CompanySettings

    with session_scope() as db:
        db.scalar(select(CompanySettings).where(CompanySettings.company_id == base.company_id)).payment_instructions = (
            "Pay online at example.com/pay or call us."
        )
        db.commit()
    draft(base)
    body = reminder(by_doc(base)["INV-102"][0].id).body
    assert body.startswith(
        "Hi Baker Home, a quick reminder that invoice INV-102 for $3,000.00 was due on Sep 30, 2026. "
        "If you've already paid, thank you, and please ignore this note.\n\n"
        "Pay online at example.com/pay or call us.\n\nThank you,\nTest HVAC\n\n--\n"
    )


def test_T5_6_banned_words_rejected_whole_words_only(base):
    draft(base)
    target = by_doc(base)["INV-104"][0]
    with session_scope() as db:
        r = db.get(Reminder, target.id)
        with pytest.raises(ReminderError) as error:
            update_draft(db, r, r.subject, r.body.replace("Hi Delgado LLC,", "Hi Delgado LLC, We will take legal action."))
        assert str(error.value) == 'Remove "legal action". Reminders must stay polite and must not threaten.'
        db.rollback()
        r = db.get(Reminder, target.id)
        update_draft(db, r, r.subject, r.body.replace("Hi Delgado LLC,", "Hi Delgado LLC, about the billing issue,"))
    assert "billing issue" in reminder(target.id).body


@pytest.mark.parametrize(
    "text, word",
    [
        ("We may SUE you", "sue"),
        ("No Late  Fee yet", "late fee"),
        ("this is your final notice", "final notice"),
        ("sent to collections", "collections"),
    ],
)
def test_T5_6_banned_word_variants(text, word):
    assert reminders_module.banned_word_error(text) == f'Remove "{word}". Reminders must stay polite and must not threaten.'


@pytest.mark.parametrize("text", ["the billing issue", "pursue payment", "Suez canal", "collection of tools"])
def test_T5_6_allowed_words(text):
    assert reminders_module.banned_word_error(text) is None


def test_T5_7_send_window_starts_at_9(base):
    draft(base)
    reminder_id = approve_doc(base, "INV-104", local(2026, 10, 12, 8, 0))
    send(local(2026, 10, 12, 8, 30))
    assert reminder(reminder_id).status == ReminderStatus.APPROVED
    send(local(2026, 10, 12, 9, 0))
    sent = reminder(reminder_id)
    assert sent.status == ReminderStatus.SENT
    assert sent.sent_at == to_db(local(2026, 10, 12, 9, 0))
    assert Path(f"outbox/reminder-{base.company_id}-{reminder_id}.eml").exists()


def test_T5_8_stale_sync_blocks_sending_and_starts_a_sync(base, monkeypatch):
    started = []
    monkeypatch.setattr(reminders_module, "start_sync", lambda db, company_id, now: started.append(company_id))
    draft(base)
    reminder_id = approve_doc(base, "INV-104", local(2026, 10, 12, 8, 0))
    replace_sync_runs(base, (SyncStatus.SUCCEEDED, local(2026, 10, 11, 7, 59), local(2026, 10, 11, 8, 0)))
    send(local(2026, 10, 12, 9, 0))
    waiting = reminder(reminder_id)
    assert waiting.status == ReminderStatus.APPROVED
    assert waiting.error_text == SYNC_NEEDED == "Sync needed before sending"
    assert started == [base.company_id]


def test_T5_9_balance_change_cancels(base):
    draft(base)
    reminder_id = approve_doc(base, "INV-104", local(2026, 10, 12, 8, 0))
    with session_scope() as db:
        db.get(Invoice, base.invoice_ids["INV-104"]).balance_cents = 300000
        db.commit()
    send(local(2026, 10, 12, 9, 0))
    cancelled = reminder(reminder_id)
    assert cancelled.status == ReminderStatus.CANCELLED
    assert cancelled.error_text == BALANCE_CHANGED == "Balance changed"
    # The next drafting run creates a new draft at the new balance.
    draft(base, local(2026, 10, 12, 9, 5))
    fresh = [r for r in by_doc(base)["INV-104"] if r.status == ReminderStatus.DRAFT]
    assert len(fresh) == 1 and "$3,000.00" in fresh[0].body


class PaidInvoiceSource(DataSource):
    """Returns the base invoices except INV-102, which was paid."""

    def __init__(self, base):
        self.base = base

    def fetch_week(self, week_start: date) -> WeekData:
        return WeekData(4000000, 2200000, 1000000, 0, 0, 800000)

    def fetch_open_invoices(self) -> list[InvoiceData]:
        from tests.fixtures import INVOICES

        return [
            InvoiceData(doc[4:], doc, customer, email, due - timedelta(days=30), due, balance, balance)
            for doc, customer, email, due, balance in INVOICES
            if doc != "INV-102"
        ]

    def fetch_bank_balance(self) -> BankBalanceData:
        return BankBalanceData(18000000, 1)


def test_T5_10_sync_finding_invoice_paid_cancels_draft(base):
    draft(base)
    with session_scope() as db:
        run = run_sync(db, base.company_id, local(2026, 10, 12, 8, 0), source=PaidInvoiceSource(base))
    assert run.status == SyncStatus.SUCCEEDED
    docs = by_doc(base)
    assert [r.status for r in docs["INV-102"]] == [ReminderStatus.CANCELLED]
    assert [r.status for r in docs["INV-104"]] == [ReminderStatus.DRAFT]


def test_T5_11_weekend_approval_waits_until_monday_9(base):
    draft(base)
    reminder_id = approve_doc(base, "INV-104", local(2026, 10, 17, 10, 0))
    for when in (local(2026, 10, 17, 10, 0), local(2026, 10, 18, 12, 0), local(2026, 10, 19, 8, 59)):
        send(when)
        assert reminder(reminder_id).status == ReminderStatus.APPROVED
    replace_sync_runs(base, (SyncStatus.SUCCEEDED, local(2026, 10, 19, 5, 59), local(2026, 10, 19, 6, 0)))
    send(local(2026, 10, 19, 9, 0))
    sent = reminder(reminder_id)
    assert sent.status == ReminderStatus.SENT
    assert sent.sent_at == to_db(local(2026, 10, 19, 9, 0))


def test_T5_11_window_ends_at_17(base):
    draft(base)
    reminder_id = approve_doc(base, "INV-104", local(2026, 10, 12, 16, 0))
    send(local(2026, 10, 12, 17, 0))
    assert reminder(reminder_id).status == ReminderStatus.APPROVED


def test_T5_12_unsubscribe_suppresses_and_cancels(client, base):
    draft(base)
    target = by_doc(base)["INV-104"][0]
    page = client.get(f"/r/{target.token}")
    assert page.status_code == 200 and "Confirm" in page.text
    response = client.post(f"/r/{target.token}")
    assert response.status_code == 200
    assert "You won't get more payment reminders by email from Test HVAC." in response.text
    with session_scope() as db:
        suppression = db.scalar(select(Suppression).where(Suppression.company_id == base.company_id))
        assert (suppression.email, suppression.reason) == ("ap@delgado.example.com", "UNSUBSCRIBED")
    assert reminder(target.id).status == ReminderStatus.CANCELLED
    draft(base, local(2026, 10, 19, 6, 30))
    assert [r.status for r in by_doc(base)["INV-104"]] == [ReminderStatus.CANCELLED]
    assert client.get("/r/not-a-real-token").status_code == 404


def _add_approved(base, count: int, approved_local) -> list[int]:
    ids = []
    with session_scope() as db:
        for i in range(count):
            invoice = Invoice(
                company_id=base.company_id,
                source_id=f"bulk-{i}",
                doc_number=f"B-{i}",
                customer_name=f"Bulk Customer {i}",
                customer_email=f"bulk{i}@example.com",
                txn_date=date(2026, 8, 1),
                due_date=date(2026, 9, 1),
                total_cents=10000,
                balance_cents=10000,
                is_open=True,
                last_synced_at=to_db(approved_local),
            )
            db.add(invoice)
            db.flush()
            reminder_row = Reminder(
                company_id=base.company_id,
                invoice_id=invoice.id,
                sequence_number=1,
                template_level=2,
                status=ReminderStatus.APPROVED,
                to_email=invoice.customer_email,
                subject=f"Invoice B-{i}",
                body="Hi",
                token=f"bulk-token-{i:04d}".ljust(32, "x"),
                drafted_at=to_db(approved_local),
                approved_at=to_db(approved_local + timedelta(seconds=i)),
                approved_by_user_id=base.owner_id,
                balance_cents_at_draft=10000,
            )
            db.add(reminder_row)
            db.flush()
            ids.append(reminder_row.id)
        db.commit()
    return ids


def test_T5_13_at_most_50_per_company_per_day(base):
    ids = _add_approved(base, 51, local(2026, 10, 12, 8, 0))
    send(local(2026, 10, 12, 9, 0))
    with session_scope() as db:
        sent = db.scalar(select(func.count(Reminder.id)).where(Reminder.status == ReminderStatus.SENT))
    assert sent == 50
    assert reminder(ids[-1]).status == ReminderStatus.APPROVED
    send(local(2026, 10, 12, 15, 0))
    assert reminder(ids[-1]).status == ReminderStatus.APPROVED
    with session_scope() as db:  # Tuesday's nightly sync keeps the data fresh
        db.add(
            SyncRun(
                company_id=base.company_id,
                started_at=to_db(local(2026, 10, 13, 1, 59)),
                finished_at=to_db(local(2026, 10, 13, 2, 0)),
                status=SyncStatus.SUCCEEDED,
            )
        )
        db.commit()
    send(local(2026, 10, 13, 9, 0))
    last = reminder(ids[-1])
    assert last.status == ReminderStatus.SENT
    assert last.sent_at == to_db(local(2026, 10, 13, 9, 0))


def test_mark_disputed_cancels_open_reminders(base):
    draft(base)
    with session_scope() as db:
        set_disputed(db, db.get(Invoice, base.invoice_ids["INV-104"]), True)
    assert [r.status for r in by_doc(base)["INV-104"]] == [ReminderStatus.CANCELLED]


def test_editing_cannot_remove_unsubscribe_line(base):
    draft(base)
    target = by_doc(base)["INV-104"][0]
    with session_scope() as db:
        r = db.get(Reminder, target.id)
        with pytest.raises(ReminderError):
            update_draft(db, r, r.subject, r.body.split("\n\n--")[0])
