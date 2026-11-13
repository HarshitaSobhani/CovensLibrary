# CLAUDE.md — Coven's Library

Private, owner-only library management web app for one physical library (~50 members, ~200 catalogue editions; physical copies tracked separately). Product name: **Coven's Library** (exact apostrophe; never "Coffee"). Local-first on the owner's Dell; cloud-portable later. No member accounts, no multi-tenant.
Authority order: `OPEN_QUESTIONS.md` §1 (decision register) → `PRD.md` → `docs/*` → `Claude_Code_Master_Prompt.md`.

## Status
Phases 1a, 1c, 1b and 2 (2a members, 2b catalogue, 2c Excel import) are implemented and verified on PostgreSQL 16 (see `docs/IMPLEMENTATION_LOG.md`). L6 two-role database hardening is implemented (`docs/DATABASE_SECURITY.md`). Phases 3a-3d (subscription policy, finance ledger and receipts, tokens/registration/renewal, refunds/reversals/adjustments) are implemented and verified, awaiting owner review (`docs/IMPLEMENTATION_LOG.md`). Phase 3 is released (committed and pushed). Phases 4a-4b (loans: issue, return, renewal, grace, automatic day-8 forfeiture with the system actor, Poor / Damaged / Lost decisions, grace-change decisions) plus the transactional outbox (`core_domainevent`, D-23) are implemented, verified and owner-accepted (released). Phase 4 is released (`20a87c3`). Phase 5 is released (`3378e8a`). Phases 6a-6b (reports with both revenue bases, member statement, CSV/XLSX/PDF exports with formula escaping and masking for anonymised members, full ZIP export) are implemented, verified and owner-accepted (released with commit 'Complete Phase 6 reporting and exports'). Phase 6 is released (`2d7433b`). Phase 7a (backups: `pg_dump -Fc` + media + manifest, AES-256-GCM, SHA-256, verified upload, retention, catch-up, dashboard health) is implemented and verified, uncommitted; real Google Drive and Windows Credential Manager behaviour are UNVERIFIED. Phase 7b (CLI restore with disposable-database rehearsals) and 7c (serving, scheduling scripts, headers/CSP, loopback check, DEPLOYMENT/OPERATIONS/SECURITY_REVIEW/LOCAL_VS_CLOUD documents) are implemented and verified on the development machine, uncommitted. NOTHING is verified on the Dell (Windows 11), real Google Drive or the Credential Manager, and recovery is NOT verified until a restore on a clean Windows 11 machine has been performed. Windows 11 runtime testing is pending (Phase 7c).

## Hard rules
- Git: commits are authored by the owner (Harshita Sobhani) only; the owner authorised one push of the Phase 1a-2 work directly to `main`. No other commits, pushes, branches or PRs unless the owner explicitly asks. Never alter Git history.
- Do not invent business rules. The decision register is complete; no specification decision is outstanding. Anything not covered by the register must be asked, never assumed.
- No secrets in the repo (`.env.example` placeholders only). Never modify `assets/brand/references/*`.
- Server-side validation + DB constraints for critical rules. Money = integer paise.
- Financial, token, loan and subscription history is immutable: corrections = reversal/adjustment records. No hard delete.
- No React, microservices, Redis, Celery; Docker not mandatory; no offline mode, payment gateway, member accounts, reservations. New dependencies need justification in `docs/TECHNICAL_ARCHITECTURE.md` §14.
- Do not claim tests passed unless run; do not claim recovery verified until a clean-machine restore has been performed.

## Environments (document separately)
- Development: MacBook M3, macOS.
- Runtime: Dell, **Windows 11** (edition/build unspecified until verified); localhost-only 127.0.0.1; no LAN; Task Scheduler, `waitress`, PowerShell. Never call it Windows 10.
- Code stays OS-neutral. Timezone `Asia/Kolkata` everywhere.

## Stack (pin in Phase 1)
Python 3.12, Django 5.2 LTS, PostgreSQL, Django Templates + HTMX, Bootstrap 5 + CSS tokens, Chart.js. `keyring` (Windows Credential Manager) is approved but must not be installed until implementation is approved. Modular monolith (apps in `docs/TECHNICAL_ARCHITECTURE.md` §3).

