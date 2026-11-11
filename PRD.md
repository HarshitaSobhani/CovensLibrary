# Coven Library --- Product Requirements Document

**Version:** 1.0\
**Status:** Draft for implementation\
**Product type:** Private, owner-facing library management web
application\
**Primary operator:** One library owner\
**Initial scale:** Up to 50 members and approximately 200 catalogue
entries\
**Development workflow:** Claude Code\
**Initial deployment direction:** Local application accessed through the
Dell laptop's browser; cloud migration may be considered later.

------------------------------------------------------------------------

## 1. Executive summary

Coven Library is a private web application used only by the library
owner to manage a physical lending library. Members do not receive
application accounts and cannot access the system. The owner creates and
maintains member records, manages the catalogue and individual physical
copies, records subscriptions and payments, issues and receives books,
handles renewals, tracks refundable security tokens, and reviews
operational and financial reports.

The first release is intended for a small operation: no more than 50
members and approximately 200 books at launch. The system should be
simple to operate, reliable, auditable, and designed so that a later
move from local hosting to cloud hosting does not require a rewrite.

The initial application is online-dependent. Offline operation is
explicitly out of scope.

## 2. Goals

1.  Maintain one reliable source of truth for members, subscriptions,
    books, copies, loans, payments, and token balances.
2.  Make common owner workflows fast: register a member, record payment,
    issue a book, process a return, and renew a membership or loan.
3.  Prevent invalid operations through business-rule validation rather
    than relying on the owner to remember every rule.
4.  Preserve historical records for financial and operational
    accountability.
5.  Provide useful dashboard reminders, reports, exports, receipts, and
    daily encrypted backups.
6.  Use a lightweight architecture appropriate for the library's scale
    and the Dell's 8 GB RAM / 256 GB storage runtime environment.

## 3. Non-goals

The MVP will not include: - Member login, member portal, public
catalogue, public registration, or member self-service. - Multiple
branches or multi-tenant support. - Online payment gateway
integration. - WhatsApp Business API automation. - Member-initiated
in-app reservations or a reservation queue. - Delivery/shipping
workflows. - Mobile applications. - AI recommendations, ratings,
reviews, wishlists, or social features. - Offline application
operation. - Staff accounts or role management.

Members may contact the owner by phone, text, WhatsApp, or email to
request a book-loan renewal. The owner records the renewal in the
application.

## 4. Users, access, and security boundary

### 4.1 User roles

-   **Owner:** The only authenticated application user; full access to
    all application modules and settings.

No member accounts or staff accounts are required.

### 4.2 Authentication

-   Private login page.
-   Secure password hashing using Django's authentication system.
-   Session-based authentication.
-   Logout.
-   Re-authentication after a configurable period of inactivity.
-   Login activity log, including timestamp and outcome where practical.
-   Protect every application view and action behind authentication,
    except login and required static assets.
-   Do not expose member, book, payment, or report data publicly.

### 4.3 Security requirements

-   CSRF protection, secure session configuration, input validation,
    output escaping, and ORM parameterization.
-   Secrets and credentials must be supplied through environment
    variables, never committed.
-   Restrict uploaded files by type and size.
-   Validate imported spreadsheets before committing records.
-   Financial and membership operations must be auditable.
-   Do not log passwords, secrets, or unnecessary personal data.

