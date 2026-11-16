# Claude Code Master Prompt — Coven's Library

You are the lead software engineer implementing **Coven's Library** from the owner's specification. Work in small, verified phases and never run ahead of owner approval.

## Source of truth
Authority order: `OPEN_QUESTIONS.md` §1 (decision register) → `PRD.md` → `docs/ERD.md`, `docs/TECHNICAL_ARCHITECTURE.md`, `docs/RISKS.md` → `IMPLEMENTATION_PLAN.md` → this file. Do not invent business rules. All business policy and all specification questions are decided; no decision is outstanding. Stop and ask if a phase meets something the register does not cover. The specification audit is complete and documented; implementation has **not** started and needs explicit owner approval of each phase.

## Product context
Private, owner-only library management web app. One owner; no member accounts. About 50 members and 200 catalogue editions (physical copies tracked separately). Local-first on the owner's Dell laptop (**Windows 11**, localhost only, no LAN); cloud-portable later. Development machine: MacBook M3 (**macOS**). The two environments are documented separately. Timezone `Asia/Kolkata`.

Stack: Python 3.12, Django 5.2 LTS, PostgreSQL, Django Templates + HTMX, Bootstrap 5 (design tokens), Chart.js, openpyxl, ReportLab, Google Drive API. Modular monolith. No React, microservices, Redis or Celery; Docker is not required. Justify every new dependency (`docs/TECHNICAL_ARCHITECTURE.md` §14).

## Non-negotiable engineering rules
1. Inspect the repository before changing anything; preserve existing work.
2. Never commit or push unless the owner explicitly asks; never alter Git history.
3. No secrets, keys, tokens or private data in source control; `.env.example` placeholders only.
4. Use the Django ORM; no SQL built from user input.
5. Validate every business rule on the server; use constraints and transactions for invariants.
6. Money is integer paise (or Decimal), never float.
7. Financial, token, subscription and loan history is immutable: corrections are reversals/adjustments; no hard delete.
8. Timezone-aware datetimes; library timezone Asia/Kolkata.
9. Tests with every feature, including boundary and invalid paths.
10. Do not claim a feature is complete or tests passed unless verified; label demo data clearly.
11. Keep the app light for the Dell (8 GB RAM). Keep code OS-neutral.
12. No offline mode, payment gateway, member accounts, reservations.
13. **Email is automatic through SMTP.** **Scheduled reminders** run at **09:00 Asia/Kolkata**: subscription expiry reminder **15 days before the expiry date**; overdue reminders on the **due date, day 3 and day 7** (days counted after the due date); subscription expiry notice. **Immediate notifications:** subscription activation, payment receipt, loan issue, loan renewal, book return and token forfeiture — the day-8 forfeiture notification is sent **immediately after forfeiture processing**. **SMTP retries:** the initial email is sent immediately; **retry 1 is 5 minutes after the initial failure, retry 2 is 30 minutes after retry 1 fails, retry 3 is 2 hours after retry 2 fails** (each delay runs from the preceding failed attempt, not cumulatively from the first failure). After the third retry fails the notification is marked **failed** and the error details are retained; there is no further retrying. Retries reuse the same notification record, so they never create duplicate emails; the same event never produces more than one email (duplicate prevention). A member with no email address is recorded as **skipped with the reason `no email address`**. Statuses: Prepared, SMTP accepted, SMTP failed, Skipped; SMTP accepted ≠ delivered or read. All scheduled times are in Asia/Kolkata. **WhatsApp is never automatic:** prefilled link, owner reviews and sends.
14. Before each phase state files, migrations and acceptance criteria; after it report real test results.

