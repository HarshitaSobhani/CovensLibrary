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