## 5. Product assumptions and confirmed business facts

  -----------------------------------------------------------------------
  Area                                Confirmed requirement
  ----------------------------------- -----------------------------------
  Access                              Owner only

  Platform                            Web application

  Physical library                    Yes

  Initial members                     Maximum 50

  Initial catalogue                   Approximately 200 books

  Book ownership                      Coven-owned books plus donated
                                      books; donated books become Coven
                                      property

  Member registration                 Owner enters details

  Member identity                     Permanent unique membership ID

  Member phone                        Mandatory and unique

  Member records                      Individual memberships, no family
                                      accounts

  Member profile                      Name, mobile, email, joining date,
                                      notes

  Subscription plans                  ₹800 / 6 months; ₹1,500 / 12 months

  Free tier                           None

  Benefits                            Same borrowing benefits for both
                                      plans

  Subscription auto-renewal           No

  Membership changes                  No upgrade/downgrade workflow;
                                      owner selects a plan for a new
                                      subscription

  Refundable token                    ₹500, mandatory, collected once per
                                      member

  Borrowing limit                     One active book per member

  Borrowing duration                  30 days

  Renewals                            Unlimited, owner-recorded; each
                                      renewal adds 30 days

  Reservations                        No reservation system

  Payments                            Cash and UPI; manually recorded

  Split payments                      Supported; multiple payment entries
                                      against one obligation and one
                                      receipt

  Receipt                             PDF download and print

  Communication                       Email and WhatsApp message
                                      generation; owner sends

  Reminders                           Dashboard is sufficient; no daily
                                      email summary

  Dashboard                           Libly Space-inspired; light and
                                      dark themes; rearrangeable widgets

  Reports                             Advanced reports, date filters,
                                      CSV/Excel/PDF exports

  Backups                             Daily encrypted backups to Google
                                      Drive; retain 30 daily and 12
                                      monthly

  Runtime                             Dell laptop, browser access,
                                      library premises

  Internet                            Required for application use

  Import                              Excel book and copy import

  Export                              Full application data export

  Future hosting                      Local initially is being
                                      considered; cloud later may be
                                      considered
  -----------------------------------------------------------------------

## 6. Functional requirements

### 6.1 Owner dashboard

The dashboard should show: - Total members. - Active members. -
Inactive/archived members, where useful. - Subscriptions expiring within
15 days. - Total catalogue titles/editions and total physical copies. -
Available copies. - Borrowed copies. - Overdue loans. - Monthly
collections. - Recent transactions. - Pending operational actions and
reminders.

The dashboard should: - Support light and dark themes. - Allow the owner
to rearrange widgets and persist their layout. - Include a global search
for member name, mobile number, membership ID, book title, author, ISBN,
copy ID, and transaction/receipt ID. - Include a calendar view for
subscription expiries, book due dates, and reminder dates. - Provide
quick actions for adding a member, adding a book, issuing a book, and
recording a return.

### 6.2 Member management

Member fields: - Unique, permanent membership ID, generated by the
application (suggested format `COVEN-0001`). - Full name. - Mobile
number (required, unique). - Email address (optional unless later
decided otherwise). - Joining date. - Additional notes. - Status:
active, suspended/inactive, archived.

Requirements: - Owner can create, view, edit, suspend/reactivate,
archive, and search members. - Registration is a guided workflow: member
details → plan selection → subscription payment entries → refundable
token collection → confirmation/receipt → activation. - A member can
have only one current subscription at a time, but the system must retain
all historical subscription records. - Membership ID remains unchanged
across renewals. - Reactivation requires a payment record and a valid
subscription; do not silently reactivate without the required financial
record. - Archiving preserves history and prevents new borrowing. -
Permanent deletion must be controlled. It must not cascade-delete loans,
subscriptions, receipts, payment records, or token ledger entries. If
historical/financial records exist, prefer archive or anonymisation
rather than hard deletion. Any permitted deletion must require explicit
confirmation and be audited. - A member leaving early is handled
manually by the owner. The refundable token is refunded, subject to the
outstanding-book and token rules. Subscription-fee refund policy is
owner-decided and must be recorded as an explicit refund or adjustment
rather than silently edited.

### 6.3 Book catalogue and inventory

The chosen model is **each edition is a separate book record**, with
individual physical copies belonging to that edition.

Book/edition fields: - Title (required). - Author (required unless owner
explicitly permits an unknown author). - Publisher. - Edition. -
Publication year. - One category (no multiple categories). - Additional
notes. - ISBN (optional). - Acquisition source: purchased or donated. -
Donor details where applicable. - Acquisition date and cost where
applicable. - Archived flag/status.

