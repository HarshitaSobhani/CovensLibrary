# TECHNICAL ARCHITECTURE — Coven's Library

Status: specification for approval; nothing implemented. Versions to be verified and pinned at Phase 1. Business rules: `OPEN_QUESTIONS.md` §1 (authoritative); data model: `docs/ERD.md`.

## 1. Principles
Modular monolith. One PostgreSQL database. Server-rendered Django Templates + HTMX. Business rules live in service functions backed by DB constraints. No React, microservices, Redis or Celery; Docker is not required.

## 2. Stack and environments
Python 3.12 · Django 5.2 LTS (re-check support window at Phase 1) · PostgreSQL (same major version on dev, runtime, any future cloud) · Django Templates + HTMX · Bootstrap 5 + CSS variables · Chart.js. Custom user model from the start; single owner, no signup route.

| Environment | Machine | Notes |
|---|---|---|
| Development | MacBook M3, macOS | developer tooling; documented separately |
| Runtime | Dell, **Windows 11** (edition/build unspecified until verified) | localhost-only; Task Scheduler; `waitress`; PowerShell |

Application code stays OS-neutral (no hard-coded paths); all machine-specific settings via environment variables, so a later cloud move is a configuration change. Timezone `Asia/Kolkata` for every date, timestamp, calculation, schedule, backup and audit event.

## 3. Apps and dependency direction
| App | Owns |
|---|---|
| `core` | layout, theme, `LibrarySetting`, `SettingChangeEvent`, `ID_SEQUENCE`, dashboard preferences, global search |
| `accounts` | owner login/logout, end-of-day session expiry, `LoginEvent`, rate limiting, email password reset, CLI recovery + single-use 15-minute recovery code |
| `audit` | `AuditEvent`, `record()` helper |
| `members` | `Member`, status, anonymisation |
| `catalogue` | categories, editions, acquisitions, copies, copy events, label layouts, Excel import |
| `memberships` | plans, subscriptions, **`policy.py` (all date rules)**, registration/renewal/rejoin orchestration |
| `finance` | transactions, payment entries, receipt documents, token accounts/cycles/ledger, reversals, adjustments |
| `circulation` | loans, renewals, returns, overdue/grace, forfeiture triggering, pending forfeitures |
| `communications` | templates, notification log, SMTP sender, WhatsApp link builder |
| `reports` | read-only queries, exports, statements, ZIP export |
| `backups` | backup pipeline, key handling, Drive, retention, restore |

Allowed imports: `core`, `audit` ← all. `members → core,audit`; `catalogue → core,audit`; `finance → members,audit`; `memberships → members,finance`; `circulation → members,catalogue,memberships,finance`; `communications → members,memberships,circulation,finance` (read); `reports → all` (read-only); `backups → core`. No cycles (import-lint test planned).

## 4. Service layer
`<app>/services/*.py`; each operation `@transaction.atomic`, locks rows (`select_for_update`), raises typed `RuleViolation`, calls `audit.record()`. Views are thin.

**Date policy (`memberships/policy.py`, the only place date arithmetic lives)** — pure functions with `today` injected:
- `expires_on` = start + N calendar months, day clamped to the target month's last day (31 Aug 2026+6 → 28 Feb 2027; 31 Jan 2027+1 → 28 Feb 2027; 29 Feb 2028+12 → 28 Feb 2029). Start inclusive.
- `last_valid_day = expires_on − 1`. Expired from `expires_on`.
- `max_due_date = last_valid_day` (issue and renewal). First expired day never valid.
- `issue_blocked_from = last_valid_day − 15` (10 May → 25 April). Independent of `max_due_date`; both checked.
- Renewal rejected if `today > due_on` or new due date > `max_due_date`.
- Grace end = `due_on + 7` (live setting); day 8 = `due_on + 8`.
- Early renewal starts at the current subscription's `expires_on`; late renewal at the owner-entered activation date; at most one scheduled subscription.

