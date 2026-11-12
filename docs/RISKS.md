# RISKS — Coven's Library

Likelihood (L) / Impact (I): H/M/L. Phases 1a-2 and L6 are implemented; risks below stay open until mitigations are verified in the relevant phase. Rule references use `OPEN_QUESTIONS.md` identifiers.

## Backup, recovery, environment
| ID | Risk | L | I | Mitigation |
|---|---|---|---|---|
| R-01 | **Dell is a single point of failure** (disk death, theft, spill). | M | H | Daily encrypted Drive backup; replacement-computer procedure; real clean-machine restore test. |
| R-02 | **Stale or failed backups**; Dell asleep/off at backup time. | H | H | Fixed time + catch-up at next launch; dashboard warning after 26 h; failure visible; retry. |
| R-03 | **Recovery-key loss** → backups unrecoverable. | M | H | Owner stores recovery key separately and offline; never beside backups; operator doc states loss = no recovery; key fingerprint check at restore. |
| R-04 | **Windows Credential Manager dependency**: working key tied to the Windows user account; profile loss, account change or OS reinstall loses it; entries do not transfer to a new machine. | M | H | Recovery key restores the key into the new machine's Credential Manager after owner confirmation; rehearse; document. |
| R-05 | **Replacement-machine recovery unverified.** | M | H | Not claimed verified until a clean-machine restore passes; periodic rehearsal; PostgreSQL major version match recorded in manifest. |
| R-06 | **Google OAuth access changes** (consent revoked, scope/policy changes); replacement computer needs fresh consent. | M | M | `drive.file` scope; re-consent steps documented; backup-health warning; local backup copy option considered later. |
| R-07 | **Retention bug deletes monthly backups.** | M | H | Separate classes; fake-clock tests; never delete newest successful backup. |
| R-08 | **Windows 11 runtime specifics** (paths, line endings, `pg_dump` on PATH, Task Scheduler missed runs, PostgreSQL service startup, unverified edition/build); Mac-dev vs Windows-runtime drift. | M | M | OS-neutral code; pure-Python deps; separate dev/runtime docs; restore rehearsal on Windows 11; record edition/build when verified. |
| R-09 | **Local hosting limits**: no HTTPS, shared laptop, sleep, updates. | M | M | Localhost-only binding; strong owner password; disk encryption advice; local-vs-cloud memo before launch. |
| R-10 | **Scope creep** (reservations, portal, gateway, offline). | M | M | Non-goals in CLAUDE.md. |

## Dates, rules, finance
| ID | Risk | L | I | Mitigation |
|---|---|---|---|---|
| R-11 | **Date boundary errors**: first expired day vs last valid day; due date landing on the first expired day; 25 April block boundary; inclusive/exclusive ends; Asia/Kolkata date handling. | H | H | Single `policy.py`; table-driven tests from confirmed examples; no date arithmetic elsewhere. |
| R-12 | **Month-end/leap-year arithmetic** (31 Aug, 31 Jan, 29 Feb). | M | H | Calendar-month add with clamp; tests for confirmed examples and further leap cases. |
| R-13 | **Financial ledger inconsistency**: double refund/forfeit, wrong balance, race conditions, duplicate numbers. | L | H | Partial unique per token cycle, row locks, append-only triggers, atomic transactions, concurrency tests. |
| R-14 | **Append-only enforced only in application code.** | M | M | PROTECT FKs, no edit views, DB triggers blocking UPDATE/DELETE. |
| R-15 | **Automatic overdue forfeiture wrongly posted** (boundary day 8, backdated return, setting error). | M | H | Show grace end and forfeiture status on every loan; boundary tests; audit; reversal/adjustment path. |
| R-16 | **Delayed processing when Dell is off**: forfeiture/emails late; wrong effective date. | H | M | Process at next launch with effective date = day 8 and separate `processed_at`; tests. |
| R-17 | **Grace-period change affects existing loans** and may newly trigger forfeitures. | M | H | Owner confirmation for each newly triggered forfeiture; record old/new setting and affected loans; a declined forfeiture is not posted, needs a reason, is audited and is not re-prompted (risk: forfeitable loans silently left unforfeited — visible in audit and dashboard). |
| R-18 | **Damaged/Poor-condition forfeitures** disputed or mis-classified (Poor/Damaged boundary is owner judgement). | M | M | Explicit owner confirmation, reason, audit; clear on-screen consequence before confirming. |
| R-19 | **Lost-book recovery**: forfeiture stays; owner may forget the separate adjustment. | M | M | Prompt owner when a lost copy is found/returned; adjustment workflow with audit; no auto-reversal. |
| R-20 | **Forfeited token consequences** (B15/B16/Q1): member confusion — existing subscription stays valid but new borrowing and renewal are blocked until a fresh ₹500 is paid; owner may confuse this with member status "suspended". | M | M | Distinct derived label; clear UI message; fresh token starts a new cycle; audit. |
| R-21 | **Backdated financial operations** distort reports or hide errors. | M | M | Mandatory reason, audit, extra financial confirmation; event/effective/processing dates separate. |
| R-22 | **Revenue allocation misread as accounting**; rounding drift. | M | M | Label as management view; full precision, round at display only. |

