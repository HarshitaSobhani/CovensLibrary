# Claude Code Master Prompt --- Coven Library

You are the lead software engineer responsible for implementing Coven
Library from the accompanying product specification.

## Source of truth

Read `PRD.md` completely before doing any work. Treat it as the source
of truth for product scope and confirmed business rules. Do not invent
missing business rules. If a requirement is ambiguous, record it in
`OPEN_QUESTIONS.md`, explain the impact, and ask for clarification when
it blocks safe implementation.

The owner will separately provide design colours and font references.
When those assets arrive, inspect them and use them as the authoritative
design system. Until then, use replaceable CSS design tokens and do not
hard-code a final brand palette or font family.

## Product context

Coven Library is a private, owner-facing web application for one
physical library. Only the owner logs in. Members do not have accounts.
Initial scale is approximately 50 members and 200 book catalogue
records. The owner uses a Dell laptop with 8 GB RAM and 256 GB storage
to operate the application. Development may occur on an M3 MacBook Air
with 16 GB RAM and 512 GB storage.

Recommended stack: - Python and a currently supported Django release. -
PostgreSQL. - Django Templates and HTMX. - Bootstrap 5, customised
through design tokens. - Chart.js only where charts add value. - Django
ORM and migrations. - Django authentication. - Lightweight Excel and PDF
libraries. - Google Drive API for encrypted backups after owner
configuration.

Use a modular monolith. Do not add React, microservices, Redis, Celery,
Kubernetes, or Docker by default. Do not add dependencies without
explaining why they are needed.

## Non-negotiable engineering rules

1.  Inspect the repository before changing anything. Preserve existing
    work and conventions where sensible.
2.  Do not overwrite or delete user work without explicit approval.
3.  Do not put secrets, API keys, OAuth tokens, passwords, database
    credentials, or private data in source control.
4.  Use environment variables and provide `.env.example` with
    placeholders only.
5.  Use Django ORM; do not concatenate SQL from user input.
6.  Validate all business rules on the server. Frontend validation is
    supplementary.
7.  Use database constraints/transactions to protect critical
    invariants.
8.  Store monetary amounts as `Decimal` or integer paise; never use
    binary floating point for money.
9.  Preserve financial, subscription, loan, and token history. Never
    silently hard-delete transaction history.
10. Use timezone-aware datetimes and a clearly configured library
    timezone.
11. Add tests with every feature. Include boundary and invalid-path
    tests.
12. Do not claim a feature is complete unless it has been implemented
    and verified.
13. Do not use mock data as if it were production data. Clearly label
    demo/seed data.
14. Keep the application lightweight for the Dell runtime environment.
15. Keep local and cloud deployment portable. Do not hard-code
    machine-specific paths or hostnames.
16. Do not enable offline operation; the MVP requires internet.
17. Do not automatically send WhatsApp/email messages. MVP prepares
    editable drafts and logs owner-confirmed status.
18. Do not integrate a payment gateway in the MVP. Payments are manually
    recorded as cash and/or UPI.
19. Never infer delivery from opening a WhatsApp/email link. Use
    "Prepared" and "Marked sent" states.
20. Before each phase, state the intended files/modules and acceptance
    criteria. After each phase, run relevant tests and report results.

## First task: specification audit, not implementation

Start by: 1. Listing repository files and reading existing
documentation. 2. Reading `PRD.md` completely. 3. Comparing current
repository state with the PRD. 4. Creating `OPEN_QUESTIONS.md` for
unresolved decisions. 5. Creating `IMPLEMENTATION_PLAN.md` with small,
testable phases. 6. Proposing the initial Django app boundaries and data
model. 7. Producing a Mermaid ER diagram in `docs/ERD.md`. 8. Producing
a concise risk register in `docs/RISKS.md`.

Do not start coding business features until the owner approves the
specification audit and the critical open decisions.

## Critical open decisions --- do not guess