## 5. Rule enforcement map
| Rule | Service | DB backstop |
|---|---|---|
| one active loan per member/copy | `issue_loan` | partial unique indexes |
| no subscription overlap | `create_subscription` | exclusion constraint |
| one scheduled subscription max | `create_subscription` (member lock) | — |
| fully paid before activation | single atomic registration/renewal txn | transaction rows created settled |
| fees non-refundable | no fee-refund service/kind exists | CHECK on `kind` enum |
| token once per cycle; no double refund/forfeit | `collect_token`, `refund_token`, `forfeit_token` | partial unique per cycle |
| renewal and new borrowing blocked while current token cycle is forfeited (subscription stays valid); fresh token = new cycle | `renew_subscription`, `issue_loan`, `start_new_token_cycle` | — |
| Good + Available issuable; Poor ⇒ Withdrawn | `issue_loan`, return service | CHECKs on copy |
| reversal not deletion | `reverse_transaction` | append-only triggers, UK `reverses_id` |

## 6. Identifiers and numbering
Formats: member `COV-M-000001`; copy `COV-C-000001` (barcode value = copy ID); internal transaction ID = UUID for every financial transaction; four separate receipt sequences, each numbered per year: `SUB-YYYY-000001` (subscription), `TOK-YYYY-000001` (token collection), `REF-YYYY-000001` (token refund), `ADJ-YYYY-000001` (adjustment). Forfeitures and reversals carry a UUID only. A combined registration printout lists both the subscription and token-collection receipts. Sequences come from `ID_SEQUENCE` keyed by (name, year) with row lock inside the creating transaction (gaps on rollback acceptable, duplicates impossible). `YYYY` is the **calendar year (Jan–Dec, Asia/Kolkata) of the receipt issuance date**; a backdated payment/effective date never changes it (e.g. effective 31 Dec 2026, issued 5 Jan 2027 → `SUB-2027-…`). The sequence is keyed by (name, year) using the issuance year.

## 7. Scheduled automation (no Celery)
One idempotent management command, run daily at the configured time by **Windows Task Scheduler** on the Dell and also at application launch (catch-up). It performs, in order: overdue day-8 forfeitures, automatic emails, backup (and the 26-hour health check). Idempotency by unique constraints (one forfeiture per token cycle, `dedupe_key` per notification).
- **Day 8 forfeiture:** loan with `today ≥ due_on + 8` and still unreturned → automatic full forfeiture, `owner_confirmed=false`, `effective_date = due_on + 8`, `processed_at = now`. If the Dell was off, the next launch processes it with the **original effective date**. A return recorded on day 8 still results in forfeiture.
- **Grace-setting changes:** saving a new `grace_days` records `SettingChangeEvent`, computes loans that become newly eligible, and creates `PendingForfeiture` rows; each requires owner confirmation before any ledger entry. Normal day-8 processing is not affected. If the owner **declines**, nothing is posted; a mandatory reason and audit record are stored; the owner is not prompted again for that event and the daily job skips the loan's declined event.
- **Owner-confirmed forfeitures:** Damaged, Poor and Lost returns post only after explicit owner confirmation with a mandatory reason; Poor sets condition `Poor` and status `Withdrawn`, Damaged sets status Damaged; the copy leaves circulation; Lost closes the loan as Lost. A later-recovered lost book never auto-reverses; correction requires a separate owner-approved adjustment.
- **Backdating:** any past date with mandatory reason and audit; financial effects need additional owner confirmation; event date, effective date and `processed_at` stored separately.