## Auth, comms, data
| ID | Risk | L | I | Mitigation |
|---|---|---|---|---|
| R-23 | **Daily authentication/session handling**: sessions ending at midnight Asia/Kolkata mid-work; timezone mistakes. | M | M | Session expiry computed in library timezone; warn before expiry; unlimited re-login. |
| R-24 | **Login rate limiting** (5 failures/15 min) causes owner friction or is bypassed; no lockout by decision. | L | M | Success resets counter; failures audit-logged; localhost-only binding; concurrency-safe via advisory locks (tested with 24 simultaneous attempts). |
| R-25a | **Historical plaintext passwords** in audit/login records written by pre-remediation code (append-only, cannot be removed by the application). | L | H | None exist in the development database (0 rows checked); any other environment that ran the earlier code needs an owner-led response (change those passwords). Decision D-3 in OPEN_QUESTIONS.md. |
| R-25 | **Password reset depends on SMTP**; fallback = local CLI + recovery code (single-use, hashed, shown once, 15-minute expiry — an owner who misses the window must regenerate it). | M | H | Test-email action; recovery procedure documented; code never logged. |
| R-26 | **SMTP acceptance ≠ delivery**; automatic emails sent wrongly, repeatedly or to wrong address; retry storms (retries 5 min after the initial failure, 30 min after retry 1, 2 h after retry 2; then failed, no indefinite retry) or missed 09:00 reminders when the Dell is off. | H | M | Statuses never say delivered; one notification record per event with dedupe key; retries reuse it; missing email recorded as skipped with reason; failed emails on dashboard; catch-up at next launch; credentials in environment only. |
| R-27 | **WhatsApp link misuse / bad numbers.** | L | M | Never automatic; E.164 validation; URL-encoding; restricted placeholders. |
| R-28 | **Anonymisation vs retained backups**: old backups keep personal data until retention expiry. | M | M | Disclose in app and docs; retention limits exposure. |
| R-29 | **Duplicate ISBNs, duplicate or conflicting copy IDs on import.** | M | M | Preview, ISBN warnings, per-import copy-ID mode, conflict detection, atomic import, error report. |
| R-30 | ~~CSV/Excel formula injection in exports~~ **Mitigated (Phase 6):** every text cell in CSV and XLSX is escaped (leading `= + - @`, tab, CR) and typed as a string; tested for reports, statements and the full export. | M | M | `reports/render.py`, tests in `reports/tests`. |
| R-31 | **PII exposure** via logs, backups, exports. | M | M | No PII in logs/audit summaries; encrypted backups; owner-only access. |

