# OPEN_QUESTIONS.md — Coven's Library

Status: documentation phase. Section 1 is the **decision register** (every owner-confirmed answer, identifiers preserved). Section 2 lists only **genuinely unresolved details**. No unresolved item may be decided silently in code or documents.

All 44 original decisions (A1, A2, B1–B44), all 13 clarification answers (B35 final form, N1–N12) the specification answers Q1–Q8 (including Q7/Q7b receipt year and the Q8 email schedule and retry policy), the dashboard defaults and the `keyring` approval are recorded below as confirmed. Where a later answer refines an earlier one, the refined form is the only current rule.

---

# 1. Decision register (CONFIRMED)

## 1.1 Dates, subscriptions, plans
| ID | Decision |
|---|---|
| A1 | Subscription start date is inclusive. Expiry date = **first expired day**. Last valid day = the day before expiry. Active through the last valid day; expired from the first expired day. Example: start 11 Nov 2026, 6 months → last valid 10 May 2027, first expired 11 May 2027. |
| B1 | Expiry = start + N **calendar months**; if the start day does not exist in the target month, clamp to that month's last day. 31 Aug 2026 +6 → first expired 28 Feb 2027, last valid 27 Feb 2027. 31 Jan 2027 +1 → 28 Feb 2027 / 27 Feb 2027. 29 Feb 2028 +12 → 28 Feb 2029 / 27 Feb 2029. Calendar-month arithmetic, not fixed days. |
| B5 | At most one current subscription plus one scheduled future subscription. |
| B6 | A late renewal starts on the owner-entered activation date (entered explicitly). |
| B7 | **Subscription fees are strictly non-refundable**, including early cancellation, unused periods and prorated amounts. No credit. No subscription-fee refund transaction or workflow. |
| B8 | Subscription-period revenue allocation is **daily pro-rata** over valid subscription days; payment-date collections are a separate view. |
| N9 | Allocation is computed at **full precision**; rounding happens only when displaying reports. Per-day rounded amounts are never persisted in a way that causes cumulative discrepancies. |
Plans: ₹800 / 6 months, ₹1,500 / 12 months, same benefits, no automatic renewal, fully paid before activation.

## 1.2 Loans
| ID | Decision |
|---|---|
| A2 | A loan renewal must not produce a due date later than the subscription last valid day; if membership must be renewed first, the owner renews membership first. Never silently extend. |
| B2 | A loan due date (issue or renewal) must be on or before the last valid day. The first expired day is never allowed. |
| B3 | New loans are blocked starting 15 full calendar days before the last valid day (`issue_date >= last_valid_day − 15`). Example: last valid 10 May → blocked from 25 April. |
| B4 | The final-15-day restriction and the due-date cap are **independent**; both are enforced. |
| B14 | Condition (Good, Poor, Damaged, Lost) and circulation status (Available, Borrowed, Damaged, Lost, Withdrawn) are separate fields. Issuable only if condition Good **and** status Available. A Poor copy is always Withdrawn (Q6). |
| B18 | **No loan renewal once the due date has passed**; grace gives no renewal. Membership must be valid through the resulting due date. Otherwise unlimited owner-recorded renewals of +30 days. |
Also confirmed: one active loan per member, one copy per loan, default 30 days.

## 1.3 Overdue, grace, token forfeiture
| ID | Decision |
|---|---|
| B9 | Grace = 7 calendar days after the due date (due 10 May → grace 11–17 May → day 8 = 18 May). On day 8 the full ₹500 is **forfeited automatically**, no owner confirmation; ledger + audit record. |
| B10 | A return on day 8 does not prevent forfeiture. |
| B11 | **Lost:** owner marks Lost, confirms full forfeiture, mandatory reason, audited; loan closes with Lost outcome; copy status Lost. No replacement charge. |
| B12 | A later-recovered lost book does **not** reverse the forfeiture; any refund/correction is a separate owner-approved adjustment (ledger + audit). |
| B13 / N5 / Q6 | **Poor-condition return:** owner classifies the copy Poor and **confirms** full ₹500 forfeiture with a **mandatory reason**; audited. The copy's condition is `Poor` and its circulation status is **`Withdrawn`** (removed from circulation, unavailable). Poor and Damaged remain separate classifications. |
| (damaged) | **Damaged return:** owner classifies Damaged, confirms full forfeiture, mandatory reason, audited; copy removed from circulation. |
| B17 | Grace-period setting changes apply to existing loans (no per-loan snapshot). |
| N11 / Q5 | **Safeguard:** if a grace-setting change makes an existing loan *newly* eligible for forfeiture, that forfeiture requires **owner confirmation** (distinct from normal automatic day-8 processing). Record previous setting, new setting, affected loans, newly eligible forfeitures, owner decision, resulting transactions. If the owner **declines**, the forfeiture is **not posted**; a **mandatory reason** and audit record are required; the owner is **not prompted again for the same event**, and the automatic day-8 job does not post it afterwards. |
| N6 | If the Dell was off when a forfeiture fell due, process it at the **next application launch**; **effective date remains day 8**; the actual processing timestamp is also recorded. |
| B15 | A returning member keeps the **same permanent member ID** and pays a **fresh ₹500 token**, which begins a new token cycle. |
| B16 / Q1 | Membership **renewal** is blocked while the member's current token cycle is forfeited. After forfeiture the **existing subscription remains valid**, but **new borrowing is suspended** until a fresh ₹500 token is paid; paying it starts a new token cycle and restores borrowing. Existing loans, returns and renewals of already-issued loans follow their own rules. |
| N2 | Reconciling B15/B16/Q1: after forfeiture the member **cannot renew**, keeps their still-valid subscription, **cannot borrow new books**, and may pay a fresh ₹500 token (same member ID, new token cycle) to resume borrowing and to become eligible to renew. The old forfeiture remains in the ledger; the old token is never restored. |