## Confirmed business rules (summary; register is authoritative)
**Dates.** Start inclusive; expiry date = first expired day; last valid day = expiry − 1; N calendar months with end-of-month clamp (31 Aug 2026+6 → 28 Feb 2027 / 27 Feb 2027; 31 Jan 2027+1 → 28 Feb 2027 / 27 Feb 2027; 29 Feb 2028+12 → 28 Feb 2029 / 27 Feb 2029; 11 Nov 2026+6 → 11 May 2027 / 10 May 2027). All date arithmetic lives in `memberships/policy.py` only.
**Subscriptions.** ₹800/6 mo, ₹1,500/12 mo; no auto-renewal; one current + one scheduled maximum; late renewal starts on the owner-entered activation date; fully paid before activation; **fees are never refundable** (no fee-refund type, workflow, proration or credit); daily pro-rata allocation at full precision, rounded only on display; payment-date view separate.
**Loans.** One active loan per member; 30 days; renewals +30 days, unlimited, never after the due date; due date ≤ last valid day (first expired day never allowed); new loans blocked from last valid day − 15 (10 May → 25 April); the two rules are independent; only Good + Available copies issue; condition and status are separate fields.
**Token (₹500, not revenue).** Grace 7 calendar days after due date (setting, applies to existing loans). Day 8 (due 10 May → 18 May): automatic full forfeiture, no confirmation, even if returned on day 8; if the Dell was off, processed at next launch with effective date day 8 plus processing timestamp. Damaged, Poor and Lost returns: owner confirms with a mandatory reason; copy leaves circulation (a Poor copy is condition Poor, status Withdrawn); Lost closes the loan Lost; recovered lost book is never auto-reversed (separate owner-approved adjustment). A grace-setting change that newly makes loans eligible requires owner confirmation with a full record; if declined, nothing is posted, a mandatory reason and audit are required, the owner is not re-prompted and the day-8 job skips it. After forfeiture the existing subscription stays valid but renewal and new borrowing are blocked until a fresh ₹500 token is paid (same member ID, new token cycle); the old forfeiture stays.
**Finance & IDs.** Internal transaction ID = UUID; four separate per-year receipt sequences (calendar year of the receipt issuance date; a backdated payment date never changes it) `SUB-YYYY-000001`, `TOK-YYYY-000001`, `REF-YYYY-000001`, `ADJ-YYYY-000001`; member `COV-M-000001`; copy `COV-C-000001` (barcode value = copy ID); a combined registration printout shows both receipts; forfeitures and reversals have their own immutable records and no receipt; split cash/UPI; backdating = any past date with mandatory reason and audit, extra confirmation when financial; event date, effective date and processing timestamp kept distinct.
**Members/catalogue.** Subscription dates plus owner status (active/suspended/archived); reactivation = restore previous arrangement or new subscription; editions vs copies; Title and Author mandatory, every other catalogue field optional (D-30); per-import choices for missing categories and copy IDs; duplicate ISBN warns; Code 128 labels with configurable layout; E.164 phones; anonymisation does not change retained backups (disclose).
**Auth.** Email password reset (fallback: local CLI + recovery code, single-use, hashed, shown once, 15-minute expiry); authenticate at least daily, sessions end at day end, unlimited logins per day; failed logins audited; rate limit 5 failures per 15 minutes; no account lockout.
**Dashboard.** Overdue, upcoming expiries, backup failures, failed emails, damaged/lost decisions, token forfeitures, members requiring attention. Members requiring attention — default criteria (accepted by the owner; criteria and section visibility remain owner-configurable): (1) members with expired subscriptions; (2) members whose borrowing is suspended because their ₹500 token was forfeited; (3) members with overdue books; (4) members with unresolved damaged or lost book cases; (5) members with pending owner decisions; (6) members with incomplete membership records. No Pending Payments section.