The PRD identifies these unresolved points: - Exact overdue grace period
and whether it uses calendar days. - Whether a damaged return
immediately forfeits the token or requires explicit owner confirmation;
recommended: explicit confirmation and reason. - Whether Poor-condition
copies remain borrowable. - Exact subscription expiry-date convention
for six- and twelve-month plans. - Whether a loan renewal may extend
beyond subscription expiry; conservatively block pending
clarification. - Email draft (`mailto:`) versus configured SMTP
sending. - Google Drive OAuth/API configuration and backup folder. -
Local versus cloud production hosting. - Owner password recovery
procedure. - Meaning of "200 books" in dashboard counts:
catalogue/edition records or physical copies.

Implement these as clearly identified configuration/policy points only
where safe. Do not silently choose a business answer. In particular, do
not implement token forfeiture until grace period is explicitly
configured.

## Suggested Django app boundaries

Keep app boundaries practical, not excessively granular. A reasonable
initial structure: - `core`: base layout, health checks, shared
utilities, settings. - `accounts`: single-owner authentication and login
activity. - `members`: member profiles, status, membership IDs. -
`catalogue`: categories, editions/books, physical copies,
acquisitions/donations, copy events, Excel import. - `memberships`:
plans and member subscriptions. - `finance`: payment obligations, split
payment entries, receipts, refunds, reversals, token ledger. -
`circulation`: loans, renewals, returns, overdue status, condition
checks. - `communications`: templates, reminder generation, message
logs. - `reports`: reports, exports, member statements. - `backups`:
encrypted backup creation, Google Drive upload, retention, restore
workflow. - `audit`: immutable audit events.

You may consolidate apps if the repository or Django design makes that
cleaner. Explain the reasoning.

## Core domain invariants

Design and test these invariants: - One owner account only for the
MVP. - Member ID is unique and permanent. - Member mobile number is
mandatory and unique. - One category per book/edition. - Each edition is
a separate book record. - A book/edition can have multiple physical
copies. - Each copy has a unique generated copy ID and barcode. - ISBN
is optional. - Donated books become Coven property and have the same
lending eligibility. - One active loan per member. - One active loan per
physical copy. - One current subscription at a time; historical
subscriptions are retained. - No subscription period overlap for a
member. - Subscription plan and price are snapshotted on the
subscription record. - Six-month plan is ₹800; annual plan is ₹1,500,
until changed for future subscriptions. - Mandatory ₹500 token is
collected once per member, not once per renewal. - Token is not
subscription revenue. - Token collection/refund/forfeiture are separate
ledger events. - No partial token refund. - Damaged book: full token
forfeiture, subject to the owner-confirmation decision in
OPEN_QUESTIONS. - Lost book: token forfeiture only; no additional
replacement charge under current policy. - Overdue token forfeiture only
after configured grace period. - New book issue is blocked in the final
15 days of subscription and after expiry. - New loan due date cannot
exceed subscription expiry. - Issue duration defaults to 30 days; owner
can adjust due date within policy. - Unlimited renewals; each renewal
adds 30 days to the existing due date, subject to the unresolved
subscription-end rule. - Return records condition. - Unreturned books
remain overdue and block token refund/membership closure. - Financial
corrections use reversals/adjustments; originals remain. - Split
cash/UPI payments can settle one obligation and appear on one receipt. -
Subscription activates only after full subscription fee settlement and
token collection. - Early subscription renewal begins after current
expiry; late renewal begins on payment/activation date. - No online
payment gateway in MVP. - No member portal, reservations, or member
accounts.

Use database transactions and appropriate uniqueness constraints/partial
unique constraints to prevent race conditions and duplicate active
loans. If the selected database or Django version makes a constraint
unsupported, document the alternative and test it.

## Required workflows

### Owner registration of a member

A guided workflow: 1. Enter member details. 2. Select plan. 3. Record
subscription payment components (cash/UPI split allowed). 4. Record
mandatory ₹500 token collection. 5. Generate permanent member ID. 6.
Generate receipt. 7. Activate membership only after required settlement.
Handle validation errors without losing entered form data.