## 8. Backup and replacement-computer recovery
**Pipeline:** `pg_dump -Fc` → archive with `media/` + manifest (app version, PostgreSQL version, migration list, timestamp, key fingerprint) → **AES-256-GCM** (chunked) → **SHA-256** → upload to the owner-selected Google Drive folder (`drive.file` scope) → verify upload (size/hash) → `BackupRun` → retention (30 daily, 12 monthly independently; monthly = promoted copy; never delete the newest successful backup; dry-run and fake-clock tests).
**Schedule/health:** daily at fixed time; catch-up at next launch; dashboard warns if last success > 26 h; failures shown.
**Key custody:** the key is randomly generated. On the Dell the working key is stored in **Windows Credential Manager** (protected by the Windows user account); never in the repo, database, backups or the Drive folder. The owner saves a separate **recovery key**; it is never stored with backups. Losing the recovery key may make backups unrecoverable and the owner is told so. Credential Manager entries are **not** assumed to transfer between computers.
**Restore (CLI-first):** `manage.py restore_backup` with typed confirmation and a pre-restore backup. Replacement-computer procedure:
1. Install prerequisites (Python, PostgreSQL, application code) on the replacement Windows computer.
2. Re-authorise Google Drive (fresh OAuth consent) and obtain the encrypted backup.
3. Obtain the separately stored recovery key.
4. Owner explicitly confirms key restoration.
5. Restore the working key into Windows Credential Manager from the recovery key.
6. Verify key access (fingerprint matches manifest).
7. Verify backup integrity (SHA-256).
8. Restore database and media.
9. Check PostgreSQL/application/migration compatibility; run migrations if required.
10. Verify data relationships and application functionality.
Recovery is **not** claimed verified until this has been rehearsed on a clean machine.

## 9. Local-first deployment
Bind `127.0.0.1` only; no LAN. `waitress` as WSGI server on Windows (pure Python); WhiteNoise for static files; run via Task Scheduler/service wrapper (choice in Phase 7c). Configuration only through environment variables (secret key, allowed hosts, database, SMTP, Drive, timezone). SMTP and Google credentials are never committed. Cookie `SECURE` flags configurable; verify on the Dell browser. Local-vs-cloud memo due before launch (cost, uptime, HTTPS, personal-data location, migration = backup → restore).

## 10. Authentication
Owner only. Django password hashing. Email-based password reset; when SMTP is unavailable, local CLI recovery with a recovery code that is single-use, stored hashed, displayed once and expires after 15 minutes. The owner authenticates at least daily; sessions expire at end of day (`Asia/Kolkata`); **unlimited logins per day** (logout, lost or expired sessions never block another login). Failed logins are audit-logged; **rate limiting** applies after **5 failures within 15 minutes** (throttled until the window expires; success resets the counter). **No account lockout.**

## 11. Notifications
**Email — automatic via SMTP.** **Scheduled reminders** run at **09:00 Asia/Kolkata**: subscription expiry reminder **15 days before the expiry date**; overdue reminders on the **due date, day 3 and day 7** (days counted after the due date); subscription expiry notice. **Immediate notifications:** subscription activation, payment receipt, loan issue, loan renewal, book return and token forfeiture — the day-8 forfeiture notification is sent **immediately after forfeiture processing**. **SMTP retries:** the initial email is sent immediately; **retry 1 is 5 minutes after the initial failure, retry 2 is 30 minutes after retry 1 fails, retry 3 is 2 hours after retry 2 fails** (each delay runs from the preceding failed attempt, not cumulatively from the first failure). After the third retry fails the notification is marked **failed** and the error details are retained; there is no further retrying. Retries reuse the same notification record, so they never create duplicate emails; the same event never produces more than one email (duplicate prevention). A member with no email address is recorded as **skipped with the reason `no email address`**. Statuses: Prepared, SMTP accepted, SMTP failed, Skipped; SMTP accepted ≠ delivered or read. All scheduled times are in Asia/Kolkata. Duplicate prevention by `dedupe_key` (member + event + occurrence); retry state (`attempt_count`, `next_retry_at`) lives on the same `NotificationLog` row. Failed emails appear on the dashboard. **WhatsApp — manual:** the app builds a prefilled `wa.me` message for the selected member's E.164 number; the owner reviews and sends; the owner may mark "Marked sent". Templates use restricted placeholder substitution (not Django templates).

## 12. Dashboard
Default sections: overdue books, upcoming subscription expiries, backup failures, failed emails, damaged/lost decisions, token forfeitures, members requiring attention. **No Pending Payments section.** Owner configures section visibility and the criteria of "Members requiring attention" (stored in `DashboardPreference`). Default criteria: expired subscriptions; borrowing suspended after token forfeiture; overdue books; unresolved damaged/lost cases; pending owner decisions; incomplete membership records.. Monthly collections show all receipts with subscription fees, token collections and other transactions separately.