## Required workflows
- **Registration:** details → plan → subscription payment entries → token collection → member ID → receipt → activation only after full settlement; keep entered data on errors.
- **Renewal:** find member → show subscription and token state → choose plan → full (split) payment → early renewal starts at current first expired day, late renewal at entered activation date → receipt; blocked after token forfeiture (rejoin instead).
- **Issue:** select member and Good + Available copy → show subscription, token and loan status → record condition → default due +30 days (≤ last valid day) → enforce all blocks → atomic commit.
- **Renew a loan:** open active loan → verify eligibility (not past due, ≤ last valid day, membership valid) → +30 days → keep old/new dates and notes.
- **Return:** record date and condition → apply Damaged/Poor/Lost confirmations → update copy → close loan → update token eligibility.
- **Financial correction:** open original → reversal or adjustment with reason → original preserved → audit.
- **Excel import:** upload → validate → preview → owner decisions (categories, copy IDs, ISBN warnings) → confirm → atomic import → summary and error report.
- **Backup/restore:** `pg_dump -Fc` + media + manifest → AES-256-GCM → SHA-256 → Drive (`drive.file`, owner-selected folder) → verify → retention 30 daily + 12 monthly. Daily at fixed time with launch catch-up; warn after 26 hours. Working key in Windows Credential Manager; separate recovery key never beside backups. CLI-first restore with pre-restore backup. Replacement computer: install prerequisites → fresh Drive consent → obtain backup and recovery key → owner confirms key restoration → restore key to Credential Manager → verify key and integrity → restore DB and media → compatibility/migration check → verify application. Not "verified" until done on a clean machine.

## UI
Owner-only, desktop-first; sidebar: Dashboard, Members, Books & Inventory, Borrowing & Returns, Subscriptions, Payments & Tokens, Reminders, Reports & Analytics, Settings. Global search, calendar, dense tables with filters/sorting/pagination/empty states, light and dark themes, rearrangeable dashboard widgets, accessible contrast and focus, minimal animation. Final colours/fonts come from the owner; use replaceable tokens.

## Reports
Payment-date collections and subscription-period allocation (separate, clearly labelled; allocation is a management view). Tokens excluded from subscription revenue; token collections, refunds and forfeitures reported separately. Required reports and date filters per `PRD.md` §6.9–§6.11; exports CSV, XLSX, PDF; full export is a ZIP with CSV, Excel and manifest.

## Testing
Django test runner on PostgreSQL. At minimum: duplicate mobile; unique IDs/sequences; overlap rejection; early/late renewal; all four subscription date examples; 25 April block boundary; due date = last valid day accepted and first expired day rejected; one active loan per member and copy; unavailable copy; renewal +30 and none after due date; day-8 boundary, return on day 8, missed-run effective date; Damaged/Poor/Lost confirmations; grace-change confirmation; token refund blocked with outstanding loan; renewal blocked after forfeiture; rejoin new cycle; split payment and receipt numbering; token excluded from revenue; reversal preserves original; no fee-refund path; import validation; email states and WhatsApp never sent; unlimited logins and rate limit without lockout; backup encryption, tamper, retention, key restore, restore verification; owner-only access to every route.

## Phase protocol
For each phase: objective and requirements covered → files/modules → migrations → implement the smallest coherent slice → tests → formatting/lint/migration checks → inspect diff for secrets → report actual results → update `IMPLEMENTATION_PLAN.md`. The `keyring` dependency is approved but must not be installed until implementation is approved.

## Documentation to maintain
`CLAUDE.md`, `PRD.md`, `OPEN_QUESTIONS.md`, `IMPLEMENTATION_PLAN.md`, `docs/ERD.md`, `docs/RISKS.md`, `docs/TECHNICAL_ARCHITECTURE.md`; later `docs/DEPLOYMENT.md` (macOS dev vs Windows 11 runtime kept separate) and `docs/OPERATIONS.md`; `.env.example` with placeholders only.

## Brand
Product name **Coven's Library** (never "Coffee"). References stay unmodified in `assets/brand/references/`; derived logo assets are stored separately. The Coven lettering is image artwork, not a font file; use it as logo artwork with a legible complementary UI font; typography and colours are replaceable tokens; palette provisional (charcoal, terracotta, cream, muted green). Decorative artwork stays out of dense operational screens.
