# Implementation log — Phases 1a, 1c, 1b, 2 (2a, 2b, 2c)

Scope authorised by the owner: Phase 1a → 1c → 1b → Phase 2 (2a members, 2b catalogue, 2c Excel import), then stop. Phase 3 and later are **not started**. Nothing has been deployed. Business rules were not changed.

Environment used for verification: Linux container, Python 3.12.3, Django 5.2.19, **PostgreSQL 16.15** (local cluster, role without superuser). macOS and Windows 11 were **not** exercised.

## Phase results
| Phase | Delivered | Tests at completion (all passing) |
|---|---|---|
| 1a Foundation | Django 5.2 project, split env-driven settings (`local`/`production`/`test`), custom `accounts.User`, PostgreSQL, `Asia/Kolkata`, `.gitignore` (incl. `.DS_Store`), `.gitattributes`, `.env.example`, ruff/coverage config, `requirements/`, `docs/DEVELOPMENT.md` | 9 |
| 1c Layout + audit | `core` (LibrarySetting singleton, race-safe `IdSequence`), `audit` (append-only `AuditEvent`, secret-scrubbing `record()`), base layout, sidebar, light/dark tokens (WCAG AA tested), vendored Bootstrap 5.3.3 CSS, all-routes-require-login sweep | 21 |
| 1b Authentication | login/logout, session ends at next Kolkata midnight, unlimited logins, rate limit 5 failures/15 min (no lockout, success resets, throttled attempts do not extend window), failed-login audit, email password reset, CLI `issue_recovery_code` + single-use hashed 15-minute code and `/accounts/recover/` | 43 |
| 2a Members | `Member` (COV-M-000001, E.164 phones with country picker, unique mobile, immutable ID trigger, no hard delete), status workflow, reactivation choice, anonymisation with disclosure, search/list/pagination | 62 |
| 2b Catalogue | `Category` (case-insensitive unique), `BookEdition`, `Acquisition`, `BookCopy` (COV-C-000001, condition vs status with DB CHECKs, Poor⇒Withdrawn), append-only `CopyEvent`, ISBN validation + duplicate warning, `LabelLayout` + Code 128 label PDF | 89 |
| 2c Excel import | template workbook, staged validation (`ImportBatch`/`ImportRow`), preview, per-import category and copy-ID choices, ISBN warnings, owner confirmation, atomic execution, error-report CSV | 111 |
| Integration | end-to-end owner flow + PostgreSQL schema guarantees | **117 total** |

## Models and migrations created
- `accounts`: `User`, `LoginEvent`, `RecoveryCode` — `0001_initial`, `0002_recoverycode_loginevent`
- `core`: `LibrarySetting`, `IdSequence` — `0001_initial`
- `audit`: `AuditEvent` — `0001_initial`, `0002_append_only` (trigger)
- `members`: `Member` — `0001_initial`, `0002_membership_no_immutable` (trigger), `0003_member_help_text`
- `catalogue`: `Category`, `BookEdition`, `Acquisition`, `BookCopy`, `CopyEvent`, `LabelLayout`, `ImportBatch`, `ImportRow` — `0001_initial`, `0002_copyevent_append_only` (trigger)
Migrations were applied forward on an empty PostgreSQL database, reversed to zero (including trigger `RunSQL` reverses) and applied forward again; `makemigrations --check` reports no changes.

## Verification run (actual results)
- `python manage.py check`: no issues. `ruff check .` and `ruff format --check .`: pass.
- `python manage.py test --settings=config.settings.test`: **117 tests, OK** (PostgreSQL test database). Coverage 90% overall.
- `check --deploy` (production settings): 4 expected warnings (HSTS, SSL redirect, secure session/CSRF cookies). The app is localhost-only over HTTP; the cookie flags are environment-driven (`SESSION_COOKIE_SECURE`, `CSRF_COOKIE_SECURE`).
- `runserver 127.0.0.1` served `/` (HTTP 200 for the redirect chain).
- Mutation spot-checks: changing the rate-limit threshold, session expiry day, sequence locking and the ISBN-acknowledgement rule each made tests fail, so those tests do detect regressions.
- Brand reference images: git object hashes identical to `HEAD`.
- Secret scan: no secrets in tracked files; `.env` is git-ignored.
- `btree_gist`: available as a trusted extension; created successfully by the non-superuser application role (needed from Phase 3).