Physical-copy fields: - Unique generated copy ID. - Barcode value and
printable barcode. - Condition: Good, Poor, Damaged, Lost. -
Availability/status: Available, Borrowed, Damaged, Lost, Withdrawn. -
Shelf/rack location (optional). - Copy-specific notes. -
Acquisition/source details if they differ from the title-level details.

Requirements: - One edition can have multiple physical copies. - Each
copy is tracked independently. - Damaged, lost, withdrawn, or borrowed
copies cannot be issued. - If one copy is damaged but another copy is
available, the available copy remains issuable. - Donated books become
Coven Library property and have the same borrowing eligibility as
purchased books. - Owner can archive a book/edition while preserving all
history. - Owner can mark a copy lost, damaged, or withdrawn. - Do not
hard-delete a book or copy that has transaction history. - A copy's
condition must be recorded at issue and return. - Maintain a copy-level
history of acquisition, issue, return, condition changes, and status
changes.

### 6.4 Excel import

-   Import books and physical copies from an Excel workbook.
-   Provide a downloadable sample/template workbook with documented
    columns.
-   Validate file type, required fields, categories, copy identifiers,
    duplicate records, and data types before import.
-   ISBN is optional; if supplied, validate its format and identify
    likely duplicates.
-   Show a preview with valid rows, invalid rows, and actionable error
    messages.
-   Do not partially import silently. Owner must confirm the import
    after reviewing validation.
-   Use a transaction or safe staged import so a failed import does not
    leave inconsistent records.
-   Provide a downloadable error report.
-   Import must support books and copies; category and donor fields may
    be included in the template where applicable.

### 6.5 Subscription management

Plans: - Six-month plan: ₹800. - Annual plan: ₹1,500. - Both plans have
the same borrowing benefits. - Prices must be configurable for future
subscriptions. Existing subscription records retain their original plan
name, duration, and price snapshot.

Subscription fields: - Member. - Plan. - Start date. - End/expiry
date. - Subscription fee. - Status: scheduled, active, expired,
cancelled, or otherwise explicitly defined. - Payment/receipt
association. - Creation date and audit metadata.

Rules: - No automatic renewal. - A new subscription can be created only
when there is no overlapping current subscription. - If renewed before
expiry, the new subscription starts immediately after the current
subscription ends. - If renewed after expiry, the new subscription
starts on the payment/activation date. - Owner chooses the plan at
renewal. - System calculates expiry dates. - Expiry reminders appear on
the dashboard. - A member cannot be issued a new book during the final
15 days of their subscription. - The due date of a new loan must not
extend beyond the subscription expiry date. - Issuance is blocked if the
subscription has expired. - Existing loans remain visible after
subscription expiry and must be resolved before token refund/membership
closure. - Subscription history must be preserved.

### 6.6 Refundable security token

-   Mandatory ₹500 refundable token, collected once per member.
-   Token is separate from subscription revenue.
-   Token is carried forward when the member renews.
-   Token is refunded when the member exits, provided the required
    book-return conditions are met and the token has not been forfeited.
-   Token refund can be recorded as cash or UPI.
-   Owner records refund transaction reference where available.
-   Token ledger must separately represent collection,
    carry-forward/continuity, refund, and forfeiture.
-   No partial token refund.
-   Damaged book: full token forfeiture.
-   Lost book: token forfeiture only; no additional replacement charge
    under the confirmed rule.
-   Overdue book: token forfeiture only after a grace period.
-   **Unresolved:** grace-period length and the exact trigger for
    forfeiture must be configurable or explicitly set before production
    use.
-   Unreturned books remain overdue, the copy remains unavailable, the
    member has an outstanding-book status, and token refund/membership
    closure is blocked until resolved.
-   Do not count token collection as revenue.
-   Token forfeiture must be reported separately from subscription
    income.
-   Every token movement must be represented by an immutable ledger
    entry; corrections use reversal/adjustment entries.

### 6.7 Borrowing, returns, and renewals

Loan fields: - Member. - Physical copy. - Issue date. - Due date. -
Return date. - Condition at issue. - Condition at return. - Loan
status. - Renewal history. - Notes.

