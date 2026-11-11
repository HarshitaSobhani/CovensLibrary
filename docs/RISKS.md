# RISKS — Coven's Library

Likelihood (L) / Impact (I): H/M/L. All risks open (no implementation exists). Rule references use `OPEN_QUESTIONS.md` identifiers.

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
| R-24 | **Login rate limiting** (5 failures/15 min) causes owner friction or is bypassed; no lockout by decision. | L | M | Success resets counter; failures audit-logged; localhost-only binding. |
| R-25 | **Password reset depends on SMTP**; fallback = local CLI + recovery code (single-use, hashed, shown once, 15-minute expiry — an owner who misses the window must regenerate it). | M | H | Test-email action; recovery procedure documented; code never logged. |
| R-26 | **SMTP acceptance ≠ delivery**; automatic emails sent wrongly, repeatedly or to wrong address; retry storms (retries 5 min after the initial failure, 30 min after retry 1, 2 h after retry 2; then failed, no indefinite retry) or missed 09:00 reminders when the Dell is off. | H | M | Statuses never say delivered; one notification record per event with dedupe key; retries reuse it; missing email recorded as skipped with reason; failed emails on dashboard; catch-up at next launch; credentials in environment only. |
| R-27 | **WhatsApp link misuse / bad numbers.** | L | M | Never automatic; E.164 validation; URL-encoding; restricted placeholders. |
| R-28 | **Anonymisation vs retained backups**: old backups keep personal data until retention expiry. | M | M | Disclose in app and docs; retention limits exposure. |
| R-29 | **Duplicate ISBNs, duplicate or conflicting copy IDs on import.** | M | M | Preview, ISBN warnings, per-import copy-ID mode, conflict detection, atomic import, error report. |
| R-30 | **CSV/Excel formula injection** in exports. | M | M | Escape leading `= + - @`. |
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
