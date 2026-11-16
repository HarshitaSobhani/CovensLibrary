# Engineering audit — findings register (November 2026)

Scope and limits: an audit of the repository at `f20f509` plus the uncommitted D-30 work. Automated
scans covered the whole tree; manual review was deepest in `catalogue` (import, editions) and
lighter elsewhere (views, authentication, ID sequences, loan issue). Finance, memberships,
communications, backups and the Windows scripts were NOT re-reviewed line by line; they rely on
their existing tests and earlier audits (`docs/SECURITY_AUDIT.md`). Nothing here was run on
Windows 11, on Python 3.12 (the sandbox had 3.13) or against real Google Drive.

Status: FIXED (regression test added), OPEN (owner decision), NOTED (no change).

| ID | Sev. | Category | Finding | Evidence / reproduction | Status |
|---|---|---|---|---|---|
| A-01 | Medium | Catalogue | An edition whose category is inactive could not be edited: the form only offered active categories, so saving failed with "Select a valid choice". A category can be created inactive and the import matches categories by name whatever their state, so such editions are reachable. | `test_edition_in_inactive_category_stays_editable` failed before the fix (200 instead of 302). | FIXED: the form offers active categories plus the edition's own; an inactive category still cannot be chosen for another edition. |
| A-02 | Low | Import / integrity | `set_options` saved the batch options and then revalidated rows with no transaction: a failure in between left new options with old row validation. | `test_set_options_is_atomic` failed before the fix (mode stayed "supplied"). | FIXED: `@transaction.atomic`; `test_failed_set_options_rolls_back_row_validation_too`. |
| A-03 | Medium | Import / integrity | Stale-preview detection compared only the NUMBER of valid rows. When one row became valid and another invalid, the import proceeded and wrote a row the owner had never seen as valid. | Reproduced against the original check: it imported row "A" (invalid in the preview). `test_stale_preview_detected_when_valid_rows_swap` failed before the fix. | FIXED: `preview_state` fingerprints every row (validity, cleaned values, errors, warnings); also `test_stale_preview_detected_when_a_valid_row_changes_outcome`. |
| A-04 | Medium | Import / workflow | "Confirm import" revalidated and imported in one step, so a catalogue change after the preview was shown was imported without the owner seeing the new preview (the service check above could not fire through the page). | `test_confirm_does_not_import_a_preview_the_owner_has_not_seen`. | FIXED: if the owner changed no option and the preview state differs, nothing is imported and the refreshed preview is shown. Superseded by owner decision O-02: see A-07. |
| A-05 | Low | UX / accessibility | Radio groups were rendered as full-width text inputs with no visible selected state in the dark theme; labels read "Copy id mode" and "Isbn"; the preview table had no Author column although Author is mandatory. | Browser check (Playwright, light and dark) for the radios; `test_preview_shows_author_and_labelled_controls`. | FIXED. |
| A-06 | Low | Tests | A leak check could match a short password inside ordinary words; the `pg_terminate_backend` probe targeted the test's own connection and killed it under a superuser; one test asserted exact HTML attribute order. | Suite run as a superuser: about 176 cascading "connection is closed" errors. | FIXED (earlier commit `f20f509` and this pass). |
| D-01 | Low | Documentation | `CLAUDE.md` "Status" called Phase 7 and the security passes "uncommitted", but they are committed (`a25031e`, `3ee6cb7`). `Claude_Code_Master_Prompt.md` still said the author may be blank. | `git log`. | FIXED (O-03): status split into committed / uncommitted / outstanding. |
| N-01 | Info | Repository | Three `.DS_Store` files are tracked although `.gitignore` lists the pattern. | `git ls-files`. | NOTED (owner asked not to touch them). |
| N-02 | Info | Performance | Import validation runs one ISBN-duplicate query per row with an ISBN (at most 2,000). Acceptable at this scale. | Code review. | NOTED. |
| N-03 | Info | Static analysis | Bandit: 23 Low, 0 Medium, 0 High (subprocess with argument lists, `try/except/pass` in cleanup paths, false "hardcoded password" matches on constants). pip-audit: no known vulnerabilities in `production.lock`, `base.txt`, `dev.txt`. | Scan output. | NOTED. |
| N-04 | Info | Windows | PowerShell scripts could not be parsed or run (no PowerShell here). | — | UNVERIFIED; run `deploy\\windows\\Test-Syntax.ps1` on the Dell. |

