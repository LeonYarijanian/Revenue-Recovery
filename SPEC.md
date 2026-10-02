# Owner KPI and Cash Report: Claude Code Build Spec

Oct 2, 2026 · @Leon Yarijanian

## 1. How to use this spec

Build this app one phase at a time with Claude Code, and do not start a phase until every acceptance test in the previous phase passes. Save this document as `SPEC.md` in an empty project folder, open Claude Code in that folder, and send:

> Read SPEC.md fully. Build Phase 0 only. Stop when its tests pass.

For each later phase, send the same message with the next phase number. Phases and tests are in Section 15.

### Hard rules for the coding agent

1. Build exactly what this spec says. Do not add features, pages, fields, settings or libraries that are not written here.
2. Use the exact names in this spec for tables, fields, enums, routes, files and settings. Do not rename anything.
3. Use only the libraries listed in Section 4. If another library seems required, stop and ask first.
4. Write one automated test for every acceptance test in the current phase. A phase is done only when `pytest -q` reports 0 failures.
5. Never call the real QuickBooks API from tests, and never send a real email unless every LIVE precondition in Section 9 is true. Tests use recorded JSON files and the DEMO providers.
6. Never commit secrets. Keys live in `.env`, and `.env` is listed in `.gitignore`. QuickBooks tokens are encrypted in the database (Section 12).
7. Store every timestamp in UTC. Convert to the company's time zone (default `America/Los_Angeles`) only for display, week boundaries and scheduling.
8. Store money as integer cents. Never use floats for money.
9. Round displayed decimals with `decimal.ROUND_HALF_UP`. Never use Python's built-in `round()`.
10. Every number in the report comes from a formula in Section 7 or a rule in Section 8. Never generate report text or numbers with AI.
11. If a QuickBooks response does not match the field names in Section 6, stop and ask. Do not guess field names.
12. After each phase, stop and print three things: the files created or changed, the test count and result, and the exact command to run the app.
13. If two parts of this spec conflict, or something is ambiguous, stop and ask. Do not guess.

## 2. Product summary

Every Monday at 07:00, the owner of each client company gets a one-page email built from their QuickBooks data. It shows how last week went, who owes them money, how long their cash lasts, the 3 things to fix, and a rough value for the business. The app also drafts polite reminders for overdue invoices. Nothing goes out to customers until the owner approves it.

| Part | What it does | Who uses it | Why the owner pays |
|---|---|---|---|
| Weekly report | One email, every Monday at 07:00 local, with 4 headline numbers, the top 3 issues, overdue invoices, an 8-week revenue chart and a value estimate | Owner | Replaces gut feel and a bookkeeper's late monthly PDF |
| Reminder drafts | Polite payment reminders for overdue invoices, which the owner approves one by one or all at once | Owner | Brings cash in faster |
| Operator console | Every client company's connection status, last sync, last report and open issues | You, the operator | Lets 1 person run 20 or more clients |

**Target customer for v1:** an owner of a service business with $1M to $10M in revenue that keeps its books in QuickBooks Online. The first clients are the same trades companies you would one day want to buy.

**Not in v1:** QuickBooks Desktop, Xero or other accounting systems; PDF attachments; AI-written commentary; forecasts beyond the cash runway; budgets; payroll; tax or accounting advice; texting; automatic sending of reminders without approval; and fixing the client's books. The app reads the books. It never writes to QuickBooks.

## 3. Definitions

Every term below means exactly this everywhere in the spec and the code.