### Subscription renewal

1.  Find member.
2.  Display current/expired subscription and token state.
3.  Owner selects six-month or annual plan.
4.  Record full payment, including split payment components.
5.  Schedule start after current subscription if renewed early; start on
    payment/activation date if already expired.
6.  Generate receipt.
7.  Preserve prior subscription and price snapshots.

### Issue a book

1.  Select member and available physical copy.
2.  Show subscription status, expiry, token status, and active-loan
    status.
3.  Record issue condition.
4.  Default due date to 30 days after issue date.
5.  Permit owner date adjustment only within policy.
6.  Enforce every issue invariant server-side.
7.  Commit loan and copy status atomically.

### Renew a loan

Members may contact the owner by phone, text/WhatsApp, or email. The
owner manually records the approved renewal. There is no member-facing
renewal request system. 1. Open active loan. 2. Verify renewal
eligibility. 3. Add 30 days to existing due date. 4. Store old and new
due dates, timestamp, and notes. 5. Preserve the full renewal history.

### Return a book

1.  Select active loan.
2.  Record return date and condition.
3.  Apply damaged/lost/overdue policy only after required confirmation
    and configured rules.
4.  Update copy availability/status.
5.  Close loan and preserve history.
6.  Update outstanding-book/token eligibility.

### Financial correction

1.  Open original transaction.
2.  Create reversal or adjustment with reason.
3.  Preserve original transaction.
4.  Recompute derived balances safely.
5.  Write audit event.

### Excel import

1.  Upload template-compatible workbook.
2.  Parse and validate books and copies.
3.  Detect duplicate/invalid rows.
4.  Show preview and row-level errors.
5.  Require owner confirmation.
6.  Import atomically or in a safely staged process.
7.  Provide error report and import summary.

### Backup and restore

1.  Create database backup.
2.  Encrypt before upload.
3.  Upload to configured Google Drive destination.
4.  Verify upload/integrity.
5.  Apply retention: 30 daily and 12 monthly.
6.  Log success/failure.
7.  Restore requires explicit confirmation and a pre-restore backup.
8.  Test restore in a non-production environment before declaring the
    feature complete.

## UI requirements

-   Owner-only desktop-first dashboard.
-   Libly Space-inspired information hierarchy.
-   Combined editorial minimal / modern SaaS / literary luxury
    direction.
-   Await owner-provided colours and fonts; use CSS variables until
    supplied.
-   Light and dark themes.
-   Rearrangeable dashboard widgets with persisted layout.
-   Sidebar: Dashboard, Members, Books & Inventory, Borrowing & Returns,
    Subscriptions, Payments & Tokens, Reminders, Reports & Analytics,
    Settings.
-   Global search across member, book, copy, ISBN, and transaction
    identifiers.
-   Calendar view for due dates, subscription expiries, and reminders.
-   Dense tables with search, filters, sorting, pagination, empty
    states, and clear status labels.
-   Accessible contrast, keyboard navigation, focus states, and clear
    validation messages.
-   Avoid excessive animation and unnecessary UI dependencies.

## Reports

Implement both: 1. Payment-date collections: actual amounts received
grouped by each payment component's receipt date. 2. Subscription-period
allocation: subscription fee allocated across the membership service
period for management reporting.

Exclude refundable tokens from subscription revenue. Report token
collections, refunds, and forfeitures separately. Clearly label the two
reporting bases; do not present subscription-period allocation as a
statutory accounting statement.

Required reports: - Member borrowing history. - Book/copy borrowing
history. - Subscription history. - Revenue/collections. - Inventory and
availability. - Lost/damaged/withdrawn copies. - Overdue loans. - Most
borrowed books/authors/genres. - Never-borrowed books. - Borrowing
frequency. - Member retention/renewal. - Full member statement.

Date filters: today, week, month, quarter, year, custom range. Exports:
CSV, Excel, PDF where appropriate.

## Testing requirements

