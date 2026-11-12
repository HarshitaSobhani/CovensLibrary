# Coven's Library — Product Requirements Document

**Version:** 2.0 (reconciled with the owner's confirmed decisions)
**Status:** Specification; no implementation exists
**Product type:** Private, owner-operated library management web application
**Primary operator:** One library owner
**Scale:** Up to ~50 members; ~200 catalogue editions at launch (physical copies tracked separately)
**Runtime:** Dell laptop, Windows 11, accessed through its browser on localhost
**Development:** MacBook M3, macOS (separate environment)

Decision identifiers (A1, B1…, N1…) refer to the register in `OPEN_QUESTIONS.md`, which is authoritative if this document and the register ever differ.

---

## 1. Executive summary

Coven's Library is a private web application used only by the library owner to manage a physical lending library. Members have no accounts and cannot access the system. The owner maintains members, the catalogue and individual physical copies, records subscriptions and payments, issues and receives books, records renewals, manages the refundable ₹500 token, sends reminders, and reviews reports. It must be simple, reliable and auditable, and portable to cloud hosting later. It runs locally first and needs internet only for email, Google Drive backup and similar services; offline operation is not a goal.

## 2. Goals
1. One reliable source of truth for members, subscriptions, books, copies, loans, payments and token balances.
2. Fast owner workflows: register a member, record payment, issue, return, renew.
3. Prevent invalid operations through server-side rules, not owner memory.
4. Preserve financial and operational history.
5. Useful dashboard, reports, exports, receipts, automatic email and encrypted daily backups with replacement-computer recovery.
6. Lightweight architecture suitable for an 8 GB RAM / 256 GB Dell.

## 3. Non-goals
Member login/portal/registration; multiple branches or tenants; online payment gateway; WhatsApp Business API or any automatic WhatsApp sending; reservations; delivery workflows; mobile apps; recommendations, ratings, wishlists; offline operation; staff accounts or roles; LAN access. Members request renewals by phone/text/WhatsApp/email; the owner records them.

## 4. Access and security

### 4.1 Role
Owner: the only authenticated user, full access.

### 4.2 Authentication
- Private login; Django password hashing; session authentication; logout.
- The owner authenticates at least once each day. Sessions expire at the end of the day (Asia/Kolkata). **There is no limit on logins per day**: logging out, or a lost or expired session, never prevents another login.
- **Failed-login protection:** every failed login is audit-logged. After **5 failed attempts within 15 minutes**, further attempts are rate-limited until the 15-minute window expires; a successful login resets the counter. **There is no account lockout.**
- **Password recovery:** email-based reset; if SMTP is unavailable, local command-line recovery using a recovery code that is single-use, stored hashed, displayed once, and expires after 15 minutes; generating a new code immediately invalidates any earlier unused code (B48).
- Every view is protected except login and required static files. No public data.

### 4.3 Security requirements
CSRF protection, secure sessions, input validation, output escaping, ORM parameterisation; secrets only in environment variables or Windows Credential Manager; restricted upload type and size; validated imports; auditable financial and membership operations; no passwords, secrets or unnecessary personal data in logs; bind to `127.0.0.1` only.

## 5. Confirmed business facts

| Area | Requirement |
|---|---|
| Access | Owner only; localhost only |
| Library | One physical library |
| Members | Up to 50; individual memberships; permanent unique member ID; mobile mandatory and unique (E.164, international, country-code picker); email optional |
| Book ownership | Coven-owned and donated books (donated books become library property) |
| Plans | ₹800 / 6 calendar months; ₹1,500 / 12 calendar months; same benefits; no free tier; no automatic renewal |
| Subscription fees | **Strictly non-refundable** |
| Token | ₹500, mandatory, refundable under the rules in §6.6 |
| Borrowing | One active book per member; 30 days; unlimited owner-recorded renewals of 30 days |
| Payments | Cash and UPI recorded manually; split payments |
| Receipts | PDF and print |
| Email | Automatic via SMTP |
| WhatsApp | Manual: prefilled message, owner sends |
| Timezone | Asia/Kolkata |
| Backups | Daily encrypted to Google Drive; 30 daily + 12 monthly |
| Import / export | Excel import; full ZIP export (CSV + Excel + manifest) |
| Hosting | Local first (Dell, Windows 11); cloud-portable |

## 6. Functional requirements

### 6.1 Owner dashboard
Default sections: overdue books; upcoming subscription expiries; backup failures; failed emails; damaged/lost-book decisions; token forfeitures; members requiring attention. There is **no Pending Payments section**. The owner configures section visibility and the criteria of "Members requiring attention". Members requiring attention — default criteria (accepted by the owner; criteria and section visibility remain owner-configurable): (1) members with expired subscriptions; (2) members whose borrowing is suspended because their ₹500 token was forfeited; (3) members with overdue books; (4) members with unresolved damaged or lost book cases; (5) members with pending owner decisions; (6) members with incomplete membership records.
Also: total editions **and** total physical copies; total/active members; available and borrowed copies; monthly collections showing all receipts with subscription fees, token collections and other transactions separately (tokens never counted as subscription revenue); recent transactions; light/dark themes; rearrangeable widgets with persisted layout; global search (member name, mobile, member ID, title, author, ISBN, copy ID, transaction/receipt number); calendar of expiries, due dates and reminder dates; quick actions (add member, add book, issue, return).

### 6.2 Member management
Fields: permanent member ID (format `COV-M-000001`), full name, mobile (E.164, mandatory, unique), optional email, joining date, notes, owner-managed status (active, suspended, archived).
- Owner can create, view, edit, suspend, reactivate, archive and search.
- **Registration** is a guided workflow: details → plan → subscription payment entries → token collection → confirmation and receipt → activation. Activation happens only after the full subscription fee is paid and the token is collected. Entered data is retained on validation errors.
- A member has at most one current subscription and one scheduled future subscription; history is retained. The member ID never changes, including on rejoining.
- Eligibility: subscription dates determine subscription eligibility; owner status can independently suspend or archive. A valid subscription does not override suspension.
- **Reactivation** of a suspended or archived member: the owner chooses to restore the previous arrangement or require a new subscription.
- **Exit:** the owner handles early exit manually. The token is refunded if eligible (§6.6). The subscription fee is never refunded; there is no prorated or credit workflow and no subscription-fee refund transaction.
- **Rejoining / fresh token:** the same member ID is kept and a fresh ₹500 token is paid, which begins a new token cycle (see §6.6 for the forfeited-token case).
- **Deletion/anonymisation:** controlled; never cascades into financial, loan or audit history. Anonymisation changes live data only; historical backups remain unchanged until their normal retention expiry, and the application discloses this.

### 6.3 Book catalogue and inventory
Editions and physical copies are separate entities; the dashboard reports both. Each edition is its own record.
- **Edition:** title (required); author (may be blank or explicitly "Unknown author"); ISBN (optional; duplicates produce a warning, not a block); publisher; edition information; publication year; one category; description; notes; archived flag.
- **Copy:** unique copy ID in the format `COV-C-000001` (generated, or a supplied legacy ID chosen per import); Code 128 barcode whose value is the copy ID; edition; **condition** (Good, Poor, Damaged, Lost) and **circulation status** (Available, Borrowed, Damaged, Lost, Withdrawn) as separate fields; a Poor-condition copy always has status Withdrawn; acquisition source (purchased/donated), donor, date, cost where applicable; shelf location; notes.
- A copy is issuable only if condition is Good and status is Available. Poor, Damaged, Lost, Withdrawn and Borrowed copies are not issuable. Copy-level history (acquisition, issue, return, condition and status changes) is kept. Books or copies with history are never hard-deleted.
- **Barcode labels:** Code 128 printable label sheets; the owner configures label dimensions and page layout. No fixed printer or label stock.
- Categories are owner-managed.

### 6.4 Excel import
Books and copies; downloadable template. Workflow: select file → validate/map columns → preview with errors and warnings → owner decisions → owner confirmation → execution → summary and downloadable error report. Original rows are preserved in staging. Validation rules: Unit Cost with more than two decimal places is rejected (B45); supplied copy IDs matching `COV-C-<number>` are reserved and rejected (B46); Publication Year must be 1000 to the next calendar year (B47).
- Per import the owner chooses whether to **create missing categories or reject** affected rows.
- Per import the owner chooses whether to **use supplied legacy copy IDs or generate** them; conflicts are detected and duplicate copy IDs prevented.
- Duplicate ISBNs produce warnings; the owner decides whether to continue; no automatic merging or silent discarding.
- Atomic commit: a failed import leaves no partial data.

### 6.5 Subscription management
Plans are configurable for future subscriptions; existing records keep their plan name, duration and price snapshot.

**Date calculation (A1, B1).** The start date is inclusive. The expiry date is the **first expired day** = start + N calendar months; if the start day does not exist in the target month it is clamped to that month's last day. The **last valid day** is the day before the expiry date. The member is active through the last valid day and expired from the expiry date onward. Calendar-month arithmetic, not fixed days.

| Start | Duration | First expired day | Last valid day |
|---|---|---|---|
| 11 Nov 2026 | 6 months | 11 May 2027 | 10 May 2027 |
| 31 Aug 2026 | 6 months | 28 Feb 2027 | 27 Feb 2027 |
| 31 Jan 2027 | 1 month | 28 Feb 2027 | 27 Feb 2027 |
| 29 Feb 2028 | 12 months | 28 Feb 2029 | 27 Feb 2029 |

**Rules.**
- No automatic renewal. The owner chooses the plan; the system calculates dates.
- At most one current subscription and one scheduled future subscription; periods never overlap.
- Early renewal starts on the current subscription's first expired day. Late renewal starts on the **owner-entered activation date**.
- A subscription must be fully paid before activation; no outstanding-balance workflow exists.
- **Subscription fees are non-refundable**, including early cancellation: no refund, no prorated or unused-period amount, no credit.
- Renewal requires a held token. It is blocked while the current token cycle is forfeited (the existing subscription stays valid, but new borrowing is suspended until a fresh ₹500 token is paid, §6.6), and a refunded or reversed token does not satisfy it either: a fresh token is collected first.
- A subscription start date before today is a backdated operation and needs a reason and an additional owner confirmation.
- Plan prices and durations are intended to be editable for future subscriptions only; the editing screen is deferred to the settings phase (the two plans are seeded until then).
- Expiry reminders are emailed automatically and appear on the dashboard.
- Existing loans stay visible after expiry and must be resolved before token refund or membership closure.

### 6.6 Refundable security token
- ₹500, collected once per **token cycle** (first joining, and again on each rejoining), separate from subscription revenue, carried forward across ordinary renewals.
- Refunded on eligible exit (cash or UPI, with optional reference) when no book is outstanding and the token has not been forfeited. No partial refund. An eligible full refund remains possible after the member is anonymised; anonymisation never restores borrowing or renewal. A forfeited token is never refundable or automatically restored.
- Separate immutable ledger entries represent collection, carry-forward, refund and forfeiture; corrections are reversals/adjustments.
- Token collection is not revenue; forfeitures and refunds are reported separately.

**Forfeiture events (full ₹500 each):**
1. **Overdue.** Grace period = 7 calendar days after the due date (configurable setting). Example: due 10 May → grace 11–17 May → day 8 = 18 May. On day 8 forfeiture is **automatic**, needs no owner confirmation, and applies even if the book is returned on day 8. It is recorded in the ledger and audit log. If the Dell was off, it is processed at the next launch with the **original effective date (day 8)**; the actual processing timestamp is also recorded. The owner sees the grace end date and forfeiture status on each loan.
2. **Damaged return.** The owner classifies the copy Damaged and **confirms** forfeiture; a reason is mandatory; audited; copy removed from circulation.
3. **Poor-condition return.** The owner classifies the copy Poor and **confirms** forfeiture with a **mandatory reason**; audited. The copy's condition is Poor and its status is Withdrawn: removed from circulation and unavailable. Poor and Damaged are separate classifications.
4. **Lost book.** The owner marks the book Lost and **confirms** forfeiture with a mandatory reason; audited; the loan closes with a Lost outcome and the copy becomes Lost. No replacement charge is defined. If the book is later recovered the forfeiture stays; any refund or correction needs a separate owner-approved financial adjustment.

**Grace-period changes.** A change to the grace-period setting applies to existing loans. If it makes a loan *newly* eligible for forfeiture, that forfeiture requires owner confirmation (this differs from normal automatic day-8 processing). The application records the previous and new setting, affected loans, newly eligible forfeitures, the owner's decision and resulting transactions. If the owner **declines**, the forfeiture is **not posted**; a mandatory reason and an audit record are required; the owner is not prompted again for the same event and the automatic day-8 job does not post it later.

**Forfeited token.** After forfeiture the member cannot renew. Their existing subscription remains valid, but **new borrowing is suspended until a fresh ₹500 token is paid**. Paying it keeps the same member ID, starts a new token cycle, restores borrowing and makes the member eligible to renew again. The old forfeiture remains in the ledger and is never restored. This is separate from the owner-managed member status (suspended/archived).

### 6.7 Borrowing, returns and renewals
**Issue rules (server-enforced).** One active loan per member; one active loan per copy; default due date 30 days after issue. The due date (at issue or renewal) must be **on or before the member's last valid subscription day**; the first expired day is never a valid due date. **New loans are blocked starting 15 full calendar days before the last valid day** (last valid 10 May → blocked from 25 April). These two rules are independent and both apply. Issue is also blocked when the member is suspended or archived, the subscription has expired, a book is outstanding, or the copy is not Good + Available. Condition is recorded at issue.

**Renewal rules.** The owner records approved renewals. Unlimited; each adds 30 days to the existing due date. **No renewal once the due date has passed** (grace does not allow renewals). A renewal is blocked if the new due date would be after the last valid day or the membership is not valid through it; the owner renews the membership first. Each renewal keeps old/new due dates, timestamp and notes.

**Phase 4 clarifications (owner-confirmed, OPEN_QUESTIONS D-17 to D-24; domain events are stored durably in an outbox, D-23, delivery is Phase 5b; outbox events are business facts, never delivery state):** the loan rules use the last valid day of the member's contiguous membership; the owner may explicitly choose an earlier due date at issue; past loan dates need a reason and extra confirmation; automatic and owner-confirmed forfeitures never double-post.

**Return rules.** The owner confirms the copy and its condition. Damaged, Poor and Lost outcomes follow §6.6; Good copies become Available. Full history is retained; loan history is never deleted. Unreturned books stay overdue, the copy stays unavailable, the member is "outstanding", and token refund or closure is blocked until resolved.

**Backdating.** The owner may enter any past issue or return date. Every backdated operation requires a mandatory reason and an audit entry; if the date affects financial transactions, additional owner confirmation is required. The event date, the financial effective date and the processing timestamp are kept distinct.

### 6.8 Payments, receipts and financial ledger
- Methods: cash and UPI, recorded manually. Split payments: several entries settle one obligation; the full amount remains due until completely paid; activation is blocked until paid.
- Transaction kinds: subscription payment, token collection, token refund, token forfeiture, adjustment, reversal. **No subscription-fee refund exists.**
- Every financial transaction has a **globally unique internal transaction ID (UUID)**.
- **Separate human-readable receipt sequences, each numbered per calendar year:** subscription payments `SUB-YYYY-000001`, token collections `TOK-YYYY-000001`, token refunds `REF-YYYY-000001`, financial adjustments `ADJ-YYYY-000001`. Forfeitures and reversals create their own immutable transaction records (UUID plus a link to the original); no customer receipt is defined for them. A combined registration printout shows both the subscription receipt and the token-collection receipt. Receipt numbers use the **calendar year (January–December, Asia/Kolkata) of the receipt issuance date**; a backdated payment or effective date never changes the receipt-number year. Example: payment effective date 31 Dec 2026, receipt issued 5 Jan 2027 → `SUB-2027-…`.
- Receipts: PDF and print-friendly, with library branding, member ID/name, dates, plan and duration, subscription and token amounts shown separately, payment methods and amounts, notes.
- Financial records are never edited or deleted. Corrections are reversals or adjustments carrying date/time, amount, type, reason, actor, and a link to the original. Subscription payments and paid token refunds cannot be reversed; a recording error is corrected by an owner-approved adjustment. **An adjustment is never a way to refund a subscription fee**: money-out adjustments need the owner's explicit confirmation that they are not a fee refund and cannot reference a subscription payment. An **unlinked cash-out adjustment** is allowed only as an explicit owner-approved accounting correction with a mandatory reason; it records the owner, timestamp and amount, and the financial reports list such adjustments separately and never as fee refunds.
- Limits (owner decision D-16): the largest single amount (including a book's unit cost) is ₹49,999.00; a reason or note may have at most 2,000 characters. Anything above is rejected with a clear message (never shortened or overflowed).
- Receipts store the permanent member ID, not the name (the name is shown live, so anonymisation applies); UPI references are kept for financial reconciliation and are visible only to the logged-in owner.
- Transaction date is distinct from recorded timestamp.

### 6.9 Revenue and financial reports
Two views: (1) **payment-date collections** (actual amounts by payment date, each split component by its own date); (2) **subscription-period allocation**: daily pro-rata across valid subscription days, computed at full precision and rounded only for display; per-day rounded amounts are not stored. Tokens are excluded from subscription revenue. Separate totals: subscription fees collected, tokens collected, tokens refunded, tokens forfeited, adjustments. The allocation view is a management view, not a statutory report.

### 6.10 Reminders and communication
**Email is sent automatically through SMTP.** **Scheduled reminders** run at **09:00 Asia/Kolkata**: subscription expiry reminder **15 days before the expiry date**; overdue reminders on the **due date, day 3 and day 7** (days counted after the due date); subscription expiry notice. **Immediate notifications:** subscription activation, payment receipt, loan issue, loan renewal, book return and token forfeiture — the day-8 forfeiture notification is sent **immediately after forfeiture processing**. **SMTP retries:** the initial email is sent immediately; **retry 1 is 5 minutes after the initial failure, retry 2 is 30 minutes after retry 1 fails, retry 3 is 2 hours after retry 2 fails** (each delay runs from the preceding failed attempt, not cumulatively from the first failure). After the third retry fails the notification is marked **failed** and the error details are retained; there is no further retrying. Retries reuse the same notification record, so they never create duplicate emails; the same event never produces more than one email (duplicate prevention). A member with no email address is recorded as **skipped with the reason `no email address`**. Statuses: Prepared, SMTP accepted, SMTP failed, Skipped; SMTP accepted ≠ delivered or read. All scheduled times are in Asia/Kolkata. SMTP settings come from the environment; a test-email action is provided. Failed emails show on the dashboard.
**WhatsApp is manual.** The application generates a prefilled message for the selected member's number and opens WhatsApp; the owner reviews and sends. Never automatic. Templates (welcome, expiry, due, overdue, payment, refund) use safe placeholders and are editable.

### 6.11 Reports and exports
Reports: members and subscriptions; subscription collections; revenue allocation; token collections, refunds and forfeitures; financial adjustments; outstanding and overdue loans; lost and damaged copies; inventory and circulation; member history and statement; borrowing statistics. Date filters: today, week, month, quarter, year, custom. Formats: CSV, XLSX, PDF where applicable. **Full export** is a ZIP containing CSV files, Excel files and a manifest.

### 6.12 Settings
Library name, logo, address, contact; receipt footer; plan prices and durations (future subscriptions only); loan duration; token amount (future cycles); grace period (applies to existing loans, with the confirmation safeguard in §6.6); reminder templates; theme; dashboard sections and attention criteria; barcode label layouts; backup time and Drive folder; SMTP test. Settings never alter historical subscription prices, token obligations or past dates.

## 7. Design
The owner's brand references define the look; final fonts have not been supplied and the lettering artwork is not a font file. Direction: editorial minimal + modern SaaS + literary luxury; calm, legible, efficient. Light and dark themes; design tokens for colours and typography so they can be replaced; accessible contrast, visible focus, keyboard-friendly, clear form errors; dense tables with search, sort, filter, pagination and empty states; minimal animation; desktop-first. Provisional palette: charcoal, terracotta/burnt orange, cream/parchment, muted green. Decorative botanical artwork only on login, empty states, header and receipts. See §16.

## 8. Technical architecture
Python 3.12; Django 5.2 LTS; PostgreSQL; Django Templates + HTMX; Bootstrap 5 (token-customised); Chart.js; Django ORM and migrations; Excel and PDF libraries; Google Drive API for backups. Modular monolith. No React, microservices, Redis, Celery; Docker not required. Details: `docs/TECHNICAL_ARCHITECTURE.md`.

**Runtime and deployment.** Local-first on the Dell (Windows 11), localhost-only, no LAN. Configuration via environment variables; no hard-coded paths or hostnames; portable PostgreSQL backups allow later cloud migration. The macOS MacBook is a separate development environment. Before launch a concise local-vs-cloud decision memo (requirements, risks, backup implications, migration effort) is presented.

**Capacity.** 50 members and 200 editions comfortably; paginate; avoid N+1 queries; index identifiers and search fields; minimal background processes.

## 9. Data model
See `docs/ERD.md`. Key integrity rules: unique permanent member ID; unique mobile; unique copy ID and barcode; one active loan per member and per copy; one category per edition; non-overlapping subscriptions; one token collection per token cycle; no double refund or forfeiture; money in integer paise; no cascade deletion of history; immutable financial records.

## 10. Backup, restore and data portability
- Daily backup at a fixed configured time; a catch-up backup runs at the next launch if the Dell was off; the dashboard warns if the last success is older than 26 hours and shows failures.
- Pipeline: `pg_dump -Fc` → media files → manifest → AES-256-GCM → SHA-256 → Google Drive upload → upload verification → retention (30 daily, 12 monthly; daily cleanup never deletes monthly backups).
- **Key:** randomly generated; the working key is held in Windows Credential Manager on the Dell. The owner saves a separate recovery key and never stores it with the backups. Losing the recovery key may make backups unrecoverable; this is stated plainly.
- **Google Drive:** the owner connects their Google account and selects the folder; `drive.file` scope; a replacement computer needs fresh OAuth consent.
- **Replacement-computer recovery:** install prerequisites; obtain the encrypted backup and the recovery key; owner explicitly confirms key restoration; restore the working key into Windows Credential Manager; verify key access and backup integrity; restore database and media; check compatibility and migrations; verify the application. Credential Manager entries do not transfer automatically. Restore is CLI-first with explicit confirmation and a pre-restore backup.
- Recovery is not declared verified until a restore on a clean replacement machine has been performed.
- Full data export as in §6.11.

## 11. Auditability
Audited: member create/edit/status/archive/anonymise; subscription create/renew/cancel; payments, reversals, adjustments; token collection, refund, forfeiture (automatic and confirmed); copy create/edit/status/condition; loans, renewals, returns; backdated operations; grace-setting changes and confirmations; backups and restores; login events (including failures) and security-sensitive settings. Entries record actor, action, entity, timestamp and a safe summary, without secrets.

## 12. Acceptance criteria
Demonstrated by automated tests and/or documented manual verification:
1. Only the owner can log in; all routes require login; repeated logins per day are allowed after logout or session loss; sessions end at day end.
2. Five failed logins in 15 minutes are rate-limited without locking the account; success resets the counter.
3. Guided registration creates a permanent unique member ID; duplicate mobile numbers are rejected.
4. Plans calculate dates per §6.5 (all four example rows) and retain historical prices.
5. Split cash/UPI entries settle one obligation; activation requires full payment and the token.
6. Token collections are excluded from subscription revenue.
7. Early renewal starts on the first expired day; late renewal on the entered activation date; at most one scheduled subscription; no overlap.
8. New loans are blocked from last valid day − 15 and after expiry; due dates never exceed the last valid day; first expired day rejected.
9. One active loan per member and per copy; only Good + Available copies issue.
10. Renewals add 30 days, are blocked after the due date, and are blocked beyond the last valid day.
11. Day-8 forfeiture is automatic (10 May due → 18 May), applies when returned on day 8, and a missed run is processed at the next launch with effective date day 8.
12. Damaged, Poor and Lost forfeitures require owner confirmation and a reason (a Poor copy becomes Withdrawn); Lost closes the loan Lost; recovered lost books are not auto-reversed.
13. A grace-setting change creates owner-confirmation prompts for newly eligible forfeitures with a full record; a declined forfeiture is not posted, needs a reason and is not prompted again.
14. Token refund is blocked while a book is outstanding; after forfeiture renewal and new borrowing are blocked while the existing subscription stays valid; paying a fresh ₹500 keeps the member ID, starts a new token cycle and restores borrowing.
15. No subscription-fee refund path exists anywhere.
16. Corrections create reversals/adjustments; originals remain; four receipt sequences and unique internal IDs.
17. Backdated operations require reason and audit; financial effects require additional confirmation.
18. Receipts download and print.
19. Automatic emails follow the schedule in §6.10 (09:00 Asia/Kolkata reminders; retries 5 min after the initial failure, 30 min after retry 1, 2 h after retry 2, then marked failed with error kept), never duplicate, record skipped members with a reason, and record Prepared/SMTP accepted/SMTP failed/Skipped; WhatsApp is never sent automatically.
20. Dashboard shows the default sections (no Pending Payments) with configurable visibility and attention criteria.
21. Reports support the date filters; allocation keeps full precision; ZIP export contains CSV, Excel and manifest.
22. Excel import validates and previews, honours per-import category and copy-ID choices, warns on duplicate ISBNs.
23. Daily encrypted backups run, health is visible, 26-hour warning works; retention 30 + 12.
24. A restore, including key restoration into Windows Credential Manager, is verified on a clean machine.
25. The application works in the Dell browser on localhost.
26. No critical rule is enforced only in frontend code.

## 13. Remaining detail questions
All business policy and all specification questions are decided (see `OPEN_QUESTIONS.md`). No decision is outstanding.

## 14. Delivery phases
Phase 0 specification (this document set) → 1 foundation (project, auth, layout, settings, audit) → 2 members and catalogue (+ import) → 3 memberships and finance → 4 circulation → 5 dashboard and communications → 6 reports and exports → 7 backup, recovery, hardening, deployment. Detailed plan: `IMPLEMENTATION_PLAN.md`.

## 15. Definition of done
Requirements and edge cases implemented; server-side validation; database constraints where appropriate; automated tests for normal, invalid and boundary cases; loading/success/error/empty states; audit requirements met; no secrets committed; documentation updated; relevant tests pass; an honest report of what changed, what was tested and what remains.

## 16. Brand and visual identity
The product name is **Coven's Library** (exact apostrophe). "Coffee" never appears in the interface.
Owner-supplied references (preserved unmodified in `assets/brand/references/`): `coven-coffee-wordmark.png` (white script "Coven" on dark green texture), `coven-lettering-sticker.jpeg` (vertical vine lettering), `coven-ornate-logo.jpeg` (cream card, "Coven" in an ornate burnt-orange frame), `botanical-wreath.jpeg` (floral wreath on near-black).
- The lettering is image artwork, not an installable font; no font file or family name has been supplied. The distinctive "Coven" lettering may be used as logo artwork; the lockup reads "Coven's Library" with a clear complementary typeface for "Library". Body and interface text use a legible UI font.
- Display and UI typography are separate replaceable tokens; a real font may replace them later subject to licence and browser compatibility.
- Derived, optimised logo assets are stored separately from the originals. Provisional palette (charcoal, terracotta, cream, muted green) is documented in `docs/TECHNICAL_ARCHITECTURE.md` §15 and is replaceable.
- Decorative artwork must not reduce readability; keep it out of dense operational screens.