## Business invariants (all confirmed)
- **Dates:** start inclusive; expiry date = *first expired day*; last valid day = expiry − 1; calendar-month addition with end-of-month clamp (31 Aug 2026+6 → expiry 28 Feb 2027, last valid 27 Feb 2027; 29 Feb 2028+12 → 28 Feb 2029 / 27 Feb 2029). All date arithmetic only in `memberships/policy.py`.
- **Subscriptions:** ₹800/6 mo, ₹1,500/12 mo; no auto-renewal; one current + at most one scheduled; late renewal starts on owner-entered activation date; full payment before activation; **fees never refundable** (no fee-refund type/workflow); allocation daily pro-rata at full precision, rounded only on display.
- **Loans:** one active loan/member; 30 days; renewal +30 days, unlimited, never after the due date; due date ≤ last valid day (never the first expired day); new loans blocked from last valid day − 15 (10 May → 25 April); both rules independent. Issuable only Good + Available (condition and status are separate fields).
- **Token (₹500, not revenue):** grace 7 days (setting, applies to existing loans); day 8 → **automatic** forfeiture, no confirmation, even if returned on day 8; missed processing runs at next launch with effective date = day 8 plus processing timestamp. Damaged, Poor, Lost → owner confirms with a mandatory reason; copy leaves circulation (Poor → condition Poor + status Withdrawn); Lost closes loan as Lost. Grace-setting change that newly makes a loan eligible → owner confirmation, with full record; if declined: not posted, mandatory reason, audited, no re-prompt, day-8 job skips it. Recovered lost book → no auto-reversal; separate owner-approved adjustment. After forfeiture the existing subscription stays valid but **renewal and new borrowing are blocked until a fresh ₹500 token is paid** (same member ID, new token cycle). Old forfeiture stays.
- **Finance & IDs:** internal transaction ID = UUID; four separate per-year receipt sequences (calendar year of the receipt **issuance** date; a backdated payment date never changes it) `SUB-YYYY-000001`, `TOK-…`, `REF-…`, `ADJ-…`; member `COV-M-000001`; copy `COV-C-000001` (barcode = copy ID); combined registration printout shows both receipts; forfeitures/reversals have their own immutable records, no receipt; split cash/UPI; backdating = any past date with reason + audit (+ extra confirmation if financial).
- **Corrections & limits:** subscription payments and paid refunds are never reversed; corrections = owner-approved adjustments, which are never a fee refund (money-out needs an explicit confirmation); renewal needs a held token; largest amount Rs 49,999.00 incl. book unit cost (64-bit paise + CHECKs on every money column); reasons ≤ 2,000 characters (rejected, not truncated); backdating (incl. a past subscription start) is audited.
- **Members/catalogue:** subscription dates + owner status; reactivation = restore or new subscription; editions ≠ copies; author blank or "Unknown author"; per-import choices (categories, copy IDs); duplicate ISBN = warning; Code 128 labels, configurable layout; E.164 phones; anonymisation never rewrites old backups (disclose).
- **Comms:** automatic SMTP email. Scheduled at 09:00 Asia/Kolkata: expiry reminder 15 days before expiry; overdue reminders on due date, day 3, day 7. Immediate: activation, payment receipt, loan issue, renewal, return, token forfeiture (day-8 forfeiture notice immediately after processing). Retries: initial immediately; retry 1 five minutes after the initial failure, retry 2 thirty minutes after retry 1 fails, retry 3 two hours after retry 2 fails (delays from the preceding failed attempt); then mark failed, keep error details, no further retries. Retries reuse the same notification record; no duplicate emails; missing email = skipped with reason (Prepared / SMTP accepted / SMTP failed / Skipped; never "delivered"). WhatsApp never automatic (prefilled link, owner sends).
- **Auth:** email password reset (fallback: local CLI + recovery code: single-use, hashed, shown once, 15-minute expiry); owner authenticates daily, sessions end at day end, **unlimited logins per day**; rate limit 5 failures/15 min, failures audited, **no lockout**.
- **Dashboard:** overdue, upcoming expiries, backup failures, failed emails, damaged/lost decisions, token forfeitures, members requiring attention (defaults: expired subscriptions; borrowing suspended after token forfeiture; overdue books; unresolved damaged/lost cases; pending owner decisions; incomplete membership records; criteria and visibility owner-configurable). **No Pending Payments section.**
- **Backup:** daily fixed time + launch catch-up; warn if > 26 h; `pg_dump -Fc` + media + manifest → AES-256-GCM → SHA-256 → Drive (`drive.file`, owner-selected folder) → verify → 30 daily + 12 monthly; working key in Windows Credential Manager; separate recovery key never beside backups; replacement computer restores the key after owner confirmation; export = ZIP (CSV + Excel + manifest).

## Commands — macOS development machine only (see `docs/DEVELOPMENT.md`)
Dell runtime (Windows 11, PowerShell) instructions: `docs/DEPLOYMENT.md` and `docs/OPERATIONS.md` (written, unverified on Windows).
```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements/dev.txt
cp .env.example .env   # then fill real local values
createdb coven_library
python manage.py migrate --settings=config.settings.migrate   # owner role; runtime role is refused
python manage.py check_db_security
python manage.py create_owner --username <name> --email <address>
python manage.py runserver 127.0.0.1:8000
python manage.py test --settings=config.settings.test
python manage.py makemigrations --check --dry-run
ruff check . && ruff format --check .
```

## Database roles (L6, implemented on macOS; Windows 11 unverified)
`postgres` break-glass only; `coven_owner` owns objects and runs migrations/grants/restores; `coven_app` is runtime only. Table classes MUTABLE / NO_DELETE / APPEND_ONLY / READ_ONLY in `core/dbsecurity.py`; unclassified tables fail closed; `ALTER DEFAULT PRIVILEGES` only revokes (nothing is granted automatically); every new table must be classified. Owner credentials live only in a git-ignored, plaintext `.env.owner` (0600; never in runtime config, backups or logs). After any restore: provision roles first, then `apply_db_grants`. Details: `docs/DATABASE_SECURITY.md`.

## Brand
Lettering is image artwork, not a font; usable as logo art. Legible complementary UI font required. Palette provisional (charcoal, terracotta, cream, muted green). Originals untouched; derived assets separate.

## Phase protocol
Objective/files/migrations/acceptance → smallest slice → tests (normal, invalid, boundary) → checks → secret scan → real results → update `IMPLEMENTATION_PLAN.md`. Stop if a phase hits an open question.

## Docs
`PRD.md`, `OPEN_QUESTIONS.md`, `IMPLEMENTATION_PLAN.md`, `docs/ERD.md`, `docs/RISKS.md`, `docs/TECHNICAL_ARCHITECTURE.md`. Later: `docs/DEPLOYMENT.md`, `docs/OPERATIONS.md`.