## 13. Reports and export
Reports: members and subscriptions, subscription collections (payment date), subscription revenue allocation, token collections/refunds/forfeitures, adjustments, loans/overdue, damaged/lost, inventory/circulation, member history. Allocation is daily pro-rata computed at **full precision at report time**; rounded only for display; per-day rounded amounts are never stored. Outputs CSV/XLSX/PDF with formula-injection escaping. Full export = ZIP (CSV + Excel + manifest).

## 14. Dependencies (each justified)
| Package | Why |
|---|---|
| Django, psycopg[binary] | framework, PostgreSQL driver |
| openpyxl | Excel import/export (pure Python) |
| ReportLab | receipts/reports PDF and Code 128 barcodes; pure Python (WeasyPrint needs native libraries, awkward on Windows) |
| cryptography | AES-256-GCM backup encryption |
| keyring | **approved** (not installed until implementation is approved) — access to Windows Credential Manager for the working key |
| google-api-python-client, google-auth-oauthlib | Drive upload (`drive.file`) |
| phonenumbers | E.164 validation/normalisation |
| whitenoise | static files without a separate web server |
| waitress | production WSGI on Windows |
| SortableJS (static asset) | dashboard section rearranging |
| Dev: ruff, coverage | quality |
Rejected: django-axes (own small rate-limit table), FullCalendar (server-rendered month grid), Celery/Redis, WeasyPrint, python-dateutil (month arithmetic written and tested in `policy.py`).

## 15. Design system
Brand references (preserved in `assets/brand/references/`): wordmark on dark green, vertical vine lettering sticker, cream card with ornate burnt-orange frame, botanical wreath. The lettering is **image artwork, not an installable font**; it may be used as logo artwork ("Coven's" lettering + "Library" in the UI font). Originals are never modified; derived assets live separately (clean vector/transparent originals requested; wordmark source is only 404×192 px, stickers are angled photos). The word "Coffee" never appears in the UI.
Provisional tokens (replace on owner palette; contrast measured, text ≥ 4.5:1):

| Token | Light | Dark |
|---|---|---|
| `--bg` | #F6EFE2 parchment | #141512 charcoal |
| `--surface` | #FBF7EE | #1D1F1B |
| `--text` | #1F1D1A (14.7:1) | #EFE7D6 (14.9:1) |
| `--text-muted` | #6B665A (5.0:1) | #A8A294 (7.2:1) |
| `--primary` terracotta | #A8431A (5.3:1; white on it 6.0:1) | #E8763C (6.2:1) |
| `--accent-green` muted | #4F6B4A (5.2:1) | #8FAE88 (7.5:1) |
Typography tokens: `--font-brand` (logo image), `--font-display`, `--font-ui` — replaceable; a legible complementary UI font until supplied. Decorative art only on login, empty states, header, receipts; never in dense tables.

## 16. Testing
Django test runner + coverage on a real PostgreSQL test DB (SQLite cannot test exclusion/partial constraints). Layers: constraint/model, policy (table-driven boundaries incl. 31 Aug/31 Jan/29 Feb, 25 April block, due = last valid day accepted / first expired day rejected), services, views and an all-URLs-require-login sweep, import, finance (split payments, separate sequences, token excluded from revenue, reversal keeps original, no fee-refund path), circulation (day-8 boundary 10→17→18 May, return on day 8 forfeited, grace-change confirmation, missed-processing effective date, backdating), auth (unlimited logins, end-of-day expiry, rate limit 5/15 without lockout), notifications (automatic email states, WhatsApp never sent), backup (round trip, tamper detection, retention with fake clock, key restore into a stub credential store), restore on scratch DB, export escaping.

## 17. Security
Owner-only views via base mixin + URL sweep; CSRF; no PII/secrets in logs or audit summaries; upload type/size limits, no macros; escaped exports; secrets only in environment/Credential Manager; `DEBUG=False` outside development; localhost binding.