## Second pass (owner decisions O-01 to O-05 and a deeper review)

What "deeper" covered, honestly: `finance/services.py` and `finance/corrections.py` were read in
full; `memberships/policy.py` (first half) and the blockers/cancel parts of
`memberships/services.py`; `circulation/services.py` in full; the claim/attempt/retry part of
`communications/services.py`; `accounts` (views, recovery, rate limit, sessions) in full;
`backups/crypto.py` in full and the structure of `backups/restore.py`; the export table list in
`reports/export.py`; a scripted browser pass over 18 screens at phone width. NOT read line by
line: `backups/rotation.py`, `backups/services.py`, `backups/destinations.py`,
`backups/keystore.py`, `circulation/forfeiture.py`, `circulation/grace.py`,
`finance/allocation.py`, `finance/workspace*.py`, `members/services.py` (anonymisation),
`reports/queries.py`/`render.py`/`statement.py`, `core/dashboard.py`, the management commands and
every PowerShell script. Those rest on their existing tests.

| ID | Sev. | Category | Finding | Evidence / reproduction | Status |
|---|---|---|---|---|---|
| A-07 | Medium | Import / workflow | (O-02) Changing an option and pressing Confirm in one click imported a preview that had never been displayed. | `test_confirm_never_imports_an_unreviewed_preview` (catalogue change, option change, copy-ID mode change, missing or forged token). | FIXED: the preview page carries a token of the displayed state; Confirm with anything else regenerates the preview, imports nothing and asks for a separate confirmation. The service also accepts `reviewed_state` and refuses a mismatch. |
| A-08 | Low | Import | (O-05) "Book Title" was not recognised; and a repeated heading was resolved silently ("first non-empty cell wins", added in `f20f509`). | `test_book_title_heading_is_accepted`, `test_title_and_book_title_together_are_ambiguous`, `test_duplicate_column_is_rejected_not_guessed`. | FIXED: alias accepted; two columns mapping to one field are rejected with an error naming the columns. Unrecognised duplicate columns are still ignored. |
| C-02 | Low | Notifications | A batch of sends could outlive the 10-minute claim lease; another run could then claim and send the same notification (duplicate email). | Tests: `test_a_lapsed_lease_is_never_sent_by_the_stalled_worker`, `test_a_running_worker_renews_its_lease_so_no_one_else_can_claim`. | FIXED (replaces the earlier elapsed-time check, which was not enough): the lease of the rows still waiting is renewed before each send; `attempt` re-checks, immediately before SMTP, that its claim token is still ours and the lease still runs, and does not send otherwise. Residual risk: one SMTP call that stalls longer than the remaining lease (bounded by `EMAIL_TIMEOUT` per socket operation; a 10-minute lease and 15-second timeout leave wide margin). A crash after SMTP accepted but before the outcome is saved can still repeat the email: at-least-once delivery, recorded as such. |
| F-01 | Medium | Finance | A token collection could be reversed (₹500 paid back) while the member had a book out, although a token refund is blocked in that state. | Reproduced; `OutstandingLoanRulesTests`. Decided by the owner (O-07). | FIXED (O-07): blocked in the service with `loans_outstanding`; nothing changes. Reversal stays distinct from refund. Test `test_no_held_token_means_nothing_to_forfeit` rewritten to reach the same 'no token held' state by an owner-confirmed forfeiture instead (its purpose kept; the reversal path it used is now refused). |
| M-01 | Medium | Memberships | A subscription could be cancelled while a book was out under it. | Code review; `CancellationWithLoanTests`. Decided by the owner (O-06). | FIXED (O-06): `cancel_subscription` refuses with `loan_outstanding` under the member lock; loans, due dates, payments and audit untouched; no refund. Cancellation allowed after the return. |
| U-01 | Low | Accessibility | Every page had the same tab title and most screens had no heading element. | Scripted browser pass; `tests/test_page_headings.py`. | FIXED (O-08): each page has its own `<title>` (`Page - Coven's Library`) and exactly one `h1`. Signed in, the h1 is visually hidden and the top bar shows no page name; signed out, the top bar looks as before. Pages that showed their own visible h1 now use `h2.h1` so each screen has one h1. |
| N-05 | Info | Finance, circulation, auth, backup crypto | No defect found in the code read: member row locked first everywhere, idempotency keys, partial unique indexes behind loan issue, AES-GCM chunk format with authenticated header and final flag, single-use hashed recovery codes, limiter under advisory locks. | Code review plus the existing suites. | NOTED. |