Issue rules: - One active loan per member. - One physical copy can have
at most one active loan. - Default due date is 30 days after issue
date. - Owner may adjust due date at issue, but it cannot exceed
subscription expiry. - Block issue if member is suspended, archived, has
an expired subscription, is within the final 15 days of subscription,
already has an active loan, has an unresolved outstanding book, or the
selected copy is unavailable/damaged/lost/withdrawn. - Validate all
rules on the server, not only in the UI.

Renewal rules: - Members may contact the owner by phone, text/WhatsApp,
or email to request renewal. - There is no member-facing request portal
or reservation queue. - Owner records an approved renewal in the
application. - Unlimited renewals are permitted. - Each renewal adds 30
days to the existing due date. - Preserve each renewal event, old due
date, new due date, timestamp, and notes. - Do not permit renewal if it
would violate the subscription end-date rule. If the existing policy is
unclear for a renewal close to expiry, show a clear validation message
and require an owner decision/policy clarification rather than silently
extending beyond eligibility.

Return rules: - Owner confirms returned copy and condition. - Record
return date and condition. - If condition is damaged, apply the
full-token-forfeiture rule, with explicit owner confirmation and a
ledger event. - A returned, good/poor-but-usable copy becomes available
according to owner-selected condition/status policy. - A damaged copy is
unavailable until status is changed. - Keep full borrowing history,
including renewals. - Never delete loan history.

### 6.8 Payments, receipts, and financial ledger

Payment methods: - Cash. - UPI.

The owner records payments manually. No gateway integration in MVP.

Support: - Subscription fee payments. - Token collection. - Token
refund. - Token forfeiture. - Replacement/other charges only if
introduced later; current lost-book rule is token forfeiture only. -
Refunds. - Reversals and adjustments. - Backdated payment dates. - Split
payments across methods, such as UPI plus cash.

Rules: - A subscription must be fully paid before activation. - Partial
payment entries may be recorded as components of a fully settled
obligation; the system must not treat an incomplete subscription fee as
paid or activate the subscription early. - One receipt can contain
multiple payment entries against the same obligation. - Every financial
transaction receives a unique ID (suggested `COVEN-TXN-00001`). -
Receipt IDs are unique. - Receipt supports PDF download and browser
printing. - Receipt includes library branding, receipt number, member
ID/name, payment date, plan/duration, subscription amount, token amount,
total collected, payment methods and amounts, and notes. - Preserve
transaction date separately from entry/recorded timestamp. - Financial
records are not silently edited or deleted. Corrections use
reversal/adjustment records with reason, timestamp, and audit trail. -
Refunds are recorded as separate transactions. - Do not create a general
outstanding subscription balance workflow: the full required amount must
be collected before activation.

### 6.9 Revenue and financial reports

Provide both reporting views:

1.  **Cash/payment-date view:** groups actual receipts by the date money
    was received. This supports collections and cash-flow review.
2.  **Subscription-period allocation view:** allocates subscription fee
    over the membership service period for management reporting. This is
    not automatically a statutory accounting or tax report.

Keep separate totals for: - Subscription fees collected. - Refundable
tokens collected. - Tokens refunded. - Tokens forfeited. - Other
charges/refunds/adjustments.

For split payments, payment-date reports use the actual date and amount
of each payment component. Subscription-period allocation uses the
subscription fee only, excluding refundable token.

### 6.10 Reminders and communication

Channels: - WhatsApp via prefilled message links. - Email via a
generated message or `mailto:` link, subject to browser/device email
configuration. - No WhatsApp Business API or automated email delivery in
MVP.

Templates: - Membership welcome. - Subscription expiring in 15 days. -
Subscription expired. - Book due tomorrow. - Book overdue. - Payment
confirmation. - Token refund confirmation.

Requirements: - Owner can edit message text before opening the
communication link. - Templates support safe placeholders such as member
name, due date, book title, plan, and amount. - Record reminder
generated/sent status and timestamp. Since opening WhatsApp/email does
not prove delivery, label status accurately (for example, "Prepared" and
owner-confirmed "Marked sent"), not "Delivered". - Daily reminder list
is generated when the application is opened or through a lightweight
scheduled mechanism appropriate to the deployment. - Dashboard is
sufficient; no morning email summary. - Reminder categories include
subscriptions expiring in 15 days, expired subscriptions, upcoming
returns, overdue books, and pending payment actions where relevant.