## 1.4 Finance
| ID | Decision |
|---|---|
| B19 / N12 | The owner may enter **any past** issue/return date. Every backdated operation needs a **mandatory reason** and audit entry; if the date affects financial transactions, **additional owner confirmation** is required. Event date, financial effective date and processing timestamp are kept distinct. |
| B20 / N3 / Q2 | Every financial transaction has a **globally unique internal transaction ID (UUID)**. **Separate human-readable receipt sequences, each numbered per year**: subscription `SUB-YYYY-000001`, token collection `TOK-YYYY-000001`, token refund `REF-YYYY-000001`, adjustment `ADJ-YYYY-000001`. Member ID `COV-M-000001`; copy ID `COV-C-000001` (barcode value = copy ID). Forfeitures and reversals create their own immutable transaction records with a UUID and a link to the original; no customer receipt is defined for them. A **combined registration printout must show both** the subscription receipt and the token-collection receipt. `YYYY` = **calendar year of the receipt issuance date** (Q7/Q7b, confirmed): a backdated payment date never changes the year; e.g. effective date 31 Dec 2026, issued 5 Jan 2027 → `SUB-2027-…`. |
| B49 | **Corrections.** Subscription payments and paid token refunds cannot be reversed. Corrections use owner-approved adjustments, which are never a way to refund a subscription fee (D-7). |
| B50 | **Renewal** requires a held token; a refunded or reversed token does not satisfy this (D-8). Only the latest token cycle can be corrected (D-9). |
| B51 | A subscription start date before today is a backdated operation: reason plus extra owner confirmation (D-10). |
| B52 | Receipts retain the member ID (not the name) and UPI references; after anonymisation a full token refund is allowed but borrowing and renewal never return (D-11, D-12). |
| B21 / N8 | Monthly collections show all receipts with separate breakdown (subscription fees, token collections, other); tokens never counted as subscription revenue. |
Also confirmed: split cash/UPI payments settle one obligation (full amount remains due until completely paid; activation blocked until paid); immutable ledger (reversal/adjustment only); token ledger separate from revenue; PDF + print receipts.

## 1.5 Members and catalogue
| ID | Decision |
|---|---|
| B22 | Subscription dates determine subscription eligibility; owner-managed status (active/suspended/archived) can suspend/archive independently. |
| B23 | Reactivation: owner chooses restore previous arrangement or require a new subscription. |
| B24 | Editions and physical copies tracked separately; dashboard shows both counts. |
| B25 | Per import, owner chooses: create missing categories, or reject affected rows. |
| B26 | Per import, owner chooses: supplied/legacy copy IDs, or generate; conflicts and duplicate copy IDs prevented. |
| B27 | Author may be blank or explicit "Unknown author". |
| B28 | Duplicate ISBN → warning; owner decides continue/cancel; no auto-merge. |
| B29 | Code 128 barcodes from copy IDs; configurable label dimensions/layout; printable sheets. |
| B31 | International numbers, country-code selection, E.164, mandatory and unique. |
| B42 | Anonymisation changes live data only; historical backups unchanged until retention expiry; the application discloses this. |

## 1.6 Communications
| ID | Decision |
|---|---|
| B30 / N4 / Q3 | **Email is automatic via SMTP.** *Immediate event notifications:* subscription activation, payment receipt, loan issue, loan renewal, book return, token forfeiture. *Scheduled reminders:* upcoming subscription expiry, subscription expiry notice, overdue reminders. **Up to three retries** after SMTP failure; **duplicate prevention**; **missing-email tracking** (members without an email address are recorded, not silently skipped). Statuses: Prepared, SMTP accepted, SMTP failed (acceptance ≠ delivery/reading). **WhatsApp is never automatic:** prefilled message to the selected member's number; owner reviews and sends. (Exact schedule: Q8 below.) |

## 1.7 Authentication and security
| ID | Decision |
|---|---|
| B33 / N10 / Q4 | Email-based password reset. If SMTP is unavailable: **local CLI recovery** with a **recovery code** that is **single-use, stored hashed, displayed once, and expires after 15 minutes**. |
| B34 / N1 | The owner should authenticate at least once each day; sessions expire at end of day (Asia/Kolkata). **No limit on logins per day**: logout, a lost session or an expired session never prevents another login. No one-login-per-day quota. |
| B35 | **Failed-login protection:** audit logging of failures; **rate limiting** of **5 failed attempts within 15 minutes** (further attempts throttled until the window expires); a successful login resets the relevant counter. **No account lockout.** Django's secure password hashing. |
| B32 | Timezone `Asia/Kolkata` everywhere (dates, timestamps, loans, subscriptions, dashboard, scheduled tasks, backups, audit). |
| B37 | Localhost-only (127.0.0.1); no LAN access. |

