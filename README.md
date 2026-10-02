# Revenue-Recovery: Owner KPI and Cash Report

Every Monday at 07:00, each client company's owner gets a one-page email built from their QuickBooks Online books: how last week went, who owes them money, how long their cash lasts, the 3 things to fix, and a rough value for the business. The app also drafts polite payment reminders for overdue invoices, and nothing goes to a customer until the owner approves it. One operator runs every client from one console.

The full build spec is [`SPEC.md`](SPEC.md). This README covers how to run the app and every place where the build had to interpret the spec.

## Quick start (Mac/Linux)

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
#   APP_SECRET_KEY:        python -c "import secrets; print(secrets.token_hex(32))"
#   TOKEN_ENCRYPTION_KEY:  python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
alembic upgrade head
python -m app.seed --demo
uvicorn app.main:app --port 8000
```

On Windows, create the environment with `py -3.12 -m venv .venv` and activate it with `.venv\Scripts\activate`.

Open http://localhost:8000 and log in as `owner@demo.local` or `operator@demo.local`. Both use the password `demo-password-123`.

Run the tests with `pytest -q`. They need no network and no `.env` file.

## The 5-minute pitch demo

The demo company is **Glendale Comfort Air (Demo)**. Its report always shows the same 3 issues: `RUNWAY_LOW`, then `Delgado Property Mgmt owes $4,800, 73 days past due.`, then `AR_AGING`. `REVENUE_DROP` also triggers but is not shown.

Step 4 of the pitch, where **Approve and send** shows `SENT`, follows Section 10 exactly:

* **Send window:** approved reminders only go out Monday to Friday, 09:00 up to 17:00, company-local (America/Los_Angeles). Outside that window, Approve and send shows `APPROVED` and the reminder waits.
* **Fresh data:** reminders need a sync from the last 24 hours. Seeding runs a sync, so seed on the day of the pitch:

```bash
rm -f kpi.db && alembic upgrade head && python -m app.seed --demo
```

If the sync is older than 24 hours, the reminder stays `APPROVED` with the note `Sync needed before sending`, and a demo sync starts. With the scheduler on, the reminder goes out on the next minute's check.

## Decisions made before the build

These four were confirmed with you before the build started.

1. **Layout:** the project lives at the repository root, not in an `owner-kpi/` subfolder.
2. **Two extra columns.** Two spec rules need data that the 13-table schema had no column for:
   * `users.must_change_password` lets temporary passwords be forced to change at first login (Section 12.4).
   * `reminders.balance_cents_at_draft` lets the sender spot a balance that changed since drafting (Section 10.4, test T5.9).

   The database still has exactly 13 tables.
3. **Section 10 sending rules apply in DEMO too.** The business-hours window and the 50-per-day cap are not bypassed for the pitch; see above.
4. **Intuit check:** this build environment could not reach developer.intuit.com. The URLs, query text and JSON field names in Section 6 match Intuit's published API as far as I know, and the recorded fixtures in `tests/qbo_fixtures/` follow Intuit's documented response shapes. **Two checks remain manual:** compare Section 6 against Intuit's current docs, and run one real sync against a sandbox company (`QBO_ENVIRONMENT=sandbox`).

## Interpretations

Each of these fills a gap in the spec or chooses one reading of an ambiguous line.

**Numbers**

* **Issue rules compare the rounded figures.** Rules compare the 1-decimal figures the owner reads, the way Section 15 works them (for example `37.5 ≤ 43.4 - 5.0`). That way a line never reads "8.0 weeks" while claiming to be under 8.0.
* **Metrics storage.** `reports.metrics` stores cents as integers and percentages, weeks and days as 1-decimal strings, so `runway_weeks` is `"5.7"`. No floats are stored anywhere.
* **Missing figures.** When a tile's figure is missing, its small line shows `No data`. If revenue exists but the change does not, the subject reads `(No data vs avg)`.
* **Year boundaries.** A week that crosses into a new year is labelled with both years: `Dec 28, 2026 to Jan 3, 2027`.

**Reports**

* **No sync yet means no report.** A company that has never synced successfully gets no report, since there is nothing to report. A company with stale data still gets a `HELD` report.
* **"This morning's sync failed"** means the company's newest sync run started on the report date and failed.
* **Held reports.** A `HELD` report's console text, `Held: data is <n> days old`, is stored in `reports.error_text`.

**Reminders**

* **Reminder notes** (`Balance changed`, `Sync needed before sending`, `Invoice no longer open`, `Invoice marked disputed`, `Customer unsubscribed`) are stored in `reminders.error_text`.
* **Disputed invoices.** Marking an invoice disputed, from either the Invoices or the Reminders page, cancels its open reminders. A disputed invoice never gets reminders.
* **The unsubscribe line stays.** An edited draft must keep its unsubscribe line, so customers can always stop reminder emails.
* **The 7-day gap** between reminders counts calendar days in the company's time zone, as test T5.3 needs.
* **Turning reminders off** (`reminders_enabled` false) stops both drafting and sending.

**Data sources**

* **Demo data is anchored to the sync day.** `DemoDataSource` places every date relative to the day it syncs (the seed day for the seed), so the demo looks the same whenever it is seeded.
* **First sync** means the company has no previous `SUCCEEDED` sync, so it pulls 60 weeks.
* **Missing due date.** A QuickBooks invoice without a `DueDate` uses its `TxnDate`.
* **`.local` addresses count as valid.** The spec's demo and test addresses (`owner@demo.local`) use that domain, which `email-validator` rejects by default.

**Scheduler and screens**

* **Scheduler timing.** The scheduler runs every minute:
  * Syncs run on the exact minute: nightly at 02:00, plus Monday at 06:00, for connected QBO companies.
  * On Monday from 06:30, a company without today's report first gets its reminder drafts and then its report, so the report can count the drafts.
  * Reports send once local time reaches `report_send_time`.
  * Approved reminders are checked every minute.
* **Small routes the spec's screens need:**
  * `POST /logout` and an Account link in the top bar.
  * `POST /switch-company` for the operator's company switcher.
  * `GET /companies/new` and `POST /companies` for **New company**.
  * Action routes for each company-page tab, and `GET /companies/{id}/preview` for **Preview**.
  * The top bar's **Reports** link goes to the report list on Home.
* **Settings saves are all or nothing.** If any field fails validation, nothing is saved. A refused LIVE switch lists every failing precondition by number.

## Tests

`pytest -q` runs 139 tests, with 0 failures. Every numbered acceptance test in Section 15 has at least one test named after it (for example `test_T3_6_...`):

| Phase | File | Covers |
|---|---|---|
| 0 | `tests/test_phase0.py` | T0.1–T0.8 |
| 1 | `tests/test_phase1.py` | T1.1–T1.12 |
| 2 | `tests/test_phase2.py` | T2.1–T2.8 |
| 3 | `tests/test_phase3.py` | T3.1–T3.14 (Intuit is mocked with `httpx.MockTransport` and `tests/qbo_fixtures/`) |
| 4 | `tests/test_phase4.py` | T4.1–T4.10 |
| 5 | `tests/test_phase5.py` | T5.1–T5.13 |
| 6 | `tests/test_phase6.py` | T6.1–T6.8 (Postmark mocked) |
| 7 | `tests/test_phase7.py` | T7.1–T7.4, the demo seeded on 8 different days, and the pitch pages |
| — | `tests/test_app.py` | Scheduler steps, the new-company temporary password flow, session timeout and cookie flags, tenant switching |

Tests block real network calls, so they can never reach QuickBooks or Postmark.

**Still manual before v1 is done (Section 15):**

* The offline 7-step pitch walkthrough.
* The one-time sandbox shape check.
* The Intuit documentation check.