### 6.11 Reports and exports

Reports: - Member-wise borrowing history. - Book/copy-wise borrowing
history. - Subscription history. - Revenue and collections. - Inventory
availability. - Lost/damaged/withdrawn copies. - Overdue loans. - Most
borrowed books/authors/genres. - Never-borrowed books. - Average
borrowing frequency. - Member retention/renewal statistics. - Member
statement containing subscriptions, payments, borrowing, and token
transactions.

Date filters: - Today, this week, this month, this quarter, this year,
custom range.

Export formats: - CSV, Excel, and PDF where appropriate. - Full
application data export for migration/recovery. - Exports must respect
the same access controls as the application.

### 6.12 Settings

Owner can configure: - Library name, logo, address, contact number,
email. - Receipt footer. - Subscription plan prices and durations for
future subscriptions. - Borrowing duration. - Token amount for future
members (existing member token snapshots remain unchanged). - Grace
period for overdue token forfeiture. - Library policies. - Reminder
templates. - Theme preference. - Backup settings.

Changing a setting must not retroactively alter existing subscription
prices, token obligations, or historical loan dates.

## 7. Design requirements

The owner will upload the preferred design colours and fonts separately.
Treat those assets/instructions as authoritative when provided.

Until the assets arrive: - Use a combination of editorial minimal,
modern SaaS, and literary luxury. - Keep the interface calm, readable,
polished, and operationally efficient. - Provide light and dark
themes. - Use a consistent spacing scale, typography hierarchy,
component library, and design tokens. - Do not invent final brand
colours or fonts; isolate them as design tokens so they can be replaced
when the owner supplies the design guide. - Prioritise desktop/laptop
use at the Dell's display resolution, while retaining responsive layouts
for practical access. - Use accessible contrast, visible focus states,
clear form errors, and keyboard-friendly interactions. - Use tables for
dense operational data, with search, sorting, filters, pagination, and
clear empty states. - Avoid excessive animation and heavy visual
effects.

## 8. Technical architecture

### 8.1 Recommended stack

-   Python 3.12 or a currently supported stable Python release
    compatible with selected Django version.
-   Django (current supported LTS/stable version at implementation
    time).
-   PostgreSQL.
-   Django Templates.
-   HTMX for targeted interactive updates.
-   Bootstrap 5, customised using CSS variables/design tokens.
-   Chart.js for charts.
-   Django ORM and migrations.
-   Django authentication and sessions.
-   A maintained Python library for Excel import/export.
-   A maintained PDF generation approach for receipts and reports.
-   Google Drive API integration for encrypted backup upload, only after
    credentials and consent are configured.
-   Git and GitHub.

Use a modular monolith. Do not introduce React, microservices,
Kubernetes, Redis, Celery, or Docker in the initial implementation
unless a demonstrated requirement justifies them.

### 8.2 Runtime and deployment

The Dell laptop has 8 GB RAM and 256 GB storage and will be used to
access the application. The M3 MacBook Air (16 GB RAM / 512 GB storage)
is the development machine.

The initial hosting decision is not final. The owner is considering
local execution on the Dell initially and cloud hosting later. Design
the app to support both with environment-based configuration and
portable PostgreSQL backups. Do not hard-code local paths, hostnames, or
deployment assumptions.

Internet is required for application operation in the initial release.
Do not build offline mode.

Do not make the Dell a production server by default without documenting
the operational and security trade-offs. Before deployment, present a
concise local-vs-cloud decision with requirements, risks, backup
implications, and migration effort.

### 8.3 Performance and capacity

-   Support at least 50 members and 200 catalogue entries comfortably.
-   Design for growth beyond these initial numbers without
    overengineering.
-   Paginate large tables.
-   Avoid N+1 database queries.
-   Use database indexes for identifiers and common search/filter
    fields.