## 1.8 Environment, backup, export, dashboard, brand
| ID | Decision |
|---|---|
| B36 | Dell runtime = **Windows 11** (edition/build unspecified until verified). Development = MacBook M3, macOS. Separate environments. |
| B38 | Daily fixed-time backup; catch-up at next launch; dashboard warning if last success > 26 hours. |
| B39 / N7 | Random encryption key. On the Dell the **working key is stored in Windows Credential Manager** (protected by the Windows user account). The owner saves a **separate recovery key**, never alongside backups. Credential Manager entries are not assumed to transfer between computers; replacement recovery explicitly restores the key after owner confirmation (see TECHNICAL_ARCHITECTURE §8). Loss of the recovery key may make backups unrecoverable. |
| B40 | Owner connects Google account and selects the Drive folder; `drive.file` scope; replacement computer needs fresh consent. |
| B41 | Full export = ZIP with CSV, Excel and manifest. |
| B43 / N8 | Default dashboard sections: overdue books, upcoming subscription expiries, backup failures, failed emails, damaged/lost book decisions, token forfeitures, members requiring attention. **No Pending Payments section.** The owner configures visibility and the **criteria** of "Members requiring attention"; default criteria (accepted by the owner): (1) expired subscriptions; (2) borrowing suspended because the ₹500 token was forfeited; (3) overdue books; (4) unresolved damaged or lost book cases; (5) pending owner decisions; (6) incomplete membership records. Criteria and visibility remain owner-configurable. |
| B45 | **Import cost:** a Unit Cost (INR) with more than two decimal places is rejected (no silent rounding or truncation). |
| B46 | **Import copy IDs:** supplied copy IDs matching `COV-C-<number>` are reserved for generated IDs and are rejected. |
| B47 | **Publication year** must be between 1000 and the next calendar year (inclusive). |
| B48 | **Recovery codes:** generating a new recovery code immediately invalidates all earlier unused recovery codes; only the newest is valid (single-use, hashed, shown once, 15-minute expiry still apply). |
| B44 | Brand references preserved; derived assets and design tokens separate; lettering artwork is not a font. |
Dependency decision: Python `keyring` (Windows Credential Manager access) is **approved**. Retention 30 daily + 12 monthly. Not mandatory: Docker, Celery, Redis, React.

---

**Q7b — receipt-number year (CONFIRMED).** Receipt numbers use the **calendar year (January–December, Asia/Kolkata) of the receipt issuance date**; a backdated payment or effective date never changes the receipt-number year. Example: payment effective date 31 Dec 2026, receipt issued 5 Jan 2027 → `SUB-2027-…`.

**Q8 — automatic email schedule (CONFIRMED).**

| Setting | Value |
|---|---|
| Scheduled reminder time | 09:00 Asia/Kolkata |
| Subscription expiry reminder | 15 days before expiry |
| Overdue reminders | due date, day 3, day 7 |
| Day-8 forfeiture notification | immediately after forfeiture processing |
| Initial email | immediately |
| SMTP retry 1 | 5 minutes after the initial failure |
| SMTP retry 2 | 30 minutes after retry 1 fails |
| SMTP retry 3 | 2 hours after retry 2 fails |
| After retry 3 fails | notification marked failed; error details retained; no indefinite retrying |
| Maximum retries | three retries after the initial attempt (delays run from the preceding failed attempt, not cumulatively) |

Requirements: no duplicate notification for the same event; retries never create duplicate emails; missing email addresses recorded as skipped with a reason; SMTP accepted is distinguished from delivery; all scheduled times in Asia/Kolkata.

**Q1–Q6, Q7, Q7b, Q8 and the dashboard defaults are all recorded above as confirmed.** Dependency decision: Python `keyring` is approved (not installed until implementation is approved).

---

# 2. Remaining unresolved items

**None.** All business-policy decisions and specification questions are decided. No genuine specification decision is currently outstanding.

---

# 3. Owner decisions D-1 to D-16 (reconciled after the Phase 3 acceptance review)

All of D-1 to D-16 are decided or approved; none is pending. These were implementation-level decisions (not business-policy blockers). The table reconciles D-7 to D-16 with their implementation and test evidence; the full wording of each decision is in its row below.