## Not verifiable in this sandbox

- Windows 11: every PowerShell script, `RunHidden.vbs`, Task Scheduler registration, Windows Credential Manager (`keyring`), BitLocker verification, ProgramData ACLs, the PostgreSQL Windows service, the launcher and stop scripts, browser opening, hidden-process lifecycle. No PowerShell was available, so the scripts were not even parsed.
- macOS: the documented development setup itself (the suite ran on Linux).
- Python 3.12 (pinned): the suite ran on Python 3.13.
- Google Drive: the real adapter, OAuth consent, `drive.file` folder behaviour, resumable or partial uploads, retention against real remote listings.
- Real SMTP delivery.
- Recovery: no clean-machine restore, no restore from an older database on Windows; restore tests ran against disposable local databases only.
- Real key rotation (never performed; the tests use an in-memory keystore).
- The owner's real workbook and real library database (never touched).
- Browser checks covered structure only (labels, overflow, script errors) at one phone width; no screen reader, no contrast measurement.

## Owner decisions (all recorded in `OPEN_QUESTIONS.md`)

O-01 copy limits unchanged (200 manual, 100 per import row). O-02 Confirm never imports an unreviewed preview. O-03 `CLAUDE.md` status split. O-04 Title and Author mandatory for new editions. O-05 "Book Title" accepted; duplicates rejected. O-06 cancellation blocked while a book is out. O-07 token-collection reversal blocked while a book is out. O-08 per-page titles and one h1.

## Second pass, continued: the modules that were not read line by line

| Module | What was checked | Result |
|---|---|---|
| Backup rotation and key storage (`backups/rotation.py`, `keystore.py`) | `set_current`, `prune`, `import_key`, rotation state machine read in full | No defect found. The swap in `set_current` (save retired, replace working, delete retired) is not one transaction in the credential store: a failure between steps leaves the same key present twice, never a missing key. Noted, Low. |
| Backup services and retention (`backups/services.py`) | Fingerprints only in logs; no key material written | No defect found in the read. Retention is covered by `test_rotation`/`test_retention` in the suite. |
| Google Drive (`backups/destinations.py`) | Scope is `drive.file` only; app-created folder | Verified statically. NOT tested against Google. |
| Forfeiture and grace jobs (`circulation/forfeiture.py`, `grace.py`) | Locks: member, then setting, then loan, always in that order; the job re-reads status under the lock | No defect found. Race checked: a grace change that reads loans without locking can list a loan that is returned meanwhile; `confirm_pending` re-checks eligibility and existing forfeiture under locks, so nothing is forfeited wrongly. Benign. |
| Revenue allocation (`finance/allocation.py`) | Exact fractions, rounding only for display; no `float` in finance | No defect found. |
| Payments workspace (`finance/workspace*.py`) | Every state change is POST-only (scan) | No defect found in the scan. |
| Member anonymisation (`members/services.py`) | Member row locked first; name, mobile (replaced by a unique placeholder), email and notes cleared; status archived; hooks clear notifications and reasons | No defect found. Backups keep the old data until their retention ends (disclosed in the existing rules). |
| Reports (`reports/queries.py`, `export.py`, `render.py`) | `category` now nullable: the one place that used `category.name` was fixed in the D-30 work; exports use `category__name` (blank when none) | Fixed earlier (`reports/queries.py`). XLSX writes money as a float with two decimals: exact at this scale; Low. |
| Dashboard (`core/dashboard.py`) | Read only | Not re-read line by line. |
| Management commands | Destructive commands (rotation, import, restore, prune) require a typed confirmation | Verified by scan and the existing command tests. |
| Windows PowerShell | `Stop-CovensLibrary.ps1` ends only a process it has identified by PID, start time and command line; ACLs applied with `icacls` to the attestation folder only; no `Invoke-Expression` | Static reading only. Not parsed or run (no PowerShell here). |

## Not verifiable in this sandbox