-   Keep dependencies and background processes minimal.
-   The application should remain usable on the Dell's 8 GB RAM
    environment.

## 9. Data model --- initial entity inventory

Final fields and constraints must be documented in the technical design
before implementation.

Suggested entities: - `OwnerAccount` (or Django user constrained to one
owner). - `Member`. - `Category`. - `BookEdition` (each edition is a
separate book record). - `BookCopy`. - `Acquisition/DonationRecord`
where useful. - `SubscriptionPlan`. - `MemberSubscription`. -
`SecurityDepositAccount` or equivalent member-level token state. -
`SecurityDepositLedgerEntry`. - `PaymentObligation` / `Payment`. -
`PaymentEntry` for split methods. - `Receipt`. - `Loan`. -
`LoanRenewal`. - `CopyConditionEvent` / `InventoryEvent`. -
`ReminderTemplate`. - `ReminderLog`. - `AuditLog`. -
`DashboardPreference`. - `LibrarySetting`. - `BackupRun`.

Important integrity constraints: - Member ID unique and immutable. -
Mobile number unique. - Copy ID unique. - Barcode unique. - One active
loan per member. - One active loan per copy. - ISBN optional; if
present, indexed and duplicate warning/validation should account for
editions and copies. - One category per book edition. - Subscription
periods must not overlap for a member. - Subscription must be fully
settled before activation. - One mandatory token collection per
member. - Token ledger balance cannot be refunded twice or become
negative. - Historical financial records and loan events cannot be
cascade-deleted. - All money stored as decimal/integer paise, never
floating-point.

## 10. Backup, restore, and data portability

-   Daily encrypted database backup to Google Drive.
-   Retain 30 daily backups and 12 monthly backups.
-   Define retention and rotation so monthly backups are not
    accidentally deleted by daily cleanup.
-   Backup should include database data and required uploaded
    assets/configuration that are safe to back up; secrets must be
    handled separately and never uploaded in plaintext.
-   Verify backup integrity and record backup status/time.
-   Show last successful backup and last failure in settings/dashboard.
-   Restore capability is required, with explicit confirmation and
    pre-restore safety backup.
-   Provide full data export.
-   Google Drive API credentials must be configured securely; do not
    invent credentials or bypass OAuth/consent.
-   Recovery on another computer is not yet decided; document as a
    future operational decision, but design backups to be portable.

## 11. Auditability

Audit sensitive actions: - Member create/edit/archive/reactivate. -
Subscription creation/renewal/cancellation. - Payment, refund, reversal,
adjustment. - Token collection/refund/forfeiture. - Book/copy
create/edit/archive/status change. - Loan issue/return/renewal. - Backup
and restore. - Login events and security-sensitive settings changes.

Audit entries should include actor, action, entity, timestamp, and a
safe summary of changed values. Avoid storing secrets or unnecessary
sensitive data.

## 12. Acceptance criteria

The MVP is not complete until all of the following are demonstrated by
automated tests and/or documented manual verification:

1.  Only the owner can log in and access application data.
2.  A member can be created through the guided registration workflow and
    receives a permanent unique ID.
3.  Duplicate mobile numbers are rejected.
4.  The two subscription plans calculate the correct duration and retain
    historical prices.
5.  Split UPI/cash payment entries settle one subscription obligation
    and generate one receipt.
6.  Subscription cannot activate until its full fee is paid and the
    mandatory token is recorded.
7.  Token collection is excluded from subscription revenue.
8.  Early renewal starts after the current subscription; late renewal
    starts on the new activation/payment date.
9.  New issuance is blocked during the final 15 days of subscription and
    after expiry.
10. One member cannot hold more than one active loan.
11. A copy cannot be issued if unavailable, borrowed, damaged, lost, or
    withdrawn.
12. Default loan duration is 30 days; owner may adjust due date subject
    to subscription expiry.
13. Renewals add 30 days and preserve each renewal event.
14. Return records condition and updates copy availability correctly.
15. Overdue loans remain visible and token forfeiture does not occur
    before the configured grace period.