| ID | Status | Implemented in | Verified by (tests) |
|---|---|---|---|
| D-7 | Decided (final) | `finance.corrections.post_adjustment`, trigger `finance_txn_correction_rules` (`finance.0006`) | `AdjustmentNotAFeeRefundTests`, `CorrectionRuleTriggerTests`, `ReversalTests`, `MemberJourneyTests`. **Open:** Phase 6 reports must list unlinked cash-out adjustments separately. |
| D-8 | Decided | `memberships.services.renewal_blockers` | `ForfeitedTokenTests`, `RefundTests`, `MemberJourneyTests` |
| D-9 | Decided | `finance.corrections.reverse_transaction`, per-cycle unique indexes | `ReversalTests`, `LedgerInvariantTests`, `MemberJourneyTests` |
| D-10 | Decided | `memberships.services._check_start_backdating` | `RegistrationTests`, `RenewalTests`, `BackdatingAuditTests` |
| D-11 | Decided | receipt snapshot stores the member ID only | `ReceiptNumberingTests`, `MemberJourneyTests` (anonymised receipt page) |
| D-12 | Decided | `refund_token`, `borrowing_blockers` | `AnonymisedMemberEligibilityTests`, `MemberJourneyTests` |
| D-13 | Decided | `finance.allocation` (report screens in Phase 6) | `AllocationTests` |
| D-14 | Decided; plan editing deferred to the settings phase | two seeded plans | `PlanTests` |
| D-15 | Approved; implementation deferred to Phase 4b | nothing yet | Phase 4b tests specified in D-15 |
| D-16 | Decided | `core/money.py`, migrations `finance.0007`, `memberships.0006`, `core.0004`, `catalogue.0004` | `ApprovedLimitLiteralTests`, `AmountBoundaryTests`, `AmountInterfaceTests`, `ReasonLengthTests` |
| (no automatic renewal) | Register decision (PRD 6.5) | no scheduler or creator other than `register_member` / `renew_subscription` | `test_no_auto_renewal` |

| ID | Item | Current behaviour |
|---|---|---|
| D-1 | **Password-reset link lifetime** | **DECIDED by the owner: 3,600 seconds (1 hour)**, replacing Django's 3-day default. Implemented in `PASSWORD_RESET_TIMEOUT` and covered by regression tests (expiry at the boundary, expired and used links cannot reset, no enumeration, throttle unchanged, no sensitive data in records). |
| D-2 | **Reset-request throttle** | 3 requests per 15 minutes per email key and per client address; throttled requests get the same generic response and send no email. The numbers are an implementation default. |
| D-3 | **Historical plaintext passwords** | Code before the remediation stored whatever was typed in the username box in `LoginEvent.username_tried` and in audit summaries. No such rows exist in the development database (checked: 0 login events, 0 audit events). Any other environment that ran the earlier code may hold mistyped passwords in the append-only audit log; this cannot be removed by the application. Owner-led response if such an environment exists: treat those passwords as compromised and change them. |
| D-4 | **Database hardening (L6)** | **DECIDED by the owner:** two-role model on Mac and Windows; `coven_owner` owns objects and runs privileged migrations; `coven_app` is the runtime role; `postgres` remains break-glass; members, editions and copies are no-delete; no BEFORE TRUNCATE trigger (TRUNCATE is withheld through privileges); implemented before Phase 3. Implemented and verified on macOS/PostgreSQL 16.15. Windows 11 and clean-machine recovery remain **unverified** (Phase 7b/7c). |
| D-5 | **Owner credentials on the Dell** | **DECIDED by the owner:** a temporary `.env.owner` is allowed during upgrades only. Create it only when an upgrade is required; restrict permissions to the Windows account running the upgrade; never place owner credentials in Task Scheduler's runtime environment; delete it after successful migration and verification; never include credentials in logs or backups. |
| D-6 | **Dev `coven_owner` CREATEDB** | **DECIDED by the owner:** `coven_owner` has CREATEDB on development machines only (test suite). `coven_app` never receives it. Production-like environments, including the Dell, do not grant CREATEDB to either role. |
| D-7 | **Phase 3: corrections of subscription fees and refunds** | **DECIDED by the owner (final wording):** (1) subscription payments and paid refunds cannot be reversed; (2) adjustments cannot reference subscription-payment transactions for money out; (3) an **unlinked cash-out adjustment** is permitted only as an explicit, owner-approved accounting correction, with a **mandatory reason** and **explicit owner confirmation** that it is not a subscription-fee refund; (4) owner attribution, timestamp and amount are recorded; (5) such adjustments must be **identified distinctly in financial reports** and are never presented as subscription-fee refunds. Legitimate accounting adjustments remain permitted. *Implemented in Phase 3:* the confirmation, reason, link restriction, attribution (`recorded_by`), timestamp (`processed_at`) and amount; the audit event stores the confirmation; trigger `finance_txn_correction_rules` enforces (1) and (2) for direct SQL too. *Still to do:* point (5) — the Phase 6 reports must list adjustments with `kind = adjustment`, `cash_flow = out` and no linked record separately (the data already identifies them; `reporting.collections` currently totals all outgoing adjustments together). The database cannot see the confirmation (it is a service rule recorded in the audit event). |
| D-8 | **Phase 3: renewal needs a held token** | **DECIDED by the owner (confirmed):** renewal requires a held token. A refunded or reversed token does not satisfy this; a fresh token must be collected first. |
| D-9 | **Phase 3: reversing token records** | **DECIDED by the owner (confirmed):** only the latest token cycle can be corrected. A reversed forfeiture cannot be repeated (use an adjustment). Each cycle has at most one collection, refund and forfeiture. |
| D-10 | **Phase 3: backdated subscription start** | **DECIDED by the owner (confirmed):** a subscription start date before today requires a reason and additional owner confirmation. Future activation dates remain allowed (a scheduled subscription). |
| D-11 | **Phase 3: receipts and anonymisation** | **DECIDED by the owner (confirmed):** receipts retain the permanent member ID, not the member's name (the name is shown live). UPI references are retained for financial reconciliation, with owner-only access (every page requires the owner's login). |
| D-12 | **Phase 3: anonymised members** | **DECIDED by the owner:** an eligible full token refund is allowed after anonymisation. Anonymisation never restores borrowing or renewal eligibility (renewal, a fresh token and borrowing stay blocked). |
| D-13 | **Phase 3: cancelled subscriptions in the allocation view** | **DECIDED by the owner (confirmed):** a cancelled subscription stays in revenue allocation because subscription fees are non-refundable. |
| D-14 | **Phase 3: plan configuration** | **DECIDED by the owner:** plan editing is deferred to the settings/configuration phase. **Outstanding requirement:** plan prices and durations must be editable for future subscriptions only (existing subscriptions keep their snapshots); the two seeded plans (6 months Rs 800, 12 months Rs 1,500) are the only plans until then. |
| D-15 | **System actor for automatic forfeiture (H4)** | **APPROVED by the owner; implementation deferred to Phase 4b (nothing implemented in Phase 3).** Approved design: (1) an optional `recorded_by_system` attribution is permitted **exclusively** for the automatic day-8 forfeiture (trigger `overdue_auto`); (2) it creates **no** user record and does not weaken the single-owner constraint; (3) manual owner actions keep the authenticated owner as the actor; (4) database constraints must make invalid combinations impossible (exactly one of owner / system attribution; system attribution only on a `token_forfeiture` with trigger `overdue_auto`); (5) automated events are explicitly identifiable in audit records (`actor` empty and `summary.system` naming the process); (6) the system actor is **not** a general-purpose attribution bypass: every owner-confirmed forfeiture, reversal, refund and adjustment keeps the owner. The launch catch-up uses the same actor with effective date day 8 and the real processing timestamp (N6). Phase 4b tests must prove the database refuses a system actor on any other record. |
| D-16 | **Amount and reason limits (H1/H3)** | **DECIDED by the owner (final limits):** maximum monetary amount **₹49,999.00** (4,999,900 paise); maximum reason or note length **2,000 characters**; maximum catalogue unit cost **₹49,999.00**, using the same shared monetary limit (manual entry and Excel import). Longer reasons and larger amounts are rejected with a clear message, never shortened. *Implemented:* `core/money.py` (`MAX_PAISE`, `MAX_RUPEES`, `MAX_REASON_LENGTH`), service and form validation, CHECK constraints on all eight money columns (replaced by migrations `finance.0007`, `memberships.0006`, `core.0004`, `catalogue.0004`) and tests with literal boundary values. The money columns stay 64-bit (headroom; no narrowing). Applying the migrations fails if a stored amount already exceeds ₹49,999.00 (none exist in the development database). |

