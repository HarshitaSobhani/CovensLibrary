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