16. Damaged/lost book token rules are enforced and audited.
17. Token refund is blocked while an outstanding book remains
    unresolved.
18. Financial corrections create reversal/adjustment records and do not
    erase original entries.
19. Receipts can be downloaded and printed.
20. WhatsApp/email messages can be edited, prepared, and logged without
    falsely claiming delivery.
21. Reports support required date filters and exports.
22. Excel import validates and previews books/copies before committing.
23. Daily encrypted backups run and backup status is visible.
24. Restore workflow is tested against a backup in a non-production
    environment.
25. The application works on the Dell browser at the chosen deployment
    configuration.
26. No critical business rule is enforced only in frontend JavaScript;
    server-side validation and database constraints are used where
    appropriate.

## 13. Open decisions and implementation blockers

The following must be explicitly resolved or configured before
production:

1.  **Overdue grace period:** exact number of days and whether it is
    calendar days. Do not guess. Make it a required setting with no
    silent default; prevent production activation/forfeiture until
    configured.
2.  **Damaged-book trigger:** confirm whether any return marked Damaged
    causes immediate full token forfeiture, or whether the owner must
    explicitly confirm forfeiture after reviewing the damage.
    Recommended UX: require explicit confirmation and reason.
3.  **Poor condition:** clarify whether a Poor copy is still borrowable
    or must be marked unavailable.
4.  **Subscription end-date calculation:** define whether a
    six-month/annual plan ends on the day before the corresponding
    anniversary or on the anniversary date; implement one consistent
    documented convention.
5.  **Renewal near expiry:** confirm whether a loan renewal may extend
    beyond subscription expiry. The existing new-issue rule says due
    date cannot exceed subscription expiry; apply a conservative block
    until owner confirms otherwise.
6.  **Email workflow:** decide whether email is a `mailto:` draft only
    or whether SMTP sending is required. MVP may use a draft link.
7.  **Google Drive backup:** OAuth account, API project, and backup
    folder must be configured by owner.
8.  **Local vs cloud hosting:** decide before production launch. Keep
    architecture portable.
9.  **One-owner account recovery:** define password reset/recovery
    process without creating staff accounts.
10. **Book/copy count semantics:** define whether "200 books" means
    catalogue records, editions, or physical copies for dashboard
    labels.

## 14. Delivery phases

### Phase 0 --- Specification and setup

-   Confirm open blockers.
-   Establish repository, `CLAUDE.md`, environment variables, code
    conventions, and test strategy.
-   Produce ERD and model specification.

### Phase 1 --- Foundation

-   Django project, PostgreSQL, authentication, base layout, themes,
    navigation, settings, audit log.

### Phase 2 --- Catalogue and members

-   Members, categories, editions, copies, copy history, Excel import,
    search.

### Phase 3 --- Membership and finance

-   Plans, subscriptions, token ledger, split payments, receipts,
    refunds, reversals, financial reports.

### Phase 4 --- Circulation

-   Issue, return, condition checks, renewal, overdue logic,
    grace-period workflow.

### Phase 5 --- Dashboard and communication

-   Dashboard widgets, calendar, reminders, WhatsApp/email draft
    generation, activity feed.

### Phase 6 --- Reports and data portability

-   Reports, exports, member statements, full data export.

### Phase 7 --- Backup, hardening, and deployment

-   Google Drive encrypted backups, retention, restore testing, security
    review, Dell/browser verification, deployment decision.

## 15. Definition of done

A feature is done only when: - Requirements and edge cases are
implemented. - Server-side validation exists. - Database constraints are
present where appropriate. - Automated tests cover normal, invalid, and
boundary cases. - UI includes loading, success, error, and empty
states. - Audit requirements are met. - No secrets or credentials are
committed. - Documentation is updated. - Relevant test suite passes. -
Claude Code reports exactly what it changed, what it tested, and what
remains unresolved.


## 16. Brand and visual reference assets

The owner supplied two visual references:
- An orange-and-cream botanical wreath illustration on a near-black background.
- A “Coven Coffee” wordmark on a dark, textured green background.