## Phase 4 owner decisions D-17 to D-24 (formally confirmed; not to be reinterpreted)
The owner formally confirmed D-17 to D-24. They fill gaps the register left open, change no approved rule, and are covered by tests. D-23 was revised by the owner to the transactional outbox below.

| ID | Item | Implemented default |
|---|---|---|
| D-17 | **Which loans a grace change re-evaluates (N11/Q5)** | Only loans still out (unreturned, not yet forfeited) that become eligible because of the change. Loans already eligible under both settings are left to the daily job; closed loans are not re-evaluated; a loan whose decision was declined is never prompted again (even for a later change) and the job skips it. |
| D-18 | **"Last valid day" for the loan rules (A2, B2, B3)** | The last valid day of the member's contiguous membership: the current subscription plus every non-cancelled one that starts exactly when the previous ends. A member who renewed early is therefore valid through the renewed period for the 15-day block, the due-date cap and loan renewals. |
| D-19 | **Due date at issue (PRD 6.7)** | Default issue + `loan_days` (30). If that date is after the last valid day the issue is refused with a clear message, and the owner may explicitly choose an EARLIER due date (never later than 30 days, never after the last valid day, never silently shortened). |
| D-20 | **Backdated loan dates (B19/N12)** | A past issue, return or loss date always needs a mandatory reason and the extra owner confirmation, because a loan date can change due dates and forfeiture. Future dates are refused. Renewals are recorded as of today (not backdatable). |
| D-21 | **Effective dates of forfeitures** | Automatic: day 8 (due + grace + 1), processing time recorded separately (N6). Owner-confirmed Poor / Damaged / Lost: the return or loss date. Confirmed after a grace change: day 8 under the current grace period. |
| D-22 | **Automatic and manual forfeiture together** | If a book comes back as Poor / Damaged / Lost after day 8, the automatic forfeiture is posted first (system actor) and the owner's classification still applies to the copy; the token is never forfeited twice. A return judged on its own date (return on day 8 or later forfeits). |
| D-23 | **Notifications: transactional outbox (confirmed)** | Phase 3 and Phase 4 domain events are stored durably in `core_domainevent` (APPEND_ONLY, unique non-empty `dedupe_key`) inside the SAME database transaction as the business operation: rollback = no event, commit = event exists, repeated emit with one key = one row. Implemented: durable storage only (`core/outbox.py`, `core.0007-0008`). Phase 5b owns delivery, notification records, the 09:00 schedule, SMTP retries and state. Phase 7c owns scheduling. An event with no notification record is unconsumed. |
| D-24 | **Launch catch-up (confirmed)** | `python manage.py run_daily_jobs` is the idempotent daily/catch-up command; wiring it to Task Scheduler and application launch is Phase 7c. Until then it must be run by hand, and a forfeiture is only posted when it runs (always with the original effective date). Renewals of loans already out stay possible when the token is forfeited or the member is not active (B16). |



