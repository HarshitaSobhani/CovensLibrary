# IMPLEMENTATION_PLAN.md — Coven's Library

Status: Phases 1a, 1c, 1b and 2 (2a, 2b, 2c) are implemented and verified (evidence: `docs/IMPLEMENTATION_LOG.md`). L6 two-role database hardening is implemented and verified on macOS (`docs/DATABASE_SECURITY.md`); Windows 11 is unverified. Phases 3a-3d are implemented and verified (awaiting owner review, uncommitted). Phases 4a-4b are implemented and verified (awaiting owner review, uncommitted). **Phase 5 onward is NOT STARTED and awaits owner approval.** Completing documentation is not implementation progress. No commits/pushes unless the owner asks. Phase 0 documentation work is the only permitted activity until the owner approves Phase 1.

All business-policy decisions (A1, A2, B1–B44, N1–N12), all specification questions (Q1–Q8, including Q7b receipt year and the Q8 email schedule) and the dashboard defaults are confirmed. **No phase has an open decision gate.** `keyring` is approved but not installed.

| Phase | Scope | Key tests | Gate | Status |
|---|---|---|---|---|
| **0** | Specification audit and documentation | consistency review | — | docs only |
| **1a** | Repo hygiene (`.gitignore` incl. `.DS_Store`; the already-tracked `.DS_Store` files are intentionally left tracked per owner decision), Django project, settings split, PostgreSQL, `.env.example`, ruff, test runner, custom User, `Asia/Kolkata` | settings load, `makemigrations --check` | — | implemented and verified |
| **1b** | Owner auth: login/logout, end-of-day session expiry, unlimited logins/day, rate limiting (5 failures/15 min, no lockout), failed-login audit, email password reset, CLI recovery + recovery code | anonymous redirected; re-login after logout/lost session; 5/15 throttle expires; success resets; reset flow | — | implemented and verified |
| **1c** | Base layout, sidebar, tokens, light/dark, `LibrarySetting`, sequences, `audit` | contrast, sequence uniqueness, audit write | — | implemented and verified |
| **2a** | `Member`: CRUD, status, E.164 phones, permanent ID, search, archive/anonymise (backup disclosure), reactivation choice | duplicate phone, ID immutable, no cascade delete | — | implemented and verified |
| **2b** | `catalogue`: categories, editions (blank/Unknown author), acquisitions, copies, condition vs status, Code 128 labels with configurable layout | unique copy/barcode, Good+Available issuable, Poor unavailable | — | implemented and verified |
| **2c** | Excel import: validation, preview, ISBN warnings, per-import category and copy-ID choices, confirm, error report | invalid/duplicate rows, atomic commit, copy-ID conflicts | — | implemented and verified |
| **3a** | `memberships.policy` (all date rules) + plans/snapshots | boundary tables: 11 Nov→10 May; 31 Aug→27 Feb; 31 Jan+1→27 Feb; 29 Feb 2028+12→27 Feb 2029; block from 25 April; due=last valid accepted, first expired rejected | — | implemented and verified (awaiting owner review) |
| **3b** | `finance`: transactions (global internal ID), four receipt sequences, split payments, receipt documents/PDF, append-only triggers | split cash/UPI settle one obligation, separate sequences, no edit/delete, no fee-refund kind | — | implemented and verified (awaiting owner review) |
| **3c** | Token cycles/ledger, registration, renewal (one scheduled max), fresh-token new cycle | token once per cycle, not revenue, renewal and new borrowing blocked after forfeiture (subscription stays valid), fresh ₹500 starts new cycle and restores borrowing, same member ID | — | implemented and verified (awaiting owner review) |
| **3d** | Token refund, reversals, adjustments | reversal keeps original, no double refund, refund blocked with outstanding loan | — | implemented and verified (awaiting owner review) |
| **4a** | Issue/return: all issue blocks, condition capture, copy events, backdating with reason + financial confirmation | one loan/member/copy (incl. concurrent), 15-day block, due cap, unavailable copy | — | implemented and verified (awaiting owner review) |
| **4b** | Renewal (none after due date), overdue/grace, **automatic day-8 forfeiture**, missed-processing effective date, Damaged/Poor/Lost confirmations (Poor → Withdrawn), grace-change pending forfeitures | 10→17→18 May; return on day 8 forfeited; effective date vs processed_at; grace change creates pending confirmation; declined forfeiture not posted, reason required, no re-prompt; lost recovery no auto-reversal | — | implemented and verified (awaiting owner review) |
| **5a** | Dashboard default sections (no Pending Payments), visibility + attention criteria config, calendar, search | counts match; configuration persists | — | implemented and verified (awaiting owner review) |
| **5b** | Automatic SMTP email (9 events), states, WhatsApp prefilled link (manual) | no "delivered"; failure recorded; WhatsApp never auto | — | implemented and verified (awaiting owner review) |
| **6a** | Reports + both revenue bases (full precision) | tokens excluded; allocation sums exact | — | not started |
| **6b** | Exports, member statement, ZIP export | formula escaping, access control | — | not started |
| **7a** | Backup pipeline, Credential Manager key, 26 h warning, catch-up, retention, Drive | round trip, tamper, retention fake clock | — (`keyring` approved) | not started |
| **7b** | CLI restore, replacement-computer key restoration, **real clean-machine restore on Windows 11** | restore verification passes | — | not started |
| **7c** | Hardening, `DEPLOYMENT.md`, `OPERATIONS.md`, local-vs-cloud memo, Dell browser check. **`DEPLOYMENT.md` must carry the Phase 5 deployment checklist** (details: `docs/DATABASE_SECURITY.md` "Phase 5 deployment safeguards"): (1) take a verified backup, then apply migrations as `coven_owner` in order, remembering that a refused `communications.0003` does not mean `migrate` changed nothing (other apps' pending migrations run first); (2) the populated-database guard in `communications.0003` refuses to run when notification or attempt rows exist, and `COVEN_ALLOW_NOTIFICATION_MIGRATION=1` overrides it only with a documented plan, because it permanently drops attempt addresses and its reversal fails when attempt rows exist; (3) run `manage.py preflight_notifications` (read-only report) and, if it finds anything, `--apply`; (4) verify "Nothing to redact" / "Verified: nothing remains" and `check_db_security` under both roles BEFORE enabling notification processing (`run_daily_jobs`, scheduled task, post-write sending) | PRD acceptance list | — | not started |

## Order
1a → 1c → 1b → (2a, 2b) → 2c → 3a → 3b → 3c → 3d → 4a → 4b → 5 → 6 → 7. There are no open decision gates; each phase still needs explicit owner approval to start.

## Dependency notes (verified)
- **1c before 1b:** login auditing and the login page need the `audit` app, base layout and `LibrarySetting`/timezone from 1c. Phase labels stay 1a/1b/1c; execution order is 1a → 1c → 1b.
- **1b** uses only Django's basic mail backend for password reset; the full `communications` app arrives in 5b.
- **Domain events:** stored durably in the transactional outbox `core_domainevent` (D-23, implemented with Phase 4); 5b implements delivery, notification records, retries and scheduled reminders; 7c schedules the jobs.
- **Scheduled command:** the single idempotent daily command is created in 4b (day-8 forfeiture), extended in 5b (09:00 reminders, retries) and 7a (backup, 26 h check); Windows Task Scheduler configuration is delivered in 7c with catch-up on launch.
- **5a** dashboard widgets for failed emails and backup failures are built with empty states and wired to real data in 5b and 7a.
- **4a** needs 2b (copies), 3a (policy) and 3c (token cycles, borrowing suspension); **4b** needs 3c/3d (ledger) and 1c (setting-change events).
- **7a** uses the approved `keyring`; not installed until implementation is approved.

## Protocol
Objective/files/migrations/acceptance → implement → tests → ruff + migration check → secret scan → real results → update Status only when implemented and verified.