## Design and delivery
| ID | Risk | L | I | Mitigation |
|---|---|---|---|---|
| R-32 | **Brand asset quality** (404×192 px wordmark; angled sticker photos). | H | L | Request clean vector/transparent files; provisional derived assets only; no fake font. |
| R-33 | **Palette drift** from references. | M | L | Tokens only; contrast measured; owner approves. |
| R-34 | **Dependency creep/maintenance** (keyring — approved, SortableJS, whitenoise…). | M | L | Each justified in ARCHITECTURE §14; no Docker/Celery/Redis. |
| R-35 | **Django/Python version drift.** | L | M | Verify and pin at Phase 1. |
| R-36 | **Repo hygiene**: `.DS_Store` tracked, no `.gitignore`. | H | L | Phase 1a with approval. |
| R-37 | **Documentation drift**: obsolete rules followed by future sessions. | M | H | Single authoritative register; CLAUDE.md authority order; consistency review per phase. |

## Database security (L6)
| ID | Risk | L | I | Mitigation |
|---|---|---|---|---|
| R-38 | Superuser or OS administrator bypasses all database protections; `.env.owner` is plaintext on the dev Mac. | L | H | `postgres` break-glass only; 0600 git-ignored owner file; owner password absent from the Dell except at upgrades; `docs/DATABASE_SECURITY.md`. |
| R-39 | New table left unclassified or grants forgotten after a migration. | M | M | Fail closed (`coven.E001`); post-migrate grant hook; start-up assertion; tests for registry completeness. |
| R-40 | Owner role can drop append-only triggers. | L | M | Owner used only for migrations; runtime cannot alter them; `check_db_security` verifies triggers. |
| R-41 | Deferred payment-sum trigger cost grows with the number of payment entries of one transaction (quadratic); thousands of entries per transaction would be slow. | L | L | A transaction has a handful of cash/UPI entries; the form allows two. |
| R-42 | Receipt snapshots keep amounts, dates, references and member ID permanently (append-only), so receipts survive anonymisation and old backups. | M | L | Name is shown live, not stored; disclosed with anonymisation (R-28). |
| R-43 | Reversal/adjustment limits (D-7 to D-9) may not cover every real correction the owner meets. | M | M | **Owner-decided (D-7 to D-9).** The owner-approved adjustment is the general correction mechanism; unlinked cash-out adjustments are permitted with a mandatory reason and explicit confirmation. Residual: anything outside these rules needs a new owner decision. |
| R-44 | Phase 4 must register the outstanding-loan check and call the forfeiture/borrowing helpers; until then refunds cannot see loans (none exist yet). | M | M | `finance.corrections.OUTSTANDING_LOAN_CHECKS` and `memberships.services.borrowing_blockers` are tested hooks; Phase 4 tests must cover them. |
| R-45 | **System actor not yet modelled**: the automatic day-8 forfeiture (Phase 4b) cannot be recorded without an actor until D-15 is implemented. | M | M | Design in TECHNICAL_ARCHITECTURE 19.1 and OPEN_QUESTIONS D-15; decided and tested in Phase 4b. |
| R-46 | An unlinked cash-out adjustment could be used to pay a subscription fee back; the database cannot see the owner's confirmation. | L | M | **Accepted by the owner (D-7 final).** Mandatory reason, explicit "not a fee refund" confirmation, owner attribution, timestamp, amount and audit event; adjustments may not reference subscription payments (service and trigger). Phase 6 must list unlinked cash-out adjustments separately. |
| R-47 | The owner role and superuser can still TRUNCATE ledger tables (accepted L6 decision); only the runtime role is denied. | L | H | Owner credentials only during upgrades (D-5); backups. |
| R-48 | The daily job only runs when someone runs it (Task Scheduler / launch wiring is Phase 7c); until then an overdue forfeiture is posted late. | M | M | Late posting keeps the original effective date (N6); the command is idempotent; wiring and a "job last ran" health check in Phase 7c / 5a. |
| R-49 | ~~Notification events were not persisted~~ **Resolved (D-23):** events are stored in `core_domainevent` in the business transaction (tests: commit, rollback, failure-after-insert, dedupe, privileges). Key reuse with a different body raises `OutboxKeyConflict` (never silently dropped). Residual: Phase 5b must consume them idempotently (concurrent workers, claiming) and never delete them. | L | M | Phase 5b contract in TECHNICAL_ARCHITECTURE 20; unconsumed = no notification record. |
| R-50 | D-17 to D-24 are formally confirmed by the owner; residual risk is only implementation error. | L | L | Covered by tests; not to be reinterpreted. |
| R-51 | Retries and reminders only run when a cycle runs (after an owner action, `run_daily_jobs`, "Process now"). Until Phase 7c schedules the job unattended, a retry can be late, and a reminder missed for more than 7 days is not recovered (D-27a). | M | M | Documented; back-fill for 7 days; dashboard shows failed emails; Phase 7c wires Task Scheduler and launch catch-up. |