## Phase 5 implementation defaults awaiting owner confirmation (D-25 to D-26)
Phase 5 filled gaps the register leaves open. None changes an approved rule; each is the most conservative reading, is isolated in one place and is covered by tests. Please confirm or change.

| # | Gap | Implemented default |
|---|---|---|
| D-25a | **"Incomplete membership record"** (attention criterion 6) | A member with no subscription at all, or one whose reactivation requires a new subscription. A missing email is NOT incomplete: it is a normal state recorded as a skipped email. |
| D-25b | **"Unresolved damaged or lost case"** (criterion 4, decisions section) | A copy of that member's loan that is still Lost, or Damaged/Poor and not available again. (Phase 4 has no separate "resolved" flag.) |
| D-25c | **Upcoming-expiry window** | 30 days by default, owner-configurable from 1 to 365 days in dashboard settings. A member whose subscription is continued by an early renewal is not listed. |
| D-26a | **When the "subscription expiry notice" is sent** | At 09:00 Asia/Kolkata on the expiry date (the first expired day), unless the subscription is continued by a renewal. |
| D-26b | **Subscription renewal email** | A `subscription.renewed` event sends the same "subscription activation" email as a first activation (a renewal activates a subscription). |
| D-26c | **Events with no email** | `member.registered`, `token.collected`, `token.refunded` (its REF receipt already sends `payment.receipted`) and `loan.lost` (the token forfeiture it causes sends `token.forfeited`) have no email. They are consumed with a bookkeeping "skipped" record so that "consumed = has a notification record" holds. |
| D-26d | **Missed reminder days** | **Decided by the owner (D-27a):** back-filled for up to 7 calendar days, only while still relevant (see D-27). |
| D-26e | **Anonymisation and past emails** | **Decided by the owner (D-27b).** |
| D-26f | **Immediate sending** | The email follows an owner action in the same request (best effort, never blocks or breaks the action); `run_daily_jobs` and the "Process now" button are the safety net. Retries happen only when a cycle runs after they fall due, so the timetable is exact only once Phase 7c runs the job unattended. |
| D-26g | **Crash window** | If the application dies after SMTP accepts a message but before the result is saved, the 10-minute lease expires and the message is sent again. This at-least-once window cannot be closed without SMTP idempotency; it is disclosed rather than hidden. |

## Owner decisions D-27 (Phase 5 acceptance review; supersede D-26d and D-26e)
| # | Decision |
|---|---|
| D-27a | **Reminder back-fill.** A reminder missed while the application was off is created on the next run for up to **7 calendar days** after its scheduled date, but only if still relevant at processing time: an expiry reminder only before expiry and while not renewed; an expiry notice only while the member is still not covered by a subscription; an overdue reminder only for a loan still out and before day 8 (and only the LATEST missed milestone is created, older ones are superseded). The original date is kept in `scheduled_for`; the real processing time is `created_at` / the attempt log. Idempotency keys prevent duplicates. Day-8 forfeiture processing is unchanged. |
| D-27b | **Anonymisation of notifications**, done inside the anonymisation transaction and audited (`notifications.redacted`): not yet accepted by SMTP (prepared, retry-pending) -> skipped, address, subject and body erased; failed after all retries -> skipped, address, subject and body erased; SMTP-accepted -> subject and body erased, address and accepted time kept **90 days**, then the address is erased automatically and the status and time remain for reports. Stored errors keep only their type (attempt rows never contain an address). |
| D-27c | *Implementation assumption to confirm:* the 90 days are counted **from the anonymisation date** (not from the send date). |

## Phase 6 questions for the owner (D-28; raised before implementation)
The register fixes the report list, both revenue bases, the D-7 adjustment listing, full-precision allocation, formula escaping and the ZIP structure. It does not define the following; each has a proposed default and only the first four block work.