## Corrected during the work (not hidden)
- Settings imported before `.env` loaded → fixed (`local`/`test` load the file first).
- Singleton `LibrarySetting.save()` attempted a second INSERT → fixed.
- Integration test wrongly expected the PostgreSQL connection timezone to be Asia/Kolkata. Django stores UTC and converts in Python; the **test** was corrected to assert that real behaviour (the application timezone itself is Asia/Kolkata).
- Mermaid ERD had invalid key syntax (`FK UK`) → fixed and re-parsed.

## Known limitations / not done
- Windows 11 (Dell) and macOS runs not performed; Windows work pending: Python 3.12 + PostgreSQL install, `waitress`, Task Scheduler, PowerShell setup docs (`docs/DEPLOYMENT.md`), a Windows test run, `pg_dump` PATH, Credential Manager (Phase 7). Deferred to Phase 7c by the owner.
- No Phase 3+ features: subscriptions, finance, circulation, communications, dashboard widgets, reports, backups. `keyring` approved but not installed (Phase 7a).
- Dashboard is a placeholder; sidebar entries for later phases are disabled.
- No derived logo assets yet; the header uses a text wordmark. Fonts/palette provisional.
- Bootstrap CSS is vendored (MIT); HTMX/Chart.js not yet used.
- Password-reset link lifetime uses Django's default (not specified by the owner).
- Recovery codes are generated on demand through the CLI command (interpretation of "separately generated recovery code" plus the 15-minute expiry).
- Members `needs_new_subscription` flag is stored but only consumed in Phase 3c.
- Import batches are not expired or cleaned up automatically.
- Coverage gaps: some view error branches (members views 68%).
- `LoginEvent.username_tried` stores the attempted username (no passwords).
- Failed or skipped tests: none.

---

# Remediation pass after the Phase 1-2 acceptance review (uncommitted)

Scope authorised: M1-M6 and selected low findings; no Phase 3. Nothing here is committed; the working tree holds the changes for owner review.

| Finding | Status | What changed |
|---|---|---|
| M4 Sensitive data | **Fixed** | A typed username is stored only if it matches a real account; otherwise only an HMAC key (rate-limit use) is kept. `audit/redaction.py` redacts keys (password, secret, api_key, otp, recovery_code, code_hash, passphrase, token, reset_token, authorization, key, session and variants) and credential-shaped values, recursively, without touching business data. Historical rows were not modified. |
| M1 Import validation | **Fixed** | Lengths come from the model fields; infinity/NaN/overflow/non-integers/over-large numbers, two-decimal costs, printable-ASCII and non-reserved supplied copy IDs are validated; unforeseen database errors roll back and give a clear message. Preview "valid" now equals "execution succeeds" (boundary test imports rows at every limit). |
| M2 Rate-limit race | **Fixed** | Check, authentication and recording run in one transaction under PostgreSQL advisory locks (identifier key + client address). 24 simultaneous bad logins -> exactly 5 evaluated, 19 throttled. |
| M3 Password reset | **Fixed** | Requests and completions audited (no address/token/link); 3 requests per 15 minutes per email key and per address, identical generic response; lifetime explicitly set and tested (now 3,600 s, see the final adjustments below). Throttle number recorded as owner decision D-2. |
| M5 Contrast/theming | **Fixed** | Link, danger, table, header, input, border and focus colours come from tokens; tests cover token contrast (text 4.5:1, boundaries/focus 3:1) and the wiring. Browser measurement (not part of the suite): links 5.61-7.05:1, text 13.5-15.7:1, tables and inputs follow the theme, no horizontal overflow at 390 px. |
| M6 Dev database | **Done** | `coven_library` (local cluster, 127.0.0.1:5432, 0 users/0 audit/0 login rows, no other sessions) backed up with `pg_dump -Fc` to the session scratch directory outside the repository (41,728 bytes, readable), then 6 migrations applied (0 unapplied afterwards). |
| L1 Responsive | Fixed | Sidebar stacks below 768 px. |
| L2 Label default | Fixed | New default replaces the old; "Make default" action. |
| L3 Query parameters | Fixed | Controlled messages; also found and fixed a NUL-byte search term that caused HTTP 500. |
| L4 Pagination | Fixed | Parameters URL-encoded. |
| L5 Duplicate-mobile race | Fixed | Clean validation error; sequence stays gap-free. |
| L7 Single owner | Fixed | `accounts_single_owner` constraint + `create_owner` command. |
| L8 Startup | Fixed | Explicit settings selection in `manage.py`, `.env.example`, `docs/DEVELOPMENT.md`. |
| L9 Docs | Fixed | ERD, architecture (§10, §14), open decisions. |
| L10 CSV | Fixed | Tabs, CR/LF, spaces, NBSP, BOM, zero-width space handled. |
| L11 Upload | Partly hardened | Read bounded to 5 MB, size checked before reading, decompressed-size/entry/ratio limits, macro payload refused, scanned-row and column caps. The 5 MB content is still held in memory (acceptable for an owner-only app). |
| L12 Minor | Mostly fixed | Library-timezone year, cost decimals, ISBN-10/13 duplicates, dead route removed, preview paging, newest recovery code supersedes earlier ones. Not done: pruning of old login events/sessions (`clearsessions` documented). |
| L6 DB-level delete/TRUNCATE protection | **Resolved** | See "L6 database security hardening" below and `docs/DATABASE_SECURITY.md`. |

