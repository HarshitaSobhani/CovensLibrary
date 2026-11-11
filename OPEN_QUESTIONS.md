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