| # | Question | Proposed default |
|---|---|---|
| D-28a | **Date presets "week", "quarter", "year"** | Week = Monday to Sunday; quarter = calendar quarter (Jan-Mar, ...); year = calendar year (1 Jan-31 Dec). Alternative: Indian financial year (1 April-31 March) for quarter and year. |
| D-28b | **Which outputs get a PDF** (the register says "PDF where applicable") | PDF for the member statement and for the two revenue reports (collections and allocation); every report gets CSV and XLSX. |
| D-28c | **"Borrowing statistics"** | Loans issued and returned per month, average days kept, most borrowed titles (top 10) and categories, overdue rate (share of loans returned after the due date). |
| D-28d | **Full export (ZIP) contents** | One CSV and one XLSX per entity (members, subscriptions, token cycles and ledger, financial transactions and payment entries, receipt documents, editions, copies, copy events, loans and renewals, notifications without message text, audit events) plus `manifest.json` (export time, app version, row counts, SHA-256 of every file). Anonymised members appear anonymised. Message bodies and addresses are excluded. |
| D-28e | Rounding in report totals | Each displayed line is rounded half-up once from the exact amount; a total is rounded once from the exact sum, so a total can differ from the sum of its displayed lines by Rs 0.01; a note says so. |
| D-28f | Member statement | One member, all dates or a chosen range: subscriptions, payments and receipts, token movements, refunds, forfeitures, adjustments, loans. Open to anonymised members only as "Anonymised member" with the member ID. |
| D-28g | Access and audit of exports | Owner login only; each export is audited (kind, filters, row count, no personal data) and the file is generated on request, not stored. |

**Owner answers (Phase 6 start):** D-28a **decided: calendar** (week Monday-Sunday, calendar quarter, calendar year). D-28b **decided: PDF for the member statement and the two revenue reports**; every report has CSV and XLSX. D-28c **decided: default borrowing set** (issued/returned per month, average days kept, top 10 titles and categories, overdue rate). D-28d **decided: all records, no message text** (ZIP of CSV + one XLSX workbook with a sheet per entity + `manifest.json` with row counts and SHA-256; no message subjects, bodies or addresses). D-28e, D-28f and D-28g are implementation defaults awaiting confirmation; they are isolated in `reports/` and covered by tests.

**Phase 6 implementation status:** D-7 point (5) is now implemented: the adjustments report lists unlinked cash-out adjustments in their own section titled "NOT subscription-fee refunds" (tested). D-28a to D-28d are implemented as the owner decided. D-28e (rounding once, with a printed note), D-28f (statement contents) and D-28g (owner-only, audited, generated on request) are implemented as proposed and await confirmation. Further interpretations to confirm: the lost/damaged report lists copies currently lost, damaged, poor or withdrawn plus loans closed that way in the period; "outstanding and overdue loans" is an as-at report (end of the period, never later than today); the members report lists subscriptions valid on any day of the period.

## Phase 6 acceptance decisions (owner; D-28e to D-28k)
D-28e (round once, with the printed note), D-28f (statement contents) and D-28g (owner-only, audited, generated on request) are **accepted**, as are the three report interpretations (lost/damaged, outstanding/overdue as at the period end capped at today, members with a subscription valid on any day of the period). New decisions, implemented in `reports/`:
| # | Decision |
|---|---|
| D-28h | **Phone numbers and formula escaping.** A value is dangerous when its first character after any leading whitespace or control characters (all Unicode whitespace, C0/C1 controls, DEL, zero-width and BOM characters) is `=`, `+`, `-` or `@`, or when it starts with a tab or carriage return. It is kept exactly as entered with an apostrophe in front. The ONLY exemption is a strict phone number: `+`, then digits, spaces and dots, with at least one digit (nothing else beginning with `+` is exempt). Applies to CSV, XLSX, the statement and the ZIP. |
| D-28i | **Masking for anonymised members.** The immutable records are not changed. Transaction reasons, backdate reasons, token-ledger reasons, forfeiture confirm and decline reasons and (via the loan) copy-event notes show `Redacted — member anonymised` in statements, the adjustments report and every CSV, XLSX, PDF and ZIP output; a live member's text is unchanged. Acquisition donor names and contacts are NOT in any export. **Corrected wording:** they remain stored (entered through the add-copies form and the Excel import) but are NOT displayed anywhere in the current application and are not part of any general ZIP export; there is no owner-only inventory detail for them. A donor view or report would need its own decision. |
| D-28j | *Implementation note:* XLSX cannot store control characters (they crashed the workbook). Each is shown as its visible control picture (for example NUL appears as the NUL symbol); CSV, PDF and the database keep the original character. Found while testing D-28h. |

**D-28i, second pass (owner decision):** payment-entry **references** are also masked (`Redacted \u2014 member anonymised`) for anonymised members in the collections report (screen, CSV, XLSX) and the ZIP; backdate reasons and forfeiture confirmation reasons are covered by permanent tests (masked for anonymised members, unchanged for live members); originals stay in the database. **Decided (R-59):** the operational receipt page and receipt PDF (owner-only documents reproduced from the immutable receipt record) keep showing the reference as recorded, also for anonymised members; they are not reports or exports, and anonymisation is not described as complete removal of personal information from historical financial documents. **D-28k:** the import error report now uses the same single escaper (`core.spreadsheet.safe_text`) as the reports; structure and messages are unchanged.

## Phase 7 questions for the owner (D-29; raised before implementation)
Verified against the register: B36 (Windows 11 runtime), B38 (daily fixed time, catch-up, 26 h warning), B39/N7 (random key; working key in Windows Credential Manager; separate recovery key) and B40 (owner connects Google, selects the folder, `drive.file`, fresh consent on a replacement computer) are all decided; B37 (localhost-only) is decided and already enforced. The dependencies `cryptography`, `keyring` (approved) and `google-api-python-client` / `google-auth-oauthlib` are already justified in TECHNICAL_ARCHITECTURE section 14. Backups run as the runtime role (`pg_dump` can read every table and sequence, see `core/dbsecurity.py`); restore needs the owner role. The register does NOT settle:

| # | Question | Why it matters | Proposed default |
|---|---|---|---|
| D-29a | **How is the Drive folder "selected" under the `drive.file` scope?** With that scope an application can only see files and folders it created itself or that the user opened with it through the Google Picker. A pasted folder ID of an ordinary folder is refused. | B40 says the owner selects the folder; the scope makes that mechanism-dependent. | The application creates (once, after consent) a folder named "Coven's Library Backups" in the owner's Drive and uses it; the owner may rename or move it. Alternative: Google Picker (needs a browser API key and extra JavaScript). |
| D-29b | **Default daily backup time** (`LibrarySetting.backup_time`, currently empty). | B38 says "fixed configured time". | **Decided: 19:45 Asia/Kolkata, editable in settings** (an earlier draft proposed 22:00; superseded). |
| D-29c | **Which backup becomes the monthly one?** | "12 monthly" is not defined further. | The LAST successful backup of each calendar month is kept as that month's monthly backup (promoted when the month ends or the next month's first backup succeeds); 30 daily + 12 monthly; the newest successful backup is never deleted. |
| D-29d | **Recovery key form.** | B39 says "separate recovery key". | The recovery key is the working key written as printable groups with a checksum, shown once at setup (and on request after re-authentication) for the owner to store offline; the manifest carries only a non-secret fingerprint. |
| D-29e | **Google OAuth client.** The owner (or the maintainer) must create a Google Cloud OAuth client (client ID and secret) for the application; it cannot be created from here. | Needed before the Drive adapter can be used for real. | Client ID and secret are held in Windows Credential Manager (never in files); the Drive adapter is written behind an interface and tested against a local fake; real Google behaviour stays UNVERIFIED until the owner supplies credentials. |
| D-29f | **What "media" means.** No uploaded media exists today (`media/` is absent). | The backup archive must still be well defined. | The archive includes `media/` and `static` branding assets under `assets/` if present, listed with hashes in the manifest; an absent folder is recorded as empty, not an error. |

**Owner answers (Phase 7a start):** D-29a **decided: app-created folder** ("Coven's Library Backups", created once after consent). D-29b **decided: default backup time 19:45 Asia/Kolkata** (editable). D-29c **decided: the LAST successful backup of each calendar month** is that month's monthly backup. D-29d **decided: printable copy of the working key** as the recovery key. D-29e and D-29f stand as proposed. Further implementation assumptions (awaiting confirmation, isolated in `backups/`): a failed scheduled backup is retried no sooner than 30 minutes later by the catch-up check; a backup left "running" for over 4 hours is treated as interrupted; pre-restore safety backups are never removed by retention; the key is never rotated automatically (rotation would need all older backups re-encrypted or kept with their key).

**Owner decisions on the Phase 7 assumptions (acceptance review):** (1) D-29b is 19:45 IST. (2) D-29e: the owner creates the Google OAuth client; credentials and tokens are never committed (held in the credential store). (3) A failed backup is retried no sooner than 30 minutes later: **approved**. (4) A backup "running" over 4 hours is treated as interrupted: **approved**. (5) **Safety (pre-restore) backups are kept until the restored system has passed verification AND a later backup has succeeded; after that they become eligible for normal retention. Decided and IMPLEMENTED (owner-authorised): protected until the restore's verification passed AND a later daily backup succeeded and was verified; then it takes part in normal retention. (6) No automatic key rotation; any future rotation is an explicit owner-controlled operation with verified re-encryption and a recovery procedure. (7) Scheduled tasks run only while the owner is signed in; backups are deferred while signed out and the 26-hour warning applies: **accepted**. (8) The printable recovery key is approved; the owner keeps an additional offline copy apart from the Dell; losing the Dell and every copy of the key means recovery is impossible.

**Phase 7b implementation decisions (assumptions awaiting your confirmation, isolated in `backups/restore.py`):** (1) restore never creates a database (the Dell's owner role has no CREATEDB, D-6): it restores into an EXISTING database owned by the owner role (replacement computers provision an empty one first); (2) a non-empty target is replaced only with `--replace` and typed confirmation, and only after a successful safety backup of what is being replaced (`--skip-safety-backup` exists but needs a second typed confirmation and is reported); replacing removes what the owner role owns in that database (`DROP OWNED BY`) and then restores, so a failure after that point is recovered from the safety backup; (3) `media/` is restored exactly as backed up (files added after the backup are removed); application `assets/` ship with the code, so a missing asset is restored and a different one is only reported; (4) importing the recovery key into the Windows Credential Manager needs the typed confirmation "RESTORE KEY" and is read back to verify; (5) the backup run that was in progress when the dump was taken is completed in the restored history as the record of the restored file, so a restored database is never blocked by a phantom running backup; (6) the restore command states that recovery is NOT proven until a clean-machine rehearsal has been performed.