Use these as brand-direction references, not as permission to rename the product. The application/product name remains **Coven Library** unless the owner explicitly confirms otherwise.

Visual direction to explore:
- Near-black / charcoal foundations for dark surfaces.
- Burnt orange / terracotta as a primary accent.
- Warm cream / parchment as a supporting light tone.
- Muted botanical green as a secondary accent.
- Hand-drawn botanical illustration as a restrained decorative motif.

Do not sample colours blindly and apply them everywhere. Define accessible design tokens and verify text/background contrast in both themes. Keep the application highly legible and operationally efficient; decorative botanical elements should be limited to login, empty states, headers, or subtle brand placements, not dense data tables.

The supplied wordmark image reads “Coven Coffee”. Do not place that exact wording in the product UI or use it as the Coven Library logo without owner confirmation. Treat it as a lettering/style reference only. No separate font files or font-family specification have been supplied yet; keep typography tokens replaceable and ask the owner for the intended font files/names before finalising typography.

When the owner places the images in the repository, preserve the originals under `assets/brand/references/` and document their filenames in the design system notes. Do not modify or overwrite the source images.


## 17. Confirmed operational decisions — latest update

- **Overdue grace period:** seven calendar days after the due date. If the book remains unreturned after the complete seven-day grace period, the entire ₹500 token is forfeited. A return during the grace period does not trigger overdue forfeiture.
- **Damaged return:** if the copy is returned in Damaged condition, the entire ₹500 token is forfeited. Require explicit owner confirmation and capture a reason/audit event.
- **Poor condition:** a Poor-condition copy is removed from circulation and cannot be issued.
- **Renewal beyond membership expiry:** recommended strict rule: block if the resulting due date exceeds subscription expiry; renew the member's subscription first. This awaits final owner confirmation.
- **Subscription expiry convention:** recommended: subscription start date is inclusive and the last valid date is the day before the six-/twelve-month anniversary. The anniversary date is the first expired day. This awaits final owner confirmation.
- **Email:** SMTP sending is selected. SMTP settings and credentials must be securely configurable; do not commit secrets.
- **Hosting:** local deployment first, with cloud migration supported if needed.
- **Recovery:** restoration on another computer is required using encrypted Google Drive backups.
- **Design:** owner will supply final colours and fonts; existing images are visual references.

### Seven-day grace-period calculation

If a book's due date is 10 May:
- 10 May: due date.
- 11–17 May: seven calendar days of grace.
- 18 May: if still unreturned, the overdue-token forfeiture condition is met.

The implementation must use the configured library timezone and date-based comparisons consistently. The owner should see the grace-period end date and forfeiture status before the forfeiture action is posted.


## 18. Confirmed product name and brand identity

The official application/product name is **Coven's Library**.

The owner supplied three visual references:
- A “Coven Coffee” wordmark on a dark textured green background.
- A vertical decorative Coven lettering/sticker reference.
- A cream card with “Coven” lettering inside an ornate burnt-orange frame.

Brand requirements:
- Use **Coven's Library** as the product name in application navigation, login, page titles, receipts, and relevant system-generated messages.
- The word “Coffee” is not part of the product name and must not appear in the application UI.
- Incorporate the distinctive supplied **Coven lettering style** into the brand identity. Use the supplied lettering/logo artwork as a wordmark or image asset where appropriate.
- Do not claim that a usable font file has been supplied. The images show lettering artwork, not an installable font. For body text and interface controls, use a highly legible complementary font. Keep the display/brand font and UI font as separate configurable design tokens.
- If the owner later supplies an actual font file or font-family name, use it subject to its licence and browser compatibility.
- The botanical wreath, ornate orange frame, cream background, dark green texture, and hand-lettered wordmark are brand references. Use them selectively; do not let decorative artwork reduce readability or usability.
- Preserve source images under `assets/brand/references/` and create optimised derived assets for the web app without modifying the originals.
- The final logo lockup should read **Coven's Library**, not “Coven Coffee”. If a combined lockup is designed, use the Coven lettering for “Coven's” and a clear complementary typeface for “Library”.