Use Django's test framework or an appropriately justified test tool.
Include: - Model tests. - Service/business-rule tests. - View/form
tests. - Permission/authentication tests. - Import validation tests. -
Receipt and financial ledger tests. - Backup/restore tests. - Regression
tests for every bug fixed.

At minimum, test: - Duplicate member mobile. - Unique
member/copy/barcode/transaction/receipt IDs. - Overlapping subscription
rejection. - Early and late renewal date calculations. - Final 15-day
issuance block. - Expired subscription issue block. - Due date beyond
subscription expiry. - One active loan per member and per copy. -
Unavailable copy issue block. - Renewal adds 30 days. - Overdue grace
period boundary. - Damaged and lost token rules. - Token refund blocked
with outstanding loan. - Split cash/UPI settlement and single receipt. -
Token excluded from subscription revenue. - Reversal does not erase
original transaction. - Excel duplicate and invalid row handling. -
Backup encryption, retention, and restore verification. - Owner-only
access to all private routes.

## Phase execution protocol

Work in small phases. Do not attempt the entire application in one
enormous code change.

For each phase: 1. State the objective and requirements covered. 2. List
files/modules expected to change. 3. Identify data migrations. 4.
Implement the smallest coherent slice. 5. Add or update tests. 6. Run
formatting, linting/type checks if configured, migrations checks, and
tests. 7. Inspect the diff for accidental changes and secrets. 8. Report
test commands and actual results. 9. Update implementation documentation
and checklist. 10. Stop and request approval before proceeding to the
next major phase if the phase changes business rules or schema in a way
that depends on an unresolved decision.

Never claim tests passed if they were not run. If a tool or environment
is unavailable, report that limitation.

## Required repository documentation

Maintain: - `CLAUDE.md` --- durable project instructions and commands. -
`PRD.md` --- product requirements. - `OPEN_QUESTIONS.md` --- unresolved
product decisions. - `IMPLEMENTATION_PLAN.md` --- phases and status. -
`docs/ERD.md` --- Mermaid ERD and relationship notes. - `docs/RISKS.md`
--- risks and mitigations. - `docs/DEPLOYMENT.md` --- local/cloud setup,
environment variables, backup and restore. - `docs/OPERATIONS.md` ---
daily owner workflows and recovery procedures. - `.env.example` ---
placeholders only.

## Final instruction for the first session

Do not begin feature implementation immediately. Perform the
specification audit first, produce the documentation listed under "First
task", and return: - Summary of confirmed requirements. - Open questions
requiring owner decisions. - Proposed app boundaries and ERD. - Risks,
especially local hosting and backup recovery. - Recommended phase
plan. - Exact commands to set up and run the project on macOS
development and Dell runtime environments.

Wait for approval before implementing Phase 1.


## Owner-supplied brand references

The owner has supplied two image references:
1. Botanical wreath illustration: orange and cream artwork on a near-black background.
2. “Coven Coffee” wordmark: white lettering on a dark textured green background.

Treat these as visual direction. The product name remains **Coven Library**. Do not display “Coven Coffee” as the application name or assume that the supplied wordmark is the final Coven Library logo unless the owner explicitly confirms this.

Explore a restrained palette based on near-black/charcoal, burnt orange/terracotta, warm cream/parchment, and muted botanical green. Build replaceable CSS design tokens and test accessible contrast in light and dark themes. Use the botanical illustration sparingly; keep dense tables and operational screens clean and readable.

No actual font files or font-family names have been supplied yet. Do not claim that the font has been identified from the logo image. Keep typography tokens configurable and ask for font files/names before finalising typography. Preserve original reference images in `assets/brand/references/` once the owner adds them to the repository.

## Confirmed cross-computer recovery requirement

The owner has confirmed that recovery on another computer must be possible. Google Drive backups are therefore not merely archival: they are the recovery mechanism if the Dell fails or is replaced.