- Windows 11: every PowerShell script (not parsed or run), `RunHidden.vbs`, Task Scheduler, Credential Manager (`keyring`), BitLocker check, ACLs, PostgreSQL Windows service, launcher and stop behaviour, browser opening.
- macOS: the development setup itself (the suite ran on Linux).
- Python 3.12 (pinned): the suite ran on Python 3.13.
- Google Drive: real OAuth, folder creation, uploads, partial uploads, remote retention.
- Real SMTP delivery and real mail-server stalls (simulated only).
- Recovery: no clean-machine restore; restore tests use disposable local databases.
- Real key rotation: never performed (in-memory keystore in tests).
- The owner's real workbook and real library database: never touched.
- Accessibility: structure checked; no screen reader, no contrast measurement, no visual comparison against the old screens.

## Still open

Nothing awaits a decision. Known residual risks: C-02 residual (a single SMTP call stalling beyond the remaining lease); set_current non-atomic swap (Low); XLSX money as floats (Low). Google Drive and Windows behaviour are unverified.

## Release-readiness review (final pass)

| ID | Sev. | Finding | Evidence | Status |
|---|---|---|---|---|
| R-01 | Info | `test_no_held_token_means_nothing_to_forfeit` was rewritten (O-07). The original reversed a token collection with a book out, which O-07 now forbids. The revised test reaches the same 'no token held' state by an owner-confirmed forfeiture and keeps the same day-8 assertions (no forfeiture, one token skipped). Lost coverage: the REVERSED-token state with an active loan is no longer reachable (a reversal needs no active loan, and borrowing needs a held token), so there is nothing for the day-8 job to see there. That borrowing blocks after a reversal is still covered by `test_reversing_an_unused_token_collection_pays_it_back_and_voids_it`. | Diff against `HEAD`; the existing test cited. | Reviewed; no separate test needed. |
| R-02 | Medium | Backup key rotation, `set_current`: a retry after an interruption could be refused ("not among the retired keys") when the working key was already the target and the crash came after the target's retired slot was deleted but before the index was rewritten. Injected failure at write 5. Nothing was lost, but the operator was blocked. | `backups/tests/test_set_current_faults.py`, failure at each of the five writes. | FIXED in `rotation.set_current`: if the target is already the working key, the retry finishes the cleanup of its retired copy and returns. Records the audit event with `resumed: true`. |
| R-03 | Low | `set_current` writes the keystore in several steps, not one transaction. The order keeps every key in at least one slot at every boundary (old key retained before the working slot is replaced; target's retired copy removed only after it is working). Verified by injecting a failure at every write: no key was lost in any case, and a healthy retry completes the swap. A crash after the store change but before the audit row leaves the store changed without an audit record. | Failure-injection test (every boundary) plus the duplicate-slot test. | Verified; audit gap noted, Low. The keystore's single-item `set` must be atomic on Windows Credential Manager: UNVERIFIED on Windows. |
| R-04 | Low | `set_current` requires the target to be among the retired keys but does not check it opens the library's newest backup. An owner who types the confirmation can make a wrong retained key current. Old backups stay openable (nothing is deleted), but new backups would use the wrong key. | Code read; owner-confirmed command by design. | Noted; no approved rule requires a check. |
| R-05 | Info | Notifications: the claim token and lease are checked immediately before SMTP (`attempt`, filtered by `pk`, `claim_token` and `claimed_until > now`), leases are renewed before each send (`send_due`), and the outcome is recorded only if the token still matches. Concurrency: two workers cannot claim the same unexpired row (`select_for_update(skip_locked=True)` plus the lease filter); tested by `test_a_lapsed_lease_is_never_sent_by_the_stalled_worker` and `test_a_running_worker_renews_its_lease_so_no_one_else_can_claim`. | Code read and tests. | Verified in tests with a single process and a mocked clock; NOT verified with real parallel workers on Windows. |
| R-06 | Medium (delivery semantics) | **Delivery is at-least-once, not exactly-once.** If the process crashes after the mail server accepts a message but before the attempt is saved, the row still looks due and the same email is sent again after the lease expires. The application records "SMTP accepted", never "delivered". A residual SMTP call that stalls longer than the remaining lease is the other path to a repeat. Nothing in this design gives exactly-once delivery over SMTP. | Reasoned from `attempt`/`send_due`. | Documented here. Owner should accept at-least-once. |
| R-07 | Low | Financial XLSX output. Database and calculations stay in integer paise (`BigInteger` columns and CHECKs). The spreadsheet boundary converts paise to rupees with `to_decimal` (exact `Fraction`, half-up to 0.01), then to a float cell. Whole-paise amounts convert exactly (checked for 0 to Rs 49,999.99); fractional paise from allocation are rounded half-up for display only. Excel sums of those displayed cells can differ from the stored total by a few paise: display rounding, not a stored value. | `reports/tests/test_xlsx_money.py` (5 boundary tests). | Verified by tests. Behaviour unchanged; the rounding rule is the existing display rule. |
| R-08 | Info | Migration `catalogue.0006_edition_category_optional` alters `BookEdition.category` to nullable. Justified by D-30 ("Category is optional"): a book with no category must be storable. No data is changed; existing rows keep their category. | `makemigrations --check` clean after it. | Justified; ships with D-30. |

### Diff review (73 entries: 68 modified, 5 untracked)

- **Business rules and decisions:** `OPEN_QUESTIONS.md`, `PRD.md`, `CLAUDE.md`, `Claude_Code_Master_Prompt.md`, `docs/ERD.md`, `IMPLEMENTATION_PLAN.md`: the D-30, O-01 to O-08 decisions and the status split. Required.
- **Catalogue and import:** `catalogue/forms.py`, `import_service.py`, `import_views.py`, `models.py`, `services.py`, `views.py`, `migrations/0006`: D-30, A-01 to A-04, A-08, O-02, O-05. Required.
- **Finance and circulation:** `finance/corrections.py` (O-07), `memberships/services.py` (O-06), `reports/queries.py` (nullable category): required by the owner decisions.
- **Communications:** `communications/services.py` (C-02). Required.
- **Backups:** `backups/rotation.py` (R-02 fix). Required.
- **Templates and CSS:** `base.html` (single h1 and visually hidden heading when signed in), 37 templates (a `title` block added beside each `heading` block, plus a few visible `h1` demoted to `h2.h1`), and the empty-diff `app.css`. Required by O-08; nothing else changed on those pages.
- **Tests:** several test files updated for the new requirements (author required, Confirm token, O-07 rewrite), plus new tests: `tests/test_page_headings.py`, `backups/tests/test_set_current_faults.py`, `reports/tests/test_xlsx_money.py`, `core/tests/secret_checks.py`. Required.
- **Docs:** `docs/ENGINEERING_AUDIT.md` (new), `docs/IMPLEMENTATION_LOG.md`, `docs/ERD.md`. Required.
- **Unnecessary changes found:** none beyond the stray blank line in `app.css`, which is now removed. The earlier `CLAUDE.md` status rewrite was requested (O-03).

### Hygiene checks

- Staged files: 0. Nothing has been committed or pushed.
- Tracked `.DS_Store` files modified: 0.
- `.env` and `.env.owner`: untracked and git-ignored (`.gitignore` lines 6 and 7). Not staged. Left in place (they hold sandbox credentials written in this session; you may delete them).
- Secret-pattern scan over added lines: no matches outside test fixtures. Test passwords are fixtures only.
- Migrations: one new (`0006`), justified in R-08.
- No production data was touched. No real key rotation, no real Drive call, no real import, no destructive restore.

### Verification (two-role PostgreSQL configuration)

| Check | Result |
|---|---|
| Full suite, `manage.py test --settings=config.settings.test` | 1397 tests collected and run; OK; 0 skipped |
| `ruff check .` | All checks passed |
| `ruff format --check .` | 276 files formatted |
| `manage.py check` | No issues |
| `makemigrations --check --dry-run` | No changes |
| `check_db_security`, owner role | 0 errors, 0 warnings |
| `check_db_security`, runtime role | 0 errors, 0 warnings |
| Bandit (non-test code) | 23 Low, 0 Medium, 0 High |
| pip-audit on `requirements/production.lock` | No known vulnerabilities |

### Still environment-dependent (not verified here)

- Windows 11: all PowerShell scripts (not parsed or run); Credential Manager behaviour and atomicity of a single-item write (R-03); ACLs; Task Scheduler; BitLocker; the PostgreSQL service; launcher and stop.
- Google Drive: OAuth, folder creation, uploads, partial uploads, remote retention.
- Real SMTP: delivery, stalls, and the duplicate on crash-after-acceptance (R-06).
- Real parallel workers (R-05 tested with a single process and a mocked clock).
- Recovery: clean-machine restore not performed.
- macOS and Python 3.12: the suite ran on Linux and Python 3.13.
- Browser: structure only; no screen reader, contrast or visual comparison.