| Term | Meaning |
|---|---|
| Company | One client business (one tenant). Every table except `companies`, `users` and `login_attempts` carries `company_id`. |
| Operator | You. Role `OPERATOR`; can open every company. |
| Owner | The client's owner or manager. Role `OWNER`; belongs to exactly 1 company. |
| QuickBooks Online (QBO) | Intuit's cloud accounting software, the only data source in v1 besides the demo source. |
| Realm ID | QBO's ID for one company's books. Stored per connection. |
| Connection | The OAuth 2.0 link to one company's QBO. An access token lasts about 1 hour. A refresh token gets new access tokens, may change on every refresh, and expires if unused for a long time. Always store the newest one. |
| Sandbox / production | Intuit's free test companies with fake data, versus real client books. Chosen by `QBO_ENVIRONMENT`. |
| Sync | Pulling weekly profit-and-loss numbers, open invoices and bank balances from QBO into the app's tables. |
| Week | Monday 00:00:00 to Sunday 23:59:59.999, company-local. `week_start` is that Monday's date. |
| Report date | The Monday the report is sent, company-local. |
| Report week | The week that ended the day before the report date. |
| Revenue | The total of the Income section of QBO's profit-and-loss report for a period, accrual basis by default |
| COGS | Cost of goods sold: the COGS section total, such as materials, equipment and subcontractors |
| Gross profit | Revenue - COGS |
| Gross margin | Gross profit / revenue × 100 |
| Operating expenses | The Expenses section total |
| Other expenses | The Other Expenses section total |
| Total costs | COGS + operating expenses + other expenses |
| Net income | The Net Income line of the profit-and-loss report |
| Accounts receivable (AR) | Money customers owe: the sum of unpaid invoice balances |
| Days past due | Report date - invoice due date, in calendar days. 0 or less means not yet due. |
| Aging bucket | `CURRENT` (0 or less), `D1_30`, `D31_60`, `D61_90`, `D91_PLUS` |
| DSO (days sales outstanding) | How many days of sales are still unpaid. Formula in Section 7. |
| Cash on hand | The sum of current balances of all QBO accounts of type Bank |
| Weekly burn | Average weekly total costs over recent weeks. Formula in Section 7. |
| Runway | Cash on hand / weekly burn, in weeks |
| TTM | Trailing twelve months: the last 52 complete weeks |
| SDE (seller's discretionary earnings) | TTM net income plus the add-backs the owner enters. The earnings number small-business buyers price on. |
| Add-back | A yearly amount added back to net income: owner's pay, interest, depreciation and amortization, one-time costs, or other |
| Value estimate | SDE × a low and a high multiple. A rough estimate, never an appraisal. |
| Issue rule | One of 7 fixed checks in Section 8 that can produce an issue line |
| Reminder | A payment-reminder email for one overdue invoice, drafted by the app and sent only after the owner approves it |
| Disputed | A flag the owner sets on an invoice the customer disagrees with. Disputed invoices never get reminders. |
| DEMO mode | Reports and reminders are written to `./outbox` and shown in the app. Nothing leaves the computer. The default. |
| LIVE mode | Emails go out through Postmark. Allowed only when the Section 9 preconditions are true. |

## 4. Tech stack and folder structure

The app is Python 3.12 with FastAPI, server-rendered HTML and a single SQLite file, the same stack as the other two specs. The money math reuses the deterministic style of Deal Scout. Pin the exact current version of every library in `requirements.txt` during Phase 0.

| Layer | Use exactly | Notes |
|---|---|---|
| Language | Python 3.12 | |
| Web framework | `fastapi` + `uvicorn[standard]` | 1 worker only |
| Forms | `python-multipart` | |
| HTML | `jinja2` | Pages and the report email share 1 template |
| Interactivity | HTMX, vendored to `app/static/htmx.min.js` | |
| CSS | Pico.css, vendored, plus `app/static/app.css` | The email uses inline styles only |
| Database | SQLite through `sqlalchemy` 2.x | File: `./kpi.db` |
| Migrations | `alembic` | |
| Settings | `pydantic-settings` | |
| Sessions | Starlette `SessionMiddleware` with `itsdangerous` | |
| Passwords | `argon2-cffi` | |
| Token encryption | `cryptography` (Fernet) | Encrypts QBO tokens at rest |
| HTTP client | `httpx` | QBO API, Intuit OAuth and Postmark. No Intuit SDK. |
| Scheduler | `apscheduler` | Jobs in Section 9 |
| Email validation | `email-validator` | |
| Time zones | `zoneinfo` | |
| Tests | `pytest`, `freezegun` | QBO responses come from JSON files in `tests/qbo_fixtures/` |

This is the full approved list. Anything else needs approval first.

### Folder structure

```
owner-kpi/
  app/
    main.py
    config.py              # Section 13
    db.py
    models.py              # Section 5
    enums.py               # Section 5
    auth.py
    money.py
    weeks.py               # week boundaries and report dates
    crypto.py              # Fernet encrypt/decrypt for tokens
    sources/
      base.py              # DataSource interface (Section 6)
      demo.py              # DemoDataSource
      qbo_oauth.py         # connect, callback, refresh, revoke
      qbo.py               # QboDataSource: P&L, invoices, bank balances
    sync.py                # writes source data into tables
    kpis.py                # every Section 7 formula
    issues.py              # Section 8 rules
    report.py              # builds the report (Section 9)
    reminders.py           # Section 10
    mailer.py              # DemoMailer and PostmarkMailer
    scheduler.py
    seed.py                # Section 14
    routes/
    templates/
      report_email.html    # the report, inline styles only
    static/
  migrations/
  outbox/                  # DEMO emails (.html and .eml), in .gitignore
  tests/
    fixtures.py
    qbo_fixtures/          # recorded QBO JSON responses
  .env.example
  .gitignore
  requirements.txt
  README.md
  SPEC.md
```

### Commands

| Purpose | Command |
|---|---|
| Virtual environment (Mac/Linux) | `python3.12 -m venv .venv && source .venv/bin/activate` |
| Virtual environment (Windows) | `py -3.12 -m venv .venv` then `.venv\Scripts\activate` |
| Install | `pip install -r requirements.txt` |
| Database | `alembic upgrade head` |
| Demo data | `python -m app.seed --demo` |
| Run | `uvicorn app.main:app --port 8000` |
| Tests | `pytest -q` |

## 5. Data model

The database has exactly 13 tables. Every table has `id` (integer primary key), `created_at` and `updated_at` (UTC). Every table except `companies`, `users` and `login_attempts` has `company_id` (required, indexed). In `users`, `company_id` is required for `OWNER` and null for `OPERATOR`. Money columns are signed integers in cents, because refunds can make a week negative. Store enums as strings.

### Enums (`app/enums.py`)

| Enum | Values, in this order |
|---|---|
| `Role` | `OPERATOR`, `OWNER` |
| `SourceType` | `DEMO`, `QBO` |
| `QboEnvironment` | `SANDBOX`, `PRODUCTION` |
| `ConnectionStatus` | `NOT_CONNECTED`, `CONNECTED`, `NEEDS_RECONNECT`, `DISCONNECTED` |
| `SyncStatus` | `RUNNING`, `SUCCEEDED`, `FAILED` |
| `AgingBucket` | `CURRENT`, `D1_30`, `D31_60`, `D61_90`, `D91_PLUS` |
| `AddbackCategory` | `OWNER_PAY`, `INTEREST`, `DEPRECIATION_AMORTIZATION`, `ONE_TIME`, `OTHER` |
| `ReportStatus` | `GENERATED`, `SENT`, `HELD`, `FAILED` |
| `ReminderStatus` | `DRAFT`, `APPROVED`, `SENT`, `CANCELLED`, `FAILED` |
| `IssueCode` | `RUNWAY_LOW`, `AR_SEVERE`, `AR_AGING`, `REVENUE_DROP`, `MARGIN_LOW`, `DSO_HIGH`, `MARGIN_DROP` |
| `SendMode` | `DEMO`, `LIVE` |
| `AccountingMethod` | `ACCRUAL`, `CASH` |
| `SuppressionReason` | `UNSUBSCRIBED`, `HARD_BOUNCE`, `MANUAL` |

### Tables

| Table | Field | Type | Rules |
|---|---|---|---|
| `companies` | `name` | string(120) | Shown in reports and reminders |
| `companies` | `timezone` | string(64) | Default `America/Los_Angeles` |
| `companies` | `industry` | string(60) | Free text, for example `HVAC` |
| `companies` | `source_type` | `SourceType` | `DEMO` or `QBO` |
| `companies` | `report_enabled` | boolean | Default true |
| `company_settings` | (Section 13) | | Exactly 1 row per company |
| `users` | `email`, `password_hash`, `full_name`, `role`, `is_active` | | Email lowercase and unique across the app; argon2id hash |
| `login_attempts` | `email`, `attempted_at`, `succeeded` | | Lockout rule in Section 11 |
| `qbo_connections` | `realm_id` | string(32) | 1 row per company (`company_id` unique) |
| `qbo_connections` | `environment` | `QboEnvironment` | |
| `qbo_connections` | `status` | `ConnectionStatus` | |
| `qbo_connections` | `access_token_enc`, `refresh_token_enc` | text | Fernet-encrypted; never logged or shown |
| `qbo_connections` | `access_expires_at`, `connected_at`, `last_refreshed_at` | datetime UTC | |
| `sync_runs` | `started_at`, `finished_at` | datetime UTC | |
| `sync_runs` | `status`, `error_text` | `SyncStatus`, string(500) | |
| `sync_runs` | `weeks_synced`, `invoices_synced` | integer | |
| `weekly_financials` | `week_start` | date | A Monday. Unique per (company, `week_start`). |
| `weekly_financials` | `revenue_cents`, `cogs_cents`, `opex_cents`, `other_income_cents`, `other_expense_cents`, `net_income_cents` | integer | Signed |
| `weekly_financials` | `synced_at` | datetime UTC | |
| `invoices` | `source_id` | string(32) | QBO invoice `Id`. Unique per company. |
| `invoices` | `doc_number` | string(40) | Invoice number shown to people |
| `invoices` | `customer_name`, `customer_email` | string(120), string(254) | Email may be empty |
| `invoices` | `txn_date`, `due_date` | date | |
| `invoices` | `total_cents`, `balance_cents` | integer | |
| `invoices` | `is_open` | boolean | True while `balance_cents` > 0 |
| `invoices` | `is_disputed` | boolean | Default false. Set by the owner; never overwritten by sync. |
| `invoices` | `last_synced_at` | datetime UTC | |
| `bank_balances` | `as_of`, `total_cents`, `account_count` | datetime UTC, integer, integer | 1 row per successful sync |
| `addbacks` | `category`, `label` | `AddbackCategory`, string(80) | |
| `addbacks` | `annual_amount_cents` | integer | 0 or more |
| `addbacks` | `note` | string(200) | Optional |
| `reports` | `report_date`, `week_start` | date | Unique per (company, `report_date`) |
| `reports` | `status` | `ReportStatus` | |
| `reports` | `html` | text | The exact email body |
| `reports` | `metrics`, `issues` | JSON | Every computed number and the issue list, for tests and audit |
| `reports` | `data_as_of` | datetime UTC | Finish time of the last successful sync |
| `reports` | `generated_at`, `sent_at`, `recipients`, `error_text` | | |
| `reminders` | `invoice_id` | FK | |
| `reminders` | `sequence_number` | integer | 1 to 4 for that invoice |
| `reminders` | `template_level` | integer | 1, 2 or 3 (Section 10) |
| `reminders` | `status` | `ReminderStatus` | |
| `reminders` | `to_email`, `subject`, `body` | | Editable while `DRAFT` |
| `reminders` | `token` | string(32) | Random; used in the unsubscribe link |
| `reminders` | `drafted_at`, `approved_at`, `sent_at` | datetime UTC | |
| `reminders` | `approved_by_user_id`, `provider_message_id`, `error_text` | | |
| `suppressions` | `email`, `reason` | string(254), `SuppressionReason` | Unique per (company, `email`) |

## 6. Data sources

The app reads from exactly 2 sources behind 1 interface: a demo source with fixed fake data, and QuickBooks Online. Every report number traces back to rows the sync wrote. The app never writes to QuickBooks.

### The interface (`app/sources/base.py`)

| Method | Returns |
|---|---|
| `fetch_week(week_start)` | Revenue, COGS, operating expenses, other income, other expenses and net income for that Monday-to-Sunday week, in cents |
| `fetch_open_invoices()` | Every invoice with a balance above 0: source ID, number, customer name, customer email, transaction date, due date, total and balance in cents |
| `fetch_bank_balance()` | Total current balance of all Bank-type accounts in cents, and the number of accounts |

`DemoDataSource` returns the fixed data in Section 14 with no network calls. `QboDataSource` calls QuickBooks as below.

### QuickBooks connection (OAuth 2.0)

| Item | Value |
|---|---|
| Authorization URL | `https://appcenter.intuit.com/connect/oauth2` |
| Token URL | `https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer` |
| Revoke URL | `https://developer.api.intuit.com/v2/oauth2/tokens/revoke` |
| Scope | `com.intuit.quickbooks.accounting` (read only in practice; the app never writes) |
| API base, sandbox | `https://sandbox-quickbooks.api.intuit.com` |
| API base, production | `https://quickbooks.api.intuit.com` |

Phase 3 must check every URL and field name in this section against Intuit's current developer documentation. If any differ, stop and ask.

1. **Connect:** the **Connect QuickBooks** button stores a random `state` and the company ID in the session, then redirects to the authorization URL with `client_id`, `response_type=code`, `scope`, `redirect_uri` = `QBO_REDIRECT_URI` and `state`.
2. **Callback:** `GET /qbo/callback?code=...&realmId=...&state=...`. A `state` that doesn't match the session returns 400. Otherwise exchange the code at the token URL (basic auth with client ID and secret; form fields `grant_type=authorization_code`, `code`, `redirect_uri`). Save `access_token` and `refresh_token` encrypted, `realmId`, and `access_expires_at` = now + `expires_in` seconds. Set status `CONNECTED`, then run a sync.
3. **Refresh:** before any API call, if the access token expires within 5 minutes, post `grant_type=refresh_token` to the token URL. Save both new tokens, because the refresh token can change.
4. **401 from the API:** refresh once and retry once. If the refresh itself fails, set status `NEEDS_RECONNECT`, fail the sync, and show the company in red on the operator console.
5. **Disconnect:** send the refresh token to the revoke URL, delete both tokens, and set status `DISCONNECTED`.

### QuickBooks requests

Every request is a `GET` with headers `Authorization: Bearer <access token>` and `Accept: application/json`. Query text is URL-encoded.

| Data | Request path | Fields used |
|---|---|---|
| One week of profit and loss | `/v3/company/{realmId}/reports/ProfitAndLoss?start_date=<Monday>&end_date=<Sunday>&accounting_method=<Accrual or Cash>` | In `Rows.Row`, the top-level rows whose `group` is `Income`, `COGS`, `Expenses`, `OtherIncome`, `OtherExpenses` and `NetIncome`. Each total is the last `Summary.ColData` entry's `value`, a decimal string. A missing group counts as 0. |
| Open invoices | `/v3/company/{realmId}/query?query=select * from Invoice where Balance > '0' STARTPOSITION <n> MAXRESULTS 1000` | `QueryResponse.Invoice`: `Id`, `DocNumber`, `TxnDate`, `DueDate`, `TotalAmt`, `Balance`, `CustomerRef.name`, `BillEmail.Address`. Keep paging (n = 1, 1001, 2001 and so on) until a page has fewer than 1,000. |
| Bank balances | `/v3/company/{realmId}/query?query=select * from Account where AccountType = 'Bank'` | `QueryResponse.Account`: sum of `CurrentBalance`, and the count |

Convert decimal strings to cents with `Decimal`, quantized to 0.01 with `ROUND_HALF_UP`. `"1234.56"` becomes 123456 and `"-50.00"` becomes -5000.

### Sync rules (`app/sync.py`)

1. **First sync:** the 60 most recent complete weeks of profit and loss (60 requests). **Later syncs:** the last 6 complete weeks, because bookkeepers fix past weeks late.
2. **Weeks** are upserted by `week_start`.
3. **Invoices** are upserted by `source_id`. A previously open invoice that is not returned this time becomes `is_open` = false with `balance_cents` = 0, because it was paid or voided. Sync never changes `is_disputed`.
4. **Bank balance:** insert 1 new `bank_balances` row.
5. **All or nothing:** a sync writes in 1 database transaction. Any error rolls everything back, sets the sync run to `FAILED` with the error, and changes no data.
6. **Schedule:** nightly at 02:00 local, Monday at 06:00 local before the report, and on **Sync now**. Skip a company whose sync is already `RUNNING`.
7. **Pace:** at most 1 request every 150 milliseconds per company.
8. **Test fixtures:** `tests/qbo_fixtures/` holds `token_response.json`, `pnl_week.json`, `invoices_page1.json` and `accounts_bank.json`, written from Intuit's documented response shapes. Before Phase 3 is marked done, run 1 real sync against an Intuit sandbox company and confirm the fixtures match its shapes. That one check is manual.

## 7. KPI formulas (`app/kpis.py`)

Every number in the report comes from one formula below, computed from the synced tables for one company and one report date. Section 15 works every formula through with real numbers.

### Notation

- **R** = the report date, a Monday, company-local.
- **W0** = the report week's `week_start` = R minus 7 days. **W1**, **W2** and so on are the weeks before it: W1 = R minus 14 days.
- A week "exists" if `weekly_financials` has a row for it. Formulas use only weeks that exist.
- **No data:** if a formula has no rows, or would divide by zero or by a negative number, the figure shows `No data`.
- Percentages and weeks show 1 decimal with `ROUND_HALF_UP`. Money in the report shows whole dollars with commas, rounded half up from cents. For example, 3249950 cents shows as `$32,500`.

### Formulas

| KPI | Formula | No data when |
|---|---|---|
| `revenue_week` | `revenue_cents` of W0 | W0 does not exist |
| `revenue_avg_4` | Average `revenue_cents` of W1, W2, W3 and W4 (those that exist) | None of them exist, or the average is 0 or less |
| `revenue_change_pct` | (`revenue_week` - `revenue_avg_4`) / `revenue_avg_4` × 100 | Either input is missing |
| `margin_week_pct` | (`revenue_week` - COGS of W0) / `revenue_week` × 100 | `revenue_week` is 0 or less |
| `margin_4wk_pct` | (sum of revenue - sum of COGS) / sum of revenue × 100, over W0, W1, W2 and W3 | Summed revenue is 0 or less |
| `net_income_week` | `net_income_cents` of W0 | W0 does not exist |
| `ar_total` | Sum of `balance_cents` over open invoices | Never; 0 is a valid total |
| `days_past_due` | R minus `due_date`, in calendar days, per invoice | |
| Aging bucket | `CURRENT` if days past due is 0 or less; `D1_30` for 1 to 30; `D31_60` for 31 to 60; `D61_90` for 61 to 90; `D91_PLUS` for 91 or more | |
| `ar_61_plus_pct` | (balances in `D61_90` + `D91_PLUS`) / `ar_total` × 100 | `ar_total` is 0 |
| `dso_days` | `ar_total` / (sum of revenue over the up-to-13 weeks ending at W0) × (number of those weeks × 7) | Summed revenue is 0 or less |
| `cash_on_hand` | `total_cents` of the newest `bank_balances` row | No row exists |
| `weekly_burn` | Average total costs (COGS + operating expenses + other expenses) over the up-to-8 weeks ending at W0 | No weeks, or the average is 0 or less |
| `runway_weeks` | `cash_on_hand` / `weekly_burn` | Either input is missing |
| `ttm_weeks` | Number of existing weeks among the 52 ending at W0 | |
| `ttm_net_income` | Sum of `net_income_cents` over those weeks. If `ttm_weeks` is 13 to 51, multiply by 52 / `ttm_weeks` and label it `annualized from <n> weeks`. | `ttm_weeks` is under 13 |
| `addbacks_total` | Sum of `annual_amount_cents` over the company's add-backs | |
| `sde` | `ttm_net_income` + `addbacks_total` | `ttm_net_income` is missing |
| `value_low`, `value_high` | `sde` × `multiple_low`, and `sde` × `multiple_high` (Section 13) | `sde` is missing. If `sde` is 0 or less, show `Not meaningful: earnings are zero or negative.` |
| Revenue chart | `revenue_cents` of W7 through W0, oldest first; a missing week draws as an empty bar labeled `n/a` | |
| Top overdue | Open invoices with days past due of 1 or more, sorted by days past due (highest first), then balance (highest first); first 5 | |

## 8. Top-3 issue rules (`app/issues.py`)

The report shows at most 3 issues, chosen by checking 7 fixed rules in priority order. Cash problems come first because they can sink a business fastest. Every line and every action is fixed text with numbers filled in. Nothing is written by AI.

### Rules, in priority order

| Priority | Code | Triggers when | Issue line (exact text) | Action line (exact text) |
|---|---|---|---|---|
| 1 | `RUNWAY_LOW` | `runway_weeks` < `runway_low_weeks` (default 8.0) | `Cash covers about {runway} weeks of costs.` | `Collect overdue invoices first and hold off on big purchases.` |
| 2 | `AR_SEVERE` | Any open invoice is 61 or more days past due with balance ≥ `ar_severe_min_cents` (default 250000). Use the one with the highest balance; ties go to the most days. | `{customer} owes {amount}, {days} days past due.` | `Call them this week.` |
| 3 | `AR_AGING` | `ar_61_plus_pct` > `ar_aging_pct` (default 20.0) | `{pct}% of what customers owe you is over 60 days past due.` | `Review the overdue list below and approve the reminder drafts.` |
| 4 | `REVENUE_DROP` | `revenue_change_pct` ≤ minus `revenue_drop_pct` (default 15.0) | `Revenue was {abs_pct}% below your 4-week average.` | `Check whether jobs slipped into this week or new leads dropped.` |
| 5 | `MARGIN_LOW` | `margin_4wk_pct` < `margin_target_pct` (default 40.0) | `Gross margin over 4 weeks is {m4}%, under your {target}% target.` | `Check pricing and material costs on your last 10 jobs.` |
| 6 | `DSO_HIGH` | `dso_days` > `dso_high_days` (default 45.0) | `Customers take about {dso} days to pay.` | `Ask for payment when the job is done and shorten due dates.` |
| 7 | `MARGIN_DROP` | `margin_week_pct` ≤ `margin_4wk_pct` minus `margin_drop_points` (default 5.0) | `Last week's gross margin was {mw}%, down from {m4}% over 4 weeks.` | `Look at last week's biggest jobs for cost overruns.` |

### How the 3 are chosen

1. Check the rules from priority 1 to 7.
2. A rule whose inputs show `No data` does not trigger.
3. Show the first 3 that trigger, in priority order. Store all triggered codes in `reports.issues`, including those not shown.
4. If none trigger, show the single line `No red flags this week.`
5. **Formats:** `{runway}`, `{pct}`, `{abs_pct}`, `{m4}`, `{mw}`, `{target}` and `{dso}` show 1 decimal. `{amount}` shows whole dollars. `{days}` is a whole number. `{abs_pct}` is the drop written without a minus sign.

Example with the Section 15 fixture: rules 1, 2, 3, 4 and 7 trigger, so the report shows lines for `RUNWAY_LOW`, `AR_SEVERE` and `AR_AGING`, and `reports.issues` stores all 5 codes.

## 9. The weekly report

Each Monday the app syncs at 06:00, builds the report at 06:30 and sends it at 07:00, company-local. A report built from data more than 8 days old is held back, never sent. The same Jinja2 template renders the email and the in-app view, so what the owner reads is exactly what the operator previewed.

### Schedule (the scheduler checks every 60 seconds)

| Local time on Monday | Step | Rule |
|---|---|---|
| 06:00 | Sync | QBO companies only (Section 6) |
| 06:30 | Generate | Creates 1 `reports` row per company per report date with status `GENERATED`. If one already exists for that date, do nothing. |
| `report_send_time` (default 07:00) | Send | Sends every `GENERATED` report for today. DEMO: write files and set `SENT`. LIVE: send through Postmark and set `SENT`, or `FAILED` with the error. |

1. **Held:** if `data_as_of` is more than 8 days before generation time, set status `HELD`, do not send, and show `Held: data is <n> days old` on the operator console.
2. **Failed sync, recent data:** if this morning's sync failed but `data_as_of` is within 8 days, generate anyway with this banner at the top: `Data as of <date> <time>. This week's sync failed, so numbers may be out of date.`
3. Companies with `report_enabled` false are skipped.
4. The operator can click **Preview** (builds the report without saving or sending), **Send now** (only for `GENERATED` or `HELD`), and **Resend** (only for `SENT`).

### Layout, top to bottom

The email is HTML with inline styles only: maximum width 640 px, system fonts, no images, no JavaScript and no tracking pixels. A plain-text version with the same sections goes with it.

1. **Title:** `{company_name}: week of {Mon date} to {Sun date}`, for example `Test HVAC: week of Oct 5 to Oct 11, 2026`.
2. **Data banner**, only when rule 2 above applies.
3. **4 tiles** in a 2-by-2 table:

   | Tile | Big number | Small line under it |
   |---|---|---|
   | Revenue | `revenue_week` | `{signed change}% vs 4-week average of {revenue_avg_4}`, for example `-20.0% vs 4-week average of $40,000` |
   | Gross margin | `margin_week_pct` | `4-week: {margin_4wk_pct}%` |
   | Cash | `cash_on_hand` | `{runway_weeks} weeks of costs` |
   | Owed to you | `ar_total` | `{ar_61_plus_pct}% over 60 days late` |

4. **This week's top issues:** the numbered issue lines from Section 8, each followed by its action line in gray.
5. **Who owes you:** a table with 1 row per aging bucket (label, invoice count, amount) and a total row. Then **Most overdue**: the top 5 overdue invoices with customer, invoice number, balance and days past due.
6. **Revenue, last 8 weeks:** an HTML table with 1 row per week: the Monday's date, a bar, and the amount. The bar is a `div` whose width is revenue / the 8-week maximum × 100%, with a minimum of 1% when revenue is above 0. A missing or negative week shows no bar and `n/a` or the negative amount.
7. **What the business might sell for:** `Earnings (SDE): {sde}`, plus `annualized from {n} weeks` when that applies. Then `Rough range: {value_low} to {value_high} ({multiple_low}x to {multiple_high}x earnings)` and, on its own line, `A rough estimate from your books, not an appraisal.`
8. **Reminders**, only when drafts exist: `{n} payment reminders are ready for you to review.` with a button linking to `{PUBLIC_BASE_URL}/reminders`.
9. **Footer:** `Data from QuickBooks as of {data_as_of, local}. Questions? Reply to this email.`

**Subject:** `{company_name} weekly report: revenue {revenue_week} ({signed change}% vs avg)`, for example `Test HVAC weekly report: revenue $32,000 (-20.0% vs avg)`. If revenue shows `No data`, use `{company_name} weekly report: week of {Mon date}`.

### Delivery

| Mailer | Used when | Behavior |
|---|---|---|
| `DemoMailer` | Company `send_mode` is `DEMO` | Writes `./outbox/report-<company_id>-<report_date>.html` and a `.eml` with all headers. Prints `[DEMO EMAIL] to=<masked> subject=<subject>`. |
| `PostmarkMailer` | Company `send_mode` is `LIVE` | `POST https://api.postmarkapp.com/email` with header `X-Postmark-Server-Token`. Fields: `From` = `{REPORT_FROM_NAME} <{REPORT_FROM_EMAIL}>`, `To` (1 email per recipient), `ReplyTo` = `OPERATOR_EMAIL`, `Subject`, `HtmlBody`, `TextBody`, `MessageStream` = `outbound`. Success when `ErrorCode` is 0. |

### LIVE mode preconditions

Switching a company to LIVE requires all of these. If any fails, refuse, list the failures and stay in DEMO.

1. `ALLOW_LIVE_EMAIL=true` in `.env`.
2. `POSTMARK_SERVER_TOKEN`, `REPORT_FROM_EMAIL`, `REPORT_FROM_NAME` and `OPERATOR_EMAIL` are set.
3. `SENDER_DOMAIN_VERIFIED=true` in `.env`, set after Postmark shows the sending domain's SPF and DKIM records verified.
4. `PUBLIC_BASE_URL` starts with `https://`.
5. The company has at least 1 report recipient.
6. The company's `source_type` is `QBO` and its connection environment is `PRODUCTION`. Sandbox or demo numbers must never reach a real owner.

The Postmark and Intuit accounts must belong to the business, not an individual. That step happens outside this app.

## 10. Overdue-invoice reminders (`app/reminders.py`)

The app drafts reminders, and nothing goes to a customer until the owner or operator approves it. Reminders stay polite, use amounts synced within the last 24 hours, go out at most once a week per invoice and at most 4 times in total, and never go to disputed invoices. California's Rosenthal Act covers businesses collecting their own consumer debts, and since July 1, 2025 some commercial debts too. These limits keep the app well inside polite territory, but have a lawyer review the templates before LIVE.

### Who gets a draft

Drafting runs on Mondays at 06:30 with the report, and when someone clicks **Draft reminders now**. An open invoice gets a draft only if all 7 are true:

1. Its balance is above 0.
2. Days past due is at least `reminder_min_days_past_due` (default 7).
3. It has a customer email that is valid and not suppressed.
4. It is not marked disputed.
5. It has no reminder with status `DRAFT` or `APPROVED`.
6. Its last `SENT` reminder, if any, went out 7 or more days ago.
7. It has fewer than `reminder_max_per_invoice` (default 4) `SENT` reminders.

`sequence_number` = its sent count + 1. The template level depends on days past due when drafted: 7 to 29 days is level 1, 30 to 59 is level 2, 60 or more is level 3.

### Templates (plain text)

| Level | Subject | Body |
|---|---|---|
| 1 | `Invoice {doc_number} from {company_name}` | `Hi {customer_name}, a quick reminder that invoice {doc_number} for {balance} was due on {due_date}. If you've already paid, thank you, and please ignore this note.` |
| 2 | `Invoice {doc_number} is {days} days past due` | `Hi {customer_name}, invoice {doc_number} for {balance} was due on {due_date} and is now {days} days past due. Could you let us know when we can expect payment? If something on the invoice looks wrong, just reply and we'll sort it out.` |
| 3 | `Please call us about invoice {doc_number}` | `Hi {customer_name}, invoice {doc_number} for {balance} is {days} days past due. Please reply or call us at {company_phone} so we can settle it together.` |

Every body continues with a blank line, `{payment_instructions}` (left out if that setting is empty), a blank line, `Thank you,` and `{company_name}`. Then this footer:

```
--
To stop payment reminders by email: {PUBLIC_BASE_URL}/r/{token}
```

| Variable | Replaced with | Example |
|---|---|---|
| `{customer_name}` | `invoices.customer_name` | `Delgado LLC` |
| `{doc_number}` | `invoices.doc_number` | `INV-104` |
| `{balance}` | Balance in dollars with commas and 2 decimals | `$4,800.00` |
| `{due_date}` | Month abbreviation, day without a leading zero, year | `Jul 31, 2026` |
| `{days}` | Days past due | `73` |
| `{company_name}`, `{company_phone}` | Company name and phone setting | |
| `{payment_instructions}` | Company setting, free text | `Pay online at example.com/pay or call us.` |

### Words that are never allowed

When a draft is edited or a template is saved, reject the text if it contains any of these, as whole words or phrases, ignoring case: `collection agency`, `collections`, `lawsuit`, `sue`, `legal action`, `attorney`, `credit report`, `credit bureau`, `late fee`, `interest charge`, `final notice`, `garnish`. The error reads `Remove "<word>". Reminders must stay polite and must not threaten.` Whole-word matching matters: `sue` must not match `issue`.

### Approving and sending

1. `/reminders` lists every `DRAFT` with invoice, customer, balance, days past due, level, and editable subject and body. Buttons: **Approve and send**, **Skip** (sets `CANCELLED`), **Mark invoice disputed** (sets the flag and cancels the draft), and **Approve all**.
2. Approving sets `APPROVED`, `approved_by_user_id` and `approved_at`.
3. Approved reminders go out only Monday to Friday, 09:00 up to 17:00, company-local, with at most 50 per company per day. Outside that window they wait.
4. **Checks just before sending:** if the newest successful sync finished more than 24 hours ago, keep `APPROVED`, show `Sync needed before sending`, and start a sync. If the invoice is no longer open, set `CANCELLED`. If its balance changed since drafting, set `CANCELLED` with note `Balance changed`; the next drafting run creates a new draft.
5. **Send** with From `{company_name} <{REPORT_FROM_EMAIL}>`, Reply-To = the company's `reminder_reply_to`, stream `outbound`. The result is `SENT`, or `FAILED` with the error.
6. When a sync finds an invoice paid, its `DRAFT` and `APPROVED` reminders become `CANCELLED`.
7. **Unsubscribe:** `GET /r/{token}` shows a **Confirm** button. `POST /r/{token}` adds a suppression `UNSUBSCRIBED` for that email in that company and cancels its open reminders. It shows `You won't get more payment reminders by email from {company_name}.`

## 11. Screens and routes

Owners mostly read the Monday email and approve reminders. The operator runs every client from 1 console. Pages are server-rendered with Jinja2, and HTMX swaps small parts.

### Top bar (every page after login)

The company name for owners, or a company switcher for the operator. Links: Home, Reports, Invoices, Reminders, Add-backs, then Operator console and Settings for `OPERATOR` only. Counter: `Reminders to review: N` (drafts for the active company). A mode badge reads `DEMO` on gray or `LIVE` on red.

### Pages

| Route | Page | Roles | Contents and actions |
|---|---|---|---|
| `GET/POST /login` | Login | Public | Wrong details: `Email or password is incorrect.` 10 failed attempts for one email within 15 minutes locks that email for 15 minutes after the 10th failure: `Too many attempts. Try again in 15 minutes.` |
| `GET /` | Home | Both | The newest report, shown in the page exactly as emailed, plus links to earlier reports |
| `GET /reports/{id}` | Past report | Both | The stored `html` of that report |
| `GET /invoices` | Invoices | Both | Open invoices with customer, number, due date, balance, days past due and bucket, sorted by days past due (highest first). **Mark disputed** and **Unmark** buttons. |
| `GET /reminders` | Reminders | Both | Section 10 |
| `GET /addbacks` | Add-backs | Both | List, add, edit and delete add-backs with category, label, yearly amount and note. Shows the resulting SDE and value range live. |
| `GET /account` | Account | Both | Change password |
| `GET /qbo/connect`, `GET /qbo/callback`, `POST /qbo/disconnect` | QuickBooks | Owner of that company, or operator | Section 6 |
| `GET /operator` | Operator console | `OPERATOR` | Every company: name, source and environment, connection status, last sync time and status, last report date and status, number of issues in the last report, and drafts waiting. Rows turn red for `NEEDS_RECONNECT`, `HELD` or `FAILED`. **New company** button. |
| `GET /companies/{id}` | Company | `OPERATOR` | Tabs: Overview (live KPIs), Connection (**Connect**, **Disconnect**, **Sync now**, sync history), Report (**Preview**, **Send now**, **Resend**, report history), Settings (Section 13), Recipients, Users |
| `GET/POST /r/{token}` | Reminder unsubscribe | Public | Section 10 |

**New company** asks for name, time zone, industry, source type and the owner's name and email. It creates the company, its settings row and an `OWNER` user with a temporary password, which the operator gives to the owner.

### Roles

| Action | `OWNER` | `OPERATOR` |
|---|---|---|
| View reports, invoices and add-backs | Own company | Any company |
| Approve, edit or skip reminders; mark invoices disputed | Yes | Yes |
| Edit add-backs | Yes | Yes |
| Connect or disconnect QuickBooks | Own company | Yes |
| Settings, recipients, LIVE switch, send or resend reports | No | Yes |
| Create companies and users | No | Yes |

Every `POST` carries a CSRF token. A user requesting a route above their role gets 403. A record from another company always returns 404.

## 12. Security, privacy and compliance

This app holds a client's full financial picture and live access to their books, so it gets stricter protection than the other two specs. This section is a build checklist, not legal advice. Have a lawyer review the reminder templates and the client agreement before the first LIVE client.

### Security

1. **QuickBooks tokens:** encrypted with Fernet using `TOKEN_ENCRYPTION_KEY`. Never logged, never shown, never sent to the browser. If the key is missing or invalid, the app prints an error and exits with code 1.
2. **OAuth `state`:** random, stored in the session, and checked on callback. A mismatch returns 400 and saves nothing.
3. **Least access:** request only the `com.intuit.quickbooks.accounting` scope. The app never calls a QuickBooks write endpoint, and tests assert that no `POST`, `PUT` or `DELETE` goes to a QuickBooks URL.
4. **Passwords:** argon2id, minimum 12 characters. The operator's temporary passwords must be changed at first login.
5. **Sessions:** signed cookie, `HttpOnly`, `SameSite=Lax`, and `Secure` when `PUBLIC_BASE_URL` starts with `https://`. Log out after 12 hours with no requests.
6. **CSRF** on every `POST` except `POST /r/{token}`, which uses its own token.
7. **Tenant isolation:** every query filters by the active company. A record from another company returns 404.
8. **Logs:** never log amounts, customer names, emails or tokens at `INFO` level.

### Privacy

1. Store only the fields in Section 5. Never store bank account numbers, card data, payroll or tax IDs.
2. When a client leaves, the operator clicks **Disconnect** (which revokes the tokens). Deleting the company's data is on the roadmap.
3. A client agreement, signed outside the app, should say what data is read, who sees it and how it is protected.

### Compliance, and where the code enforces it

| Requirement | Enforced by |
|---|---|
| Reminders only after a person approves them | Section 10 approval flow |
| Reminder amounts are true and current | Section 10 sending check: sync within 24 hours, same balance |
| No threats or collection language | Section 10 banned-words check |
| Reminders are infrequent and in business hours | At most once every 7 days and 4 total per invoice; weekdays 09:00 to 17:00 |
| No reminders on disputed invoices | Section 10 rule 4 |
| Customers can stop reminder emails | Section 10 unsubscribe link |
| Value estimate is never presented as an appraisal | Section 9 layout line 7 |
| Sandbox or demo numbers never reach a real owner | Section 9 LIVE precondition 6 |

## 13. Configuration

Company-level settings live in `company_settings` and are edited by the operator. Server-level settings live in `.env`. A value outside its allowed range is rejected on save with an error naming the field and its range.

### `company_settings` fields

| Field | Type | Default | Allowed values | Used in |
|---|---|---|---|---|
| `send_mode` | `SendMode` | `DEMO` | `DEMO`, `LIVE` | Section 9 |
| `report_send_time` | time `HH:MM` | `07:00` | `07:00` to `10:00` | Section 9 |
| `report_recipients` | JSON list of emails | empty | 0 to 5 valid emails (LIVE needs at least 1) | Section 9 |
| `accounting_method` | `AccountingMethod` | `ACCRUAL` | `ACCRUAL`, `CASH` | Section 6 |
| `multiple_low` | decimal | 2.5 | 1.0 to 6.0 | Section 7 |
| `multiple_high` | decimal | 3.5 | 1.0 to 8.0, and at least `multiple_low` | Section 7 |
| `runway_low_weeks` | decimal | 8.0 | 1.0 to 52.0 | Section 8 |
| `ar_severe_min_cents` | integer | 250000 | 0 to 100000000 | Section 8 |
| `ar_aging_pct` | decimal | 20.0 | 5.0 to 80.0 | Section 8 |
| `revenue_drop_pct` | decimal | 15.0 | 5.0 to 50.0 | Section 8 |
| `margin_target_pct` | decimal | 40.0 | 5.0 to 90.0 | Section 8 |
| `dso_high_days` | decimal | 45.0 | 10.0 to 180.0 | Section 8 |
| `margin_drop_points` | decimal | 5.0 | 1.0 to 30.0 | Section 8 |
| `reminders_enabled` | boolean | true | Turning on requires `company_phone_e164` and `reminder_reply_to` | Section 10 |
| `reminder_min_days_past_due` | integer | 7 | 1 to 60 | Section 10 |
| `reminder_max_per_invoice` | integer | 4 | 1 to 6 | Section 10 |
| `reminder_reply_to` | email | empty | Valid email | Section 10 |
| `company_phone_e164` | string | empty | Empty, or valid E.164 | Section 10 |
| `payment_instructions` | string | empty | Up to 300 characters; passes the banned-words check | Section 10 |

### Environment variables (`.env`)

| Name | Required | Example | Purpose |
|---|---|---|---|
| `APP_SECRET_KEY` | Always | 64 random hex characters | Signs sessions and CSRF tokens. Missing or shorter than 32 characters: exit with code 1. |
| `TOKEN_ENCRYPTION_KEY` | Always | A Fernet key from `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` | Encrypts QuickBooks tokens. Missing or invalid: exit with code 1. |
| `DATABASE_URL` | Always | `sqlite:///./kpi.db` | |
| `SCHEDULER_ENABLED` | Always | `true` | Tests set `false` |
| `PUBLIC_BASE_URL` | Always | `http://localhost:8000` in DEMO | Links in emails. Must be `https://` for LIVE. |
| `LOG_LEVEL` | No | `INFO` | |
| `QBO_CLIENT_ID`, `QBO_CLIENT_SECRET` | For QuickBooks | From the Intuit developer app | |
| `QBO_REDIRECT_URI` | For QuickBooks | `http://localhost:8000/qbo/callback` | Must exactly match the redirect URI saved in the Intuit app |
| `QBO_ENVIRONMENT` | For QuickBooks | `sandbox` | `sandbox` or `production` |
| `ALLOW_LIVE_EMAIL` | Always | `false` | Must be `true` for LIVE |
| `POSTMARK_SERVER_TOKEN` | LIVE | (secret) | |
| `REPORT_FROM_EMAIL`, `REPORT_FROM_NAME` | LIVE | `reports@yourdomain.com`, `Your Brand Reports` | Sender of reports and reminders |
| `OPERATOR_EMAIL` | LIVE | `you@yourdomain.com` | Reply-To on reports |
| `SENDER_DOMAIN_VERIFIED` | LIVE | `false` | Set `true` after Postmark verifies the domain |

## 14. Demo data and the pitch demo

`python -m app.seed --demo` builds a fake HVAC company on `DemoDataSource` whose report always shows the same 3 issues, so the pitch works offline and identically every time. Everything random uses `random.Random(42)`, and every date is placed relative to the day the seed runs ("seed day"). If a company named `Glendale Comfort Air (Demo)` already exists, the command prints `Demo company already exists.` and exits with code 1 without changing anything.

### Demo data contents

| Item | Exact content |
|---|---|
| Company | `Glendale Comfort Air (Demo)`, industry `HVAC`, source `DEMO`, mode `DEMO`, recipients `owner@demo.local`, phone `+18185550100`, reply-to `owner@demo.local`, payment instructions `Pay online at example.com/pay or call us.` |
| Users | `operator@demo.local` (`OPERATOR`, "Demo Operator") and `owner@demo.local` (`OWNER`, "Demo Owner"). Both use password `demo-password-123`, with no forced change. |
| Weeks | 60 complete weeks ending the Sunday before seed day. Base revenue $38,000 a week, times 1.35 in June to September weeks, 1.10 in December to February, and 0.90 otherwise, then ±8% noise. COGS = 55% of revenue ±3 points. Operating expenses $10,500 a week. Other expenses $400 a week. Net income = revenue - COGS - operating expenses - other expenses. |
| Last week (W0) | Overrides the pattern: revenue = 82% of the average of W1 to W4, and COGS = 62% of W0's revenue |
| Cash | Set so that `runway_weeks` comes out between 5.5 and 6.0 |
| Invoices | 14 open invoices. One is `Delgado Property Mgmt`, `INV-2207`, balance $4,800.00, exactly 73 days past due, with email `ap@example.com`. Enough others are over 60 days past due that `ar_61_plus_pct` is between 25 and 35. 1 overdue invoice has no email and 1 is marked disputed. |
| Add-backs | Owner pay $120,000; interest $8,000; depreciation and amortization $15,000; one-time $7,000 |
| Report | Generated for the most recent Monday on or before seed day, so Home shows it right away |
| Reminders | Drafted once, giving exactly 6 drafts. The invoice with no email and the disputed one get none. |

The resulting report must show exactly these 3 issue lines: `RUNWAY_LOW`, then `AR_SEVERE` naming `Delgado Property Mgmt owes $4,800, 73 days past due.`, then `AR_AGING`. `REVENUE_DROP` must also trigger but stay unshown.

### The 5-minute pitch demo

The app must support this exact walkthrough with no errors and no internet. Log in as `owner@demo.local`.

1. **Home.** "This lands in your inbox at 7am every Monday."
2. **The 4 tiles:** revenue down 18% against the 4-week average, gross margin, cash covering under 6 weeks of costs, and what customers owe.
3. **Top issues:** "Delgado Property Mgmt owes $4,800, 73 days past due."
4. **Reminders.** Open the Delgado draft (level 3), change one word, click **Approve and send**. It shows `SENT` and the email appears in `./outbox`.
5. **Add-backs.** Change owner pay from $120,000 to $150,000. SDE rises by $30,000, so the value range rises by $75,000 at the low end (30,000 × 2.5) and $105,000 at the high end (30,000 × 3.5).
6. **Value panel:** "Roughly what your business would sell for today, from your own books."
7. Close: "$500 to set up and $350 a month. I connect to your QuickBooks in 10 minutes, and your first report comes Monday."

## 15. Build phases and acceptance tests

The build has 8 phases, 0 to 7, with 77 automated tests and 1 manual check. Each test ID becomes at least 1 pytest test. A phase passes only when all its tests pass and all earlier phases still pass.

### Test conventions

1. Freeze the clock with `freezegun` in every time-based test.
2. "Local" means `America/Los_Angeles`. October 2026 is PDT (UTC-7).
3. Set `SCHEDULER_ENABLED=false` and call `run_sync`, `generate_report`, `send_reports`, `draft_reminders` and `send_reminders` directly with a `now` argument.
4. Mock every HTTP call with `httpx.MockTransport`. QuickBooks responses come from `tests/qbo_fixtures/`.
5. Give every test a fresh SQLite database in `tmp_path`, built with `build_base_fixture()`.

### Base fixture (`build_base_fixture()`)

Report date **R** = Monday 2026-10-12, so **W0** = 2026-10-05.

| Record | Exact values |
|---|---|
| Company | `Test HVAC`, source `DEMO`, Section 13 defaults, phone `+18185550100`, reply-to `owner@test.local` |
| Users | `operator@test.local` (`OPERATOR`), `owner@test.local` (`OWNER`); password `test-password-123`; no forced change |
| Other company | `Other Co` with `other@test.local` (`OWNER`) |
| Last sync | 1 `sync_runs` row, `SUCCEEDED`, finished 2026-10-12 06:00 local |
| Cash | 1 `bank_balances` row: 18000000 cents ($180,000) |

| `week_start` | Revenue | COGS | Operating expenses | Other expenses | Net income |
|---|---|---|---|---|---|
| 2026-09-07 | 4000000 | 2200000 | 1000000 | 0 | 800000 |
| 2026-09-14 | 4400000 | 2400000 | 1000000 | 0 | 1000000 |
| 2026-09-21 | 3600000 | 2000000 | 1000000 | 0 | 600000 |
| 2026-09-28 | 4000000 | 2200000 | 1000000 | 0 | 800000 |
| 2026-10-05 | 3200000 | 2000000 | 1000000 | 0 | 200000 |

| Invoice | Customer | Email | Due date | Balance | Days past due on R | Bucket |
|---|---|---|---|---|---|---|
| INV-101 | Acme Property | `ap@acme.example.com` | 2026-10-20 | 500000 | -8 | `CURRENT` |
| INV-102 | Baker Home | `pay@baker.example.com` | 2026-09-30 | 300000 | 12 | `D1_30` |
| INV-103 | Chen Dental | `office@chen.example.com` | 2026-08-28 | 200000 | 45 | `D31_60` |
| INV-104 | Delgado LLC | `ap@delgado.example.com` | 2026-07-31 | 480000 | 73 | `D61_90` |
| INV-105 | Evans Family | none | 2026-06-01 | 120000 | 133 | `D91_PLUS` |

### Phase 0: scaffold, database, login, encryption

| ID | Setup and action | Pass when |
|---|---|---|
| T0.1 | Migrate an empty database | Table names, excluding `alembic_version`, equal exactly the 13 in Section 5 |
| T0.2 | Log in with right and wrong passwords | Right: 302 to `/`. Wrong: 200 with `Email or password is incorrect.` |
| T0.3 | 10 wrong logins, then the right one; then freeze 15 min 1 s after the 10th failure and retry | Attempt 11 shows the lockout text; the later attempt returns 302 |
| T0.4 | `GET /operator` as owner, then operator | 403, then 200 |
| T0.5 | `other@test.local` requests a `Test HVAC` report | 404 |
| T0.6 | Encrypt `abc`, then decrypt | Stored value is not `abc`; decrypting gives `abc` |
| T0.7 | Start without `TOKEN_ENCRYPTION_KEY` | Exit code 1 |
| T0.8 | Any `POST` without a CSRF token | 403 |

### Phase 1: weeks and KPI formulas

| ID | Check | Arithmetic | Pass when |
|---|---|---|---|
| T1.1 | Week helpers | R = 2026-10-12 | W0 = 2026-10-05, W1 = 2026-09-28; label `Oct 5 to Oct 11, 2026`; a non-Monday report date raises an error |
| T1.2 | Revenue | Average = (4000000 + 4400000 + 3600000 + 4000000) / 4 = 4000000. Change = (3200000 - 4000000) / 4000000 × 100 | `revenue_week` 3200000; `revenue_avg_4` 4000000; change -20.0 |
| T1.3 | Margins | Week: (3200000 - 2000000) / 3200000 × 100 = 37.5. 4 weeks: revenue 15200000, COGS 8600000, so 6600000 / 15200000 × 100 = 43.42 | 37.5 and 43.4 |
| T1.4 | Receivables | Total = 500000 + 300000 + 200000 + 480000 + 120000 = 1600000. Over 60 = (480000 + 120000) / 1600000 × 100 | 1 invoice per bucket; `ar_total` 1600000; `ar_61_plus_pct` 37.5 |
| T1.5 | DSO | 5 weeks of revenue = 19200000, days = 35. 1600000 / 19200000 × 35 = 2.917 | 2.9 |
| T1.6 | Burn and runway | Costs per week 3200000, 3400000, 3000000, 3200000, 3000000; average = 15800000 / 5 = 3160000. Runway = 18000000 / 3160000 = 5.696 | `weekly_burn` 3160000; `runway_weeks` 5.7 |
| T1.7 | TTM with only 5 weeks | 5 is under 13 | `ttm_net_income` and `sde` show `No data` |
| T1.8 | SDE and value | 52 weeks: 48 at 300000 + 4 at 900000 = 18000000. Add-backs 12000000 + 800000 + 1500000 + 700000 = 15000000. SDE = 33000000. | Low 33000000 × 2.5 = 82500000; high 33000000 × 3.5 = 115500000 |
| T1.9 | Annualized | 26 weeks summing to 9000000. 9000000 × 52 / 26 | 18000000 with label `annualized from 26 weeks` |
| T1.10 | Negative SDE | TTM -20000000 + add-backs 15000000 = -5000000 | `Not meaningful: earnings are zero or negative.` |
| T1.11 | Whole-dollar display | | 3249950 shows `$32,500`; 3249949 shows `$32,499` |
| T1.12 | Missing data | Delete W0; delete every invoice | Revenue and change show `No data`; `ar_total` shows `$0`; `ar_61_plus_pct` shows `No data`; no exception |

### Phase 2: issue rules

| ID | Setup and action | Pass when |
|---|---|---|
| T2.1 | Base fixture | Triggered codes, in order: `RUNWAY_LOW`, `AR_SEVERE`, `AR_AGING`, `REVENUE_DROP`, `MARGIN_DROP` (37.5 ≤ 43.4 - 5.0 = 38.4). Shown: the first 3. |
| T2.2 | Base fixture | Lines exactly `Cash covers about 5.7 weeks of costs.`, `Delgado LLC owes $4,800, 73 days past due.` and `37.5% of what customers owe you is over 60 days past due.` |
| T2.3 | INV-104 balance 249999 | `AR_SEVERE` does not trigger. Over-60 share = (249999 + 120000) / 1369999 × 100 = 27.0, so `AR_AGING` still triggers. Third shown line: `Revenue was 20.0% below your 4-week average.` |
| T2.4 | 2 invoices of 500000, at 70 and 80 days past due | `AR_SEVERE` names the 80-day one |
| T2.5 | 5 weeks each at revenue 4000000, COGS 2200000, operating expenses 1000000; cash 100000000; 1 current invoice | Runway = 100000000 / 3200000 = 31.25. Only line: `No red flags this week.` |
| T2.6 | Base fixture with no `bank_balances` rows | `RUNWAY_LOW` does not trigger |
| T2.7 | `runway_low_weeks` = 5.0 | 5.7 is not under 5.0, so `RUNWAY_LOW` does not trigger |
| T2.8 | `MARGIN_DROP` rule with inputs (week 37.4, 4-week 42.4), then (37.5, 42.4) | First triggers (37.4 ≤ 37.4). Second does not (37.5 > 37.4). |

### Phase 3: data sources and sync

Freeze time at 2026-10-12 10:00 UTC unless stated.

| ID | Setup and action | Pass when |
|---|---|---|
| T3.1 | Call `DemoDataSource` twice | Identical results; 0 network calls |
| T3.2 | `GET /qbo/connect` | 302 to the authorization URL with `client_id`, `scope=com.intuit.quickbooks.accounting`, `redirect_uri` and `state`; the session holds that `state` |
| T3.3 | Callback with the wrong `state` | 400; no connection saved |
| T3.4 | Callback with the right `state`; token mock returns `expires_in` 3600 | Status `CONNECTED`; `access_expires_at` = 11:00 UTC; stored token values are not the plain tokens |
| T3.5 | Any API call at 10:56 UTC | 1 refresh first (expiry within 5 minutes); the new refresh token from the response replaces the old one |
| T3.6 | API returns 401 once, then 200 | Exactly 1 refresh and 2 API calls; sync `SUCCEEDED` |
| T3.7 | Refresh returns 400 `invalid_grant` | Status `NEEDS_RECONNECT`; sync `FAILED` |
| T3.8 | Parse `pnl_week.json`: Income `32000.00`, COGS `20000.00`, Expenses `10000.00`, no OtherExpenses row, NetIncome `2000.00` | Row: 3200000, 2000000, 1000000, other expenses 0, net income 200000 |
| T3.9 | Invoice query: page 1 returns 1,000, page 2 returns 3 | 2 query calls with `STARTPOSITION 1` and `STARTPOSITION 1001`; 1,003 invoices stored |
| T3.10 | An invoice open last sync, missing now, with `is_disputed` true | `is_open` false, balance 0, `is_disputed` still true |
| T3.11 | Bank query errors after the profit-and-loss calls succeed | Sync `FAILED`; `weekly_financials` row count unchanged |
| T3.12 | First sync, then a second sync | 60 profit-and-loss calls, then 6 |
| T3.13 | All Phase 3 traffic | 0 `POST`, `PUT` or `DELETE` requests to any QuickBooks API host. Posts to the token and revoke URLs are allowed. |
| T3.14 | **Disconnect** | 1 call to the revoke URL with the refresh token; tokens deleted; status `DISCONNECTED` |

### Phase 4: weekly report (DEMO)

| ID | Setup and action | Pass when |
|---|---|---|
| T4.1 | Generate at 2026-10-12 06:30 local | 1 row, `GENERATED`, `report_date` 2026-10-12, `week_start` 2026-10-05; `metrics` has `revenue_week` 3200000 and `runway_weeks` 5.7 |
| T4.2 | Generate again the same day | Still 1 row |
| T4.3 | Subject | Exactly `Test HVAC weekly report: revenue $32,000 (-20.0% vs avg)` |
| T4.4 | HTML body | Contains `Test HVAC: week of Oct 5 to Oct 11, 2026`, the 3 issue lines from T2.2, and `-20.0% vs 4-week average of $40,000`. Contains no `<script` and no `<img`. |
| T4.5 | Revenue chart | Weeks 08-17, 08-24 and 08-31 show `n/a`. The 09-14 bar is 100%. The 10-05 bar = 3200000 / 4400000 × 100 = 72.7%. |
| T4.6 | Send at 07:00 local | Status `SENT`; `./outbox/report-<company_id>-2026-10-12.html` exists |
| T4.7 | Last successful sync finished 2026-10-03 06:00 local; generate at 2026-10-12 06:30 | More than 8 days old, so `HELD`; still `HELD` after the 07:00 send step |
| T4.8 | Last success 2026-10-10; this morning's sync failed | `GENERATED`, and the HTML contains `This week's sync failed` |
| T4.9 | `report_enabled` false | No report row |
| T4.10 | T1.8 data and add-backs | HTML contains `Rough range: $825,000 to $1,155,000 (2.5x to 3.5x earnings)` |

### Phase 5: reminders

Drafting runs at Monday 2026-10-12 06:30 local unless stated.

| ID | Setup and action | Pass when |
|---|---|---|
| T5.1 | Base fixture | 3 drafts: INV-102 level 1 (12 days), INV-103 level 2 (45), INV-104 level 3 (73). INV-101 is not due; INV-105 has no email. |
| T5.2 | INV-103 marked disputed first | 2 drafts |
| T5.3 | INV-102 had a `SENT` reminder on 2026-10-08; then repeat with 2026-10-05 | First: no INV-102 draft (4 days). Second: a draft (7 days), with `sequence_number` 2. |
| T5.4 | INV-104 already has 4 `SENT` reminders | No INV-104 draft |
| T5.5 | INV-104 draft body | Starts exactly `Hi Delgado LLC, invoice INV-104 for $4,800.00 is 73 days past due.` |
| T5.6 | Edit a body to include `We will take legal action`; then a body containing `issue` | First rejected with `Remove "legal action". Reminders must stay polite and must not threaten.` Second accepted. |
| T5.7 | Approve at 08:00 local; run the sender at 08:30, then 09:00 | `APPROVED` after 08:30; `SENT` after 09:00 |
| T5.8 | Last successful sync 2026-10-11 08:00 local; run the sender at 2026-10-12 09:00 | 25 hours old, so it stays `APPROVED` with `Sync needed before sending` |
| T5.9 | Balance changes from 480000 to 300000 between drafting and sending | `CANCELLED` with note `Balance changed` |
| T5.10 | A sync finds INV-102 paid | Its `DRAFT` becomes `CANCELLED` |
| T5.11 | Approved on Saturday 2026-10-17 at 10:00 | Waits, then sends at Monday 2026-10-19 09:00 |
| T5.12 | `POST /r/<token>` for the INV-104 reminder; then draft again | Suppression for `ap@delgado.example.com`; its open reminders `CANCELLED`; no new draft for it |
| T5.13 | 51 approved reminders at Monday 09:00 | 50 `SENT` Monday; the 51st sends Tuesday at 09:00 |

### Phase 6: LIVE mode (Postmark mocked)

| ID | Setup and action | Pass when |
|---|---|---|
| T6.1 | Switch to LIVE with `ALLOW_LIVE_EMAIL=false` | Refused, failure listed, mode stays `DEMO` |
| T6.2 | Everything ready, but `source_type` `DEMO` | Refused on precondition 6 |
| T6.3 | Everything ready, QuickBooks connection in `SANDBOX` | Refused on precondition 6 |
| T6.4 | All preconditions true with a `PRODUCTION` connection and 2 recipients; send the report | Exactly 2 posts to Postmark, each with `MessageStream` `outbound`, `ReplyTo` = `OPERATOR_EMAIL`, and both `HtmlBody` and `TextBody`. `ErrorCode` 0, so status `SENT`. |
| T6.5 | Postmark returns `ErrorCode` 300 | `FAILED`; `error_text` contains `300` |
| T6.6 | Send an approved reminder in LIVE | `From` display name is `Test HVAC`; `ReplyTo` = `reminder_reply_to` |
| T6.7 | Save `multiple_low` 2.5 with `multiple_high` 2.0; `report_send_time` 06:00; 6 recipients; payment instructions containing `late fee` | All 4 rejected |
| T6.8 | Turn on reminders with no `company_phone_e164` | Rejected |

### Phase 7: demo seed and pitch

| ID | Setup and action | Pass when |
|---|---|---|
| T7.1 | Run the demo seed on a fresh database | Exit 0. 1 company, 2 users, 60 week rows, 14 open invoices, 1 report, 6 drafts. Shown issues `RUNWAY_LOW`, `AR_SEVERE`, `AR_AGING`; triggered codes include `REVENUE_DROP`. Runway 5.5 to 6.0. Over-60 share 25 to 35. Line exactly `Delgado Property Mgmt owes $4,800, 73 days past due.` |
| T7.2 | Run it again | Exit 1; every count unchanged |
| T7.3 | Raise owner pay by 3000000 | `value_low` up 3000000 × 2.5 = 7500000; `value_high` up 3000000 × 3.5 = 10500000 |
| T7.4 | Approve and send the Delgado draft in DEMO | `SENT`; its `.eml` file exists in `./outbox` |
| Manual | Turn off Wi-Fi and run the 7-step pitch demo in Section 14 | Every step works, no error page appears, and the run takes under 5 minutes |

**v1 is done** when `pytest -q` reports 77 or more passing tests with 0 failures, the manual pitch check passes, and the one-time sandbox shape check from Section 6 passes.

## 16. Out of scope for v1, and what comes later

Nothing below gets built until v1 passes every test and a paying client is using it. The list is in build order.

| Order | Feature | Why it waits |
|---|---|---|
| 1 | Hosting with Postgres, daily backups and HTTPS | Required before the first real QuickBooks connection |
| 2 | Intuit production app approval | Needed before any real client connects. The business entity applies. |
| 3 | Delete a company's data on request | Needed when a client leaves |
| 4 | PDF copy of the report | Some owners print or forward it |
| 5 | Job-level margin, by pulling job or class tags from QuickBooks | Shows which job types lose money |
| 6 | 13-week cash forecast | The natural next question after runway |
| 7 | Xero support | Widens the market beyond QuickBooks |
| 8 | Buyer-ready package: 3 years of profit and loss, add-back schedule and earnings trend in 1 file | Directly feeds the seller-financed deals in your acquisition plan, so build it when an owner starts talking about selling |