Implement and document:
- Encrypted daily database backups uploaded to Google Drive.
- Retention of 30 daily backups and 12 monthly backups.
- Backup integrity verification and visible success/failure status.
- A restore workflow that can provision the application on a replacement computer, restore the database, restore required uploaded assets, and reconnect configuration safely.
- A recovery guide that explains prerequisites, installation, environment configuration, decryption-key handling, Google Drive authorization, restore, migration checks, and verification.
- A periodic restore test on a separate/non-production environment.

Do not store the encryption key beside the encrypted backup or upload it unencrypted to the same Drive folder. The owner must retain the key securely in a separate location, such as a password manager or offline recovery record. If the key is lost, encrypted backups may be unrecoverable; explain this clearly in the operator documentation.

Do not assume Google Drive alone makes the application instantly runnable on another machine. The recovery package must also document the supported Python version, dependency installation, PostgreSQL setup, environment variables, media/assets restoration, and Django migration procedure. Do not back up secrets in plaintext. Do not claim a recovery test passed until a real restore has been performed and verified.


## Latest confirmed operating decisions

- Overdue grace period is **seven calendar days after the due date**. Example: due 10 May; grace 11–17 May; if still unreturned on 18 May, the full ₹500 token forfeiture condition is met. Use the library timezone and show the grace-period end date.
- A book returned Damaged causes full ₹500 token forfeiture. Require owner confirmation and a reason/audit record.
- A Poor-condition book is removed from circulation and cannot be issued.
- SMTP email sending is selected. Configure SMTP host, port, TLS/SSL mode, sender, and credentials through environment variables or protected settings. Never commit credentials. Provide a test-email action and useful failure handling without exposing secrets.
- Local deployment first. Keep the project portable for later cloud migration.
- Recovery on a replacement computer is required using encrypted Google Drive backups.

## Two business rules awaiting explicit confirmation

Do not silently assume these recommendations are approved:
1. **Subscription date convention:** recommended start date inclusive; last valid date is the day before the six-/twelve-month anniversary; anniversary date is the first expired day. Example: a six-month subscription starting 11 November 2026 is valid through 10 May 2027 and expires on 11 May 2027.
2. **Loan renewal near subscription expiry:** recommended to block a renewal if adding 30 days would make the new due date later than subscription expiry. Owner renews the membership first, then processes the loan renewal.

For the first implementation session, list these two as decisions requiring owner confirmation. Do not build hidden assumptions into date logic. If the owner approves them, encode the policy in tests and centralised service functions rather than duplicating date arithmetic across views.


## Final product name and brand assets

The official product name is **Coven's Library**. Use this exact name in the application, login screen, navigation, page titles, receipts, and system-generated messages. “Coven Coffee” is only wording in a supplied reference image; do not use “Coffee” in the application.

The owner supplied visual references:
- “Coven Coffee” wordmark on dark textured green.
- Vertical decorative Coven lettering/sticker.
- Cream card with Coven lettering inside an ornate burnt-orange frame.
- Previously supplied orange/cream botanical wreath illustration on near-black.

Incorporate the distinctive **Coven lettering style** into the product's brand wordmark. The supplied images are lettering artwork, not an installable font file. Do not claim an actual font file has been supplied and do not attempt to synthesize a complete font from the images. Use the lettering artwork as a logo/wordmark image where suitable; use a legible complementary UI font for body text, forms, and dense tables. Keep brand/display lettering and UI typography as separate configurable design tokens. If the owner supplies a real font file or font-family name later, verify its licence and browser compatibility before use.

Visual direction: near-black/charcoal, burnt orange/terracotta, warm cream/parchment, muted botanical green, dark textured surfaces, ornate frame details, and restrained hand-drawn botanical accents. Prioritise contrast, accessibility, and efficient owner workflows. Decorative art should not interfere with dense operational screens.

Preserve original image references under `assets/brand/references/`. Do not overwrite originals. Create appropriately sized/optimised derived logo assets for actual UI use. The final logo lockup should read **Coven's Library**: Coven lettering for “Coven's” and a complementary readable typeface for “Library”.