| R-51 | Retries and reminders only run when a cycle runs (after an owner action, `run_daily_jobs`, "Process now"). Until Phase 7c schedules the job unattended, a retry or reminder can be late or, for a day nothing ran, missing (D-26d, D-26f). | M | M | Documented; dashboard shows failed emails; Phase 7c wires Task Scheduler and launch catch-up; owner to decide back-fill (D-26d). |
| R-52 | At-least-once sending: a crash after SMTP accepted but before the result is saved can send the same email again after the 10-minute lease (D-26g). | L | L | Disclosed; unavoidable without SMTP idempotency; idempotent record keys otherwise prevent duplicates. |
| R-53 | ~~Accepted emails kept the address and text after anonymisation~~ **Resolved (D-27b):** content erased at anonymisation, address erased 90 days later. Residual: a message in flight at the moment of anonymisation, and backups taken earlier (disclosed). | L | L | Tested, including as the runtime role. |
| R-54 | A member's email is a single address with no verification; a typo means silent non-delivery that SMTP acceptance cannot reveal. | M | L | Statuses never say delivered; the owner can send WhatsApp manually. |
| R-55 | The 90-day address retention is database-assisted, not tamper-proof: the runtime role can update `members_member.anonymised_at`, which the trigger uses as the clock (D-27, accepted limitation). | L | L | Documented in DATABASE_SECURITY; early erasure never exposes data; audited anonymisation; owner-only application. |
| R-56 | Applying Phase 5 to a database that already holds notifications could lose attempt addresses (migration 0003) or leave historic anonymised records unredacted. | L | M | Migration refuses on real data without an override; `preflight_notifications --apply` redacts and verifies; steps in DATABASE_SECURITY "Phase 5 deployment safeguards". Reversing `0003` fails with a NotNullViolation when attempt rows exist and leaves 0003 applied / 0004 unapplied (older guard function active); with no attempt rows it reverses. A refused `migrate` may already have applied other apps' pending migrations. |
| R-57 | Exports contain personal data (names, mobiles, emails of live members) by design; a downloaded file leaves the application's protection. | M | M | Owner-only, audited, generated on request and never stored on the server, `Cache-Control: no-store`; anonymised members are exported anonymised; message texts and addresses are excluded; the owner is reminded to store downloads safely. |
| R-58 | Free text typed by the owner (reasons, notes) can contain a member's name; anonymisation does not rewrite immutable records. | M | M | Masked for anonymised members in all reports and exports (D-28i); originals stay only in owner-only operational views; backups are disclosed separately (R-28). |
| R-59 | **Decided policy (owner):** the original payment reference stays on historical receipts, including receipts of anonymised members. Receipt pages and receipt PDFs are reachable only by the authenticated owner (unauthenticated requests redirect to the login page; tested). A payment reference is owner-entered free text and may contain personal information. Anonymisation therefore does NOT completely remove personal information from historical financial documents. Payment references stay masked (`Redacted \u2014 member anonymised`) in every report, statement and general export. | L | L | Receipts are immutable records; access control and masking above; permanent tests for receipt authentication, receipt content after anonymisation and uncompressed-PDF masking. |