## Verification (actual runs, isolated PostgreSQL test databases)
- `manage.py test --settings=config.settings.test`: **194 tests, 0 failed, 0 skipped**, no warnings (was 117; 77 added).
- Concurrency tests (rate limit, duplicate mobile, sequences) run five times in a row: all passed.
- `manage.py check`, `makemigrations --check --dry-run`: clean. `ruff check` / `ruff format --check`: clean (89 files).
- Coverage 92% overall (accounts/views 100%, ratelimit 100%, import_service 95%).
- Mutation spot-checks: removing the advisory lock, the username filter, the title length limit or the `key` redaction rule each made the new tests fail.
- Browser measurement of the rendered pages (links, tables, inputs, overflow) was run manually with a throwaway script outside the repository.

## Final pre-acceptance adjustment: password-reset lifetime
Owner decision: reset links now expire after **3,600 seconds (1 hour)** (was Django's 3-day default). Regression tests added/updated in `accounts/tests/test_security.py`: exact one-hour boundary (valid at +3,590 s, invalid at +3,610 s; the old 3-day window is closed), an expired link cannot reset the password (original link and a link opened in time but submitted late), a used link cannot be reused (GET, POST, fresh client), unknown/known/throttled/expired/invalid cases are indistinguishable to the requester, the throttle is still 3 requests per 15 minutes, and a full flow leaves no address, uid, token, link or password in login events or audit records. Changing the setting back to 259,200 makes these tests fail.

## Final review conditions (owner-approved)
B45-B48 recorded in `OPEN_QUESTIONS.md` (import cost decimals, reserved `COV-C-` IDs, publication year range, recovery-code supersession). Corrections: `.env.owner.example` un-ignored; provisioning script no longer accepts passwords as variables (use interactive `\password`); conversion script refuses any database except `coven_library`; rollback restricted to immediately after conversion; `ALTER DEFAULT PRIVILEGES` documented as revoke-only; `.env.example` marked development-only.

## L6 database security hardening (two-role model)
Implemented per owner authorisation. Design, matrices, provisioning and recovery: `docs/DATABASE_SECURITY.md`. A fourth table class, READ_ONLY, was added for Django framework tables. The earlier "L6 unresolved" row is now resolved: TRUNCATE is never granted to the runtime role; members, editions and copies are no-delete; audit and copy-event tables are insert-only. Existing dev database converted (backed up, test-restored, ownership reassigned to `coven_owner`, `core.0002` applied). Verification: see the L6 report (266 tests OK as `coven_owner`; privilege checks as `coven_app`; coverage 91%). Windows 11 and clean-machine recovery are not verified.

## Phase 3 (3a-3d): subscriptions, finance, tokens, corrections
Implemented in the order 3a -> 3b -> 3c -> 3d; the full suite was run and passed before each next step. Left uncommitted for owner review.

| Sub-phase | Delivered |
|---|---|
| 3a | `memberships/policy.py` (all date rules), `SubscriptionPlan` (6 months Rs 800, 12 months Rs 1,500, seeded), 21 table-driven policy tests (approved examples, leap years, 31-day clamps, inclusive start / exclusive expiry, 25 April block, due cap, loan renewal) |
| 3b | `finance` ledger (transaction, payment entry, receipt document), four per-year receipt sequences, split cash/UPI exact settlement, backdating rules, request-key idempotency, append-only triggers, payment-sum deferred triggers, allocation (exact fractions), collections report |
| 3c | token account/cycle/ledger, `MemberSubscription` (exclusion constraint, guard trigger), atomic registration / renewal / fresh token / cancellation, member-page panel, receipts HTML + PDF |
| 3d | token refund (full only), forfeiture service (own record, no receipt), reversals, owner-approved adjustments, correction screens |

Results (PostgreSQL 16.15, Mac): `manage.py test` 436 tests OK (suite role `coven_owner`; privilege tests and the end-to-end subprocess run as `coven_app`); coverage 93%; ruff clean; `manage.py check` and `makemigrations --check` clean; `check_db_security` 0 errors / 0 warnings as `coven_app` and as `coven_owner` on the migrated development database. Not verified: Windows 11, clean-machine recovery. Owner confirmation requested for D-7 to D-14 (OPEN_QUESTIONS.md).

## Phase 3 remediation after the acceptance review
Owner decisions D-7 to D-14 applied; findings H1 to H7 reconciled. Left uncommitted for owner review.

| Finding / decision | Resolution |
|---|---|
| **H1** amount overflow (adjustment limit Rs 99,999,999.99 exceeded the 32-bit paise columns) | All eight paise columns widened to 64-bit (migrations `finance.0005`, `memberships.0005`, `core.0003`, `catalogue.0003`), each with a CHECK of at most 9,999,999,999 paise; services (`core.money.check_amount`), forms (`MAX_RUPEES`) and the catalogue add-copies path validate the same limit. The accepted limit was not lowered; the Excel unit-cost cap now follows it. Same overflow existed for book unit costs; fixed. |
| **H2** backdating missing from audit | `finance.services.audit_dates` adds backdated flag, effective date, processing timestamp, reason and owner confirmation to every finance/membership audit summary; registrations/renewals add the subscription start date, so a past start with a payment made today is audited. |
| **H3** silent truncation of reasons | `core.money.clean_reason` rejects text over 1,000 characters for every reason/note; forms enforce the same maximum. |
| **H5** anonymisation race | `anonymise_member` is atomic, locks the member row first and works on the fresh row; six PostgreSQL concurrency tests (mutation-checked). |
| **H4** system actor | Design documented only (TECHNICAL_ARCHITECTURE 19.1, OPEN_QUESTIONS D-15); Phase 4 forfeiture not implemented. |
| **D-7** | Money-out adjustments need an explicit "not a fee refund" confirmation and cannot reference a subscription payment; trigger `finance_txn_correction_rules` (migration `finance.0006`) enforces this and the no-reversal rule for subscription payments, token refunds and reversals, also for direct SQL. |
| **D-8 to D-13** | Confirmed; D-12 now covered by tests (refund allowed, no eligibility restored). D-14 recorded as an outstanding requirement. |
| H6, H7 (low) | Unchanged: the PDF still prints "Rs" (no rupee glyph in the built-in font) and has no logo; escaping verified. |

Results (PostgreSQL 16.15, Mac): `manage.py test` **487 tests OK at that step** (436 before; 51 added; the final total is in the last section; suite role `coven_owner`, privilege tests and subprocess flow as `coven_app`), coverage 93%; ruff clean; `manage.py check` and `makemigrations --check` clean; `check_db_security` 0 errors / 0 warnings as `coven_app` and as `coven_owner` on the migrated development database. One existing L6 restore test was made robust against a session-closing race (test change only). Weak areas: `members/views.py` 71%, `memberships/views.py` 88%, `finance/receipts.py` 89%; settings/wsgi modules are exercised only in subprocesses. Not verified: Windows 11, clean-machine recovery, a real browser.

## Decision documentation and read-only verification (after the remediation)
Documentation only; no application code, migration or database data was changed in this step.

- **D-15 APPROVED** (system actor for the automatic day-8 forfeiture; implementation deferred to Phase 4b) and **D-7 APPROVED in final form** (unlinked cash-out adjustments are permitted only as explicit owner-approved accounting corrections, with a mandatory reason and confirmation, never as fee refunds, and are to be shown separately in financial reports) are recorded in OPEN_QUESTIONS.md, PRD.md and TECHNICAL_ARCHITECTURE.md. **D-16** was first held PENDING and then DECIDED by the owner (see the next section).
- **Verified (read-only):** the five remediation migrations (SQL, order, reversal behaviour), all eight paise columns and their CHECK limits, preserved constraints/indexes/keys, the correction-rules trigger and function, the six append-only finance triggers, the privileges and classifications of `coven_app`, default privileges (none), function ownership and `search_path`; details in `docs/DATABASE_SECURITY.md`. Behavioural probe on a disposable database (all statements rolled back): reversals of subscription payments, refunds and reversals refused; money-out adjustments against a fee refused; money-in/no-cash adjustments and an unlinked cash-out adjustment accepted at database level; `coven_app` could not disable, drop, replace or alter the trigger.
- **Open points:** Phase 6 reports must separate unlinked cash-out adjustments (D-7 point 5); the migration reversal limit above; Windows 11, clean-machine recovery and real-browser checks remain unverified.

## D-16 final limits implemented
Owner decision D-16: maximum monetary amount **Rs 49,999.00** (4,999,900 paise), maximum reason/note length **2,000 characters**, maximum catalogue unit cost **Rs 49,999.00** (same shared limit). The 64-bit paise columns are kept (no narrowing, no architectural reason to change them).

- **Changed:** `core/money.py` (`MAX_PAISE`, `MAX_RUPEES`, `MAX_REASON_LENGTH`); because every service, form, import rule and model constraint reads these constants, validation, the forms (`max_value`, `max_length`) and the eight CHECK constraints all follow. New migrations replace the eight constraints (`finance.0007_limits_d16`, `memberships.0006_limits_d16`, `core.0004_limits_d16`, `catalogue.0004_limits_d16`); no column type changes and no hand-written reverse code (the generated operations reverse cleanly).
- **Tests:** boundary tests rewritten to the new values, plus literal-value tests (exactly 4,999,900 paise and 2,000 characters accepted; one paisa / one character more rejected in services, forms and by direct SQL; all eight constraint definitions contain 4999900 and none the old limit; columns still bigint). The earlier tests for amounts between the old 32-bit limit and the old maximum no longer apply and were replaced.
- **Side effects to note:** the Excel unit-cost cap is now Rs 49,999.00 (was Rs 21,474,836.47, then briefly the shared old maximum); the seeded plans (Rs 800, Rs 1,500) and the Rs 500 token are far below the limit.

## Final acceptance conditions (documentation, end-to-end and no-auto-renewal tests)
Three conditions from the final acceptance review were closed. No application code, behaviour, migration or approved rule changed.

- **Documentation reconciled:** TECHNICAL_ARCHITECTURE section 19 (decisions no longer "awaiting confirmation"), RISKS R-43 and R-46 (owner-decided), OPEN_QUESTIONS section 3 (now a D-7 to D-16 status table with implementation and test evidence), and the earlier test totals in this log.
- **End-to-end test** `memberships/tests/test_journey.py` (one PostgreSQL test): registration (split cash/UPI, combined printout) -> early renewal -> late renewal -> token forfeiture -> fresh token -> eligible full refund -> a further token collected and reversed -> owner-approved adjustment (recovered book, never a fee refund) -> anonymisation. It checks eligibility after every step, ledger integrity (entries equal amounts, token cycles balance to zero, 10 distinct UUIDs), gap-free per-year receipt sequences (`SUB` 2026/2027/2028, `TOK` x3, `REF`, `ADJ`; forfeiture and reversal without a receipt), subscriptions (contiguous early renewal, no overlap, snapshots), report categories, immutability, the ordered audit trail without personal data, owner attribution of every financial audit event, and the anonymised receipt page. Mutation-checked (disabling the forfeited-token renewal block fails it). The reversal step uses an unused second token because, under D-9, only the latest unused collection can be reversed after a refund.
- **No-automatic-renewal tests** `memberships/tests/test_no_auto_renewal.py` (10 tests): source scan (a subscription is created in one function, reachable only from `register_member` and `renew_subscription`, called only from the owner's views); no scheduler library, signal receiver or management command in `memberships`/`finance`; time passing, page views and every available command create nothing; renewal cannot run without an actor and a full payment. Mutation-checked (a stray creator, and an added management command, both fail it).

Results (PostgreSQL 16.15, Mac): `manage.py test` **503 tests OK** (492 before; 11 added; suite role `coven_owner`, privilege tests and the end-to-end subprocess as `coven_app`); coverage **93%** (3,264 statements, 224 missed; weakest: `members/views.py` 71%, `accounts/forms.py` 79%, `catalogue/views.py` 81%; settings/wsgi modules run only in subprocesses); ruff, `manage.py check` and `makemigrations --check` clean; `check_db_security` 0 errors / 0 warnings as `coven_app` and as `coven_owner`. Remaining limitations: Phase 6 separate listing of unlinked cash-out adjustments (D-7), D-14 plan editing, D-15 implementation (Phase 4b), the receipt PDF prints "Rs" without a logo, and Windows 11, clean-machine recovery and a real-browser check are unverified.

