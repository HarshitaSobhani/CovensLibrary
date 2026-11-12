# Database security — two-role model (L6)

Decision: two PostgreSQL roles on Mac and Windows; `postgres` stays break-glass. Implemented in `core/dbsecurity.py`.

## Roles
| Role | Purpose | Attributes |
|---|---|---|
| `postgres` | Break-glass superuser. Never used by the app, scripts or Task Scheduler. | SUPERUSER |
| `coven_owner` | Owns the database and every object. Runs migrations, grants, restores. | LOGIN, NOSUPERUSER, NOCREATEROLE; CREATEDB on **development machines only** |
| `coven_app` | Runtime. Data privileges only. | LOGIN, NOSUPERUSER, NOCREATEDB, NOCREATEROLE; no CREATE on schema, no TEMP |

## Who may do what
| Operation | postgres | coven_owner | coven_app |
|---|---|---|---|
| Create roles, create database, `provision_roles.sql` | yes | no | no |
| Convert an existing database (`convert_existing_database.sql`) | yes | no | no |
| `CREATE EXTENSION btree_gist` (trusted ext., PG 13+; created by the memberships migration, as the owner) | yes | yes | **no** |
| `migrate`, `apply_db_grants`, restore (`pg_restore`) | yes (avoid) | **yes** | no |
| `check_db_security`, `pg_dump` (backup) | yes | yes | **yes** |
| Run the web app, scheduled commands, `create_owner`, recovery CLI | no | no | **yes** |
| Test suite (creates/drops test DB) | no | yes | privilege tests open a second connection |

## Table classes
Every table must appear in `TABLE_POLICIES`. An unclassified table gets **no** runtime access and `coven.E001` fails the check (fail closed). `ALTER DEFAULT PRIVILEGES` is used only to REVOKE default privileges for the runtime role; no privilege is ever granted automatically. Grants are reapplied explicitly after each `migrate`.

| Class | Runtime privileges | Tables |
|---|---|---|
| MUTABLE | SELECT, INSERT, UPDATE, DELETE | accounts_loginevent, accounts_recoverycode, core_idsequence, catalogue_category, catalogue_acquisition, catalogue_labellayout, catalogue_importbatch, catalogue_importrow, django_session |
| NO_DELETE | SELECT, INSERT, UPDATE | circulation_loan (guard trigger), circulation_pendingforfeiture (guard trigger), memberships_subscriptionplan, memberships_membersubscription (only a one-time cancellation may update it; trigger-guarded), accounts_user, core_librarysetting, members_member, catalogue_bookedition, catalogue_bookcopy |
| APPEND_ONLY | SELECT, INSERT | circulation_loanrenewal, core_settingchangeevent, finance_financialtransaction, finance_paymententry, finance_receiptdocument, finance_tokenaccount, finance_tokencycle, finance_tokenledgerentry, audit_auditevent, catalogue_copyevent |
| READ_ONLY | SELECT | accounts_user_groups, accounts_user_user_permissions, django_migrations, django_content_type, auth_* |

Never granted: TRUNCATE, REFERENCES, TRIGGER, sequence UPDATE. Sequences: USAGE+SELECT (SELECT only on READ_ONLY tables, so `pg_dump` works). Phase 3 adds `finance_txn_correction_rules` (corrections rules, D-7), `finance_txn_payment_sum`, `finance_token_balance` and `memberships_subscription_guard`, all verified by `check_db_security`. Append-only triggers (`audit_event_append_only`, `catalogue_copyevent_append_only`, `members_member_no_immutable`) stay; the runtime role cannot alter, disable or drop them or replace their functions. Trigger functions pin `search_path = pg_catalog` (core.0002).

## Settings and credentials
- `DB_SECURITY_MODE=strict` (default). `single_role_dev` is development/test only; production refuses it.
- Runtime (`config.settings.local` / `production`, `.env`): `DB_USER=coven_app`, `DB_PASSWORD`. No owner credentials.
- Owner (`config.settings.migrate` / `test`): `DB_OWNER_USER`, `DB_OWNER_PASSWORD` from a separate git-ignored `.env.owner` (template: `.env.owner.example`). Production settings never read it.
- `wsgi`/`asgi` call `assert_runtime_security()` at start-up: refuse to serve if connected as the wrong role or privileges drifted.
- `manage.py migrate` under runtime settings is refused. Passwords never appear in logs, errors or command output.

## Provisioning (new machine)
Run as `postgres` (Mac: `psql -U postgres`; Windows: PowerShell with `psql.exe`):
```bash
psql -U postgres -d postgres -v ON_ERROR_STOP=1 -v database=coven_library -v owner_createdb=on \
  -f deploy/postgres/provision_roles.sql
```
Use `owner_createdb=off` on Windows. Set passwords with `\password coven_owner` and `\password coven_app`. The script is idempotent and drops nothing. Then:
```bash
python manage.py migrate --settings=config.settings.migrate      # as coven_owner; also applies grants
python manage.py check_db_security                               # as coven_app
python manage.py check_db_security --settings=config.settings.migrate   # as coven_owner
```
`python manage.py apply_db_grants --settings=config.settings.migrate [--dry-run]` reapplies grants alone.

## Converting an existing database
1. Confirm server and database name. Confirm no other sessions.
2. `pg_dump -Fc` backup; verify with `pg_restore --list` and a test restore into a disposable database.
3. Run `provision_roles.sql`, then `deploy/postgres/convert_existing_database.sql` as `postgres` (REASSIGN OWNED BY coven_app TO coven_owner, with assertions).
4. `migrate --settings=config.settings.migrate`, then both `check_db_security` runs.
Rollback: reverse REASSIGN (documented in the script); data is untouched.

## Backup and restore
- Backup: `pg_dump -Fc` works as `coven_app`.
- Restore order: provision roles → create empty database owned by `coven_owner` → `pg_restore --no-owner --role=coven_owner` as `coven_owner` → `apply_db_grants` → both checks. Never restore as runtime.
- Verified on a disposable database (with and without `--no-acl`): see `core/tests/test_db_lifecycle.py`.
- Not yet verified: clean Windows 11 machine (Phase 7b/7c).

## Recovery
**Lost `coven_owner` password** (superuser still available): as `postgres`, run interactive `\password coven_owner` (never a literal password in SQL), update `.env.owner`, rerun both checks.

**Exceptional: no superuser password.** Stop the PostgreSQL service. Temporarily set the `127.0.0.1/32` line in `pg_hba.conf` (and `::1/128`) to `trust` for `postgres` only, reload, connect locally, set a new `postgres` password, **restore `scram-sha-256`**, reload, verify with `SHOW hba_file` and a failed passwordless login. Record the event. This is a last resort; the trust window gives any local user superuser access.

**Lost `coven_app` password:** owner or `postgres` resets it; update `.env`.

## Verification status
Verified on macOS, PostgreSQL 16.15: full suite as `coven_owner`, privilege tests as `coven_app`, owner rollback/forward migrations, backup/restore into a disposable database. **Not verified:** Windows 11, PowerShell provisioning, Task Scheduler with runtime credentials, clean-machine recovery. Do not treat recovery as verified until Phase 7b.

After any restore, run `apply_db_grants` and both `check_db_security` runs; restored ACLs are not trusted.

## Residual risks
- A `postgres` superuser, or an OS administrator who can read `.env.owner` or the data directory, can bypass everything.
- `.env.owner` on a development Mac is a plaintext file (0600, git-ignored). Windows: keep owner password out of the machine except during upgrades.
- Trigger-based append-only rules bind the owner role too, but the owner can drop them; owner use is limited to migrations.
- Windows 11 runtime is untested until Phase 7c.

## Phase 3 remediation migrations: verification notes
Read-only inspection of `finance.0005`, `memberships.0005`, `core.0003`, `catalogue.0003` and `finance.0006` (migration SQL via `sqlmigrate`, PostgreSQL catalog queries, and a rolled-back behavioural probe on a disposable database):

- **Types and limits.** All eight paise columns are `bigint` (`finance_financialtransaction.amount_paise`, `finance_paymententry.amount_paise`, `finance_tokencycle.amount_snapshot_paise`, `finance_tokenledgerentry.delta_paise`, `memberships_subscriptionplan.price_paise`, `memberships_membersubscription.price_paise_snapshot`, `core_librarysetting.token_amount_paise`, `catalogue_acquisition.unit_cost_paise`). Each has a CHECK with the single maximum 4,999,900 paise (Rs 49,999.00, D-16; the token ledger delta is bounded on both sides, the unit cost allows NULL). The checks were first created at 9,999,999,999 (`*.0005`) and replaced by the D-16 limit in `finance.0007`, `memberships.0006`, `core.0004` and `catalogue.0004` (each drops and re-adds the same-named constraints; the columns stay `bigint`). Applying them fails if any stored amount exceeds Rs 49,999.00; reversing them restores the earlier, wider limit and is always safe. No other money column exists.
- **Nothing else changed.** Every named constraint, index, foreign key and unique constraint declared by the models still exists on the migrated database; owners, privileges, classifications and the trigger list are unchanged apart from the new `finance_txn_correction_rules`. `ALTER COLUMN ... TYPE bigint` rewrites a table without firing row triggers, so the append-only and guard triggers are not involved.
- **Order.** `finance.0005 -> finance.0006`; `memberships.0005` depends on `finance.0005`; `core.0003` follows `core.0002`; `catalogue.0003` follows `catalogue.0002`.
- **Reversal.** Each of the four widening migrations is reversible (drop the checks, then `ALTER COLUMN ... TYPE integer`). **Reversal fails with "integer out of range" as soon as any stored value exceeds 2,147,483,647 paise**, and is only safe while all amounts fit in 32 bits (as on the development database today). `finance.0006` reverses by dropping its trigger and function.
- **`finance_txn_correction_rules`.** BEFORE INSERT on `finance_financialtransaction` (updates are already refused by the append-only trigger), owned by `coven_owner`, `search_path=pg_catalog`, not SECURITY DEFINER, default PUBLIC EXECUTE (harmless: calling a trigger function directly is refused). It refuses a reversal of a subscription payment, a paid refund or a reversal, and a money-out adjustment that references a subscription payment, for every role including the owner. `coven_app` cannot disable, drop, replace or alter it, cannot change `session_replication_role`, and has only SELECT and INSERT on the six append-only finance tables. Limits: the owner role can still disable triggers or TRUNCATE (accepted L6 decision), and the database cannot see the owner's "not a fee refund" confirmation (service rule; stored in the audit event).

## Phase 4 additions
Tables: `circulation_loan` (NO_DELETE, trigger `circulation_loan_guard`: issue facts and closed loans frozen, due date only forward while active, forfeiture link set once, never deleted), `circulation_loanrenewal` (APPEND_ONLY), `circulation_pendingforfeiture` (NO_DELETE, trigger `circulation_pending_forfeiture_guard`: pending -> confirmed or declined once), `core_settingchangeevent` (APPEND_ONLY). `catalogue_copyevent` gained a nullable `loan_id` (still APPEND_ONLY). `finance_financialtransaction.recorded_by` is optional and `recorded_by_system` was added (CHECKs `txn_exactly_one_attribution`, `txn_system_actor_only_for_automatic_forfeiture`; trigger `finance_ledger_attribution` ties the system attribution to the automatic forfeiture in both directions). Migrations: `circulation.0001-0004`, `catalogue.0005`, `core.0005-0006`, `finance.0008-0009`, all run as `coven_owner`; the runtime role `coven_app` runs the daily job and every workflow with only SELECT/INSERT/UPDATE as classified (verified by a subprocess test with the owner credentials removed).



## Outbox table `core_domainevent` (D-23)
Class APPEND_ONLY (registered in `core/dbsecurity.py` `TABLE_POLICIES` and `REQUIRED_TRIGGERS`). Owner: `coven_owner`. `coven_app`: INSERT and SELECT only (no UPDATE, DELETE, TRUNCATE, no `FOR UPDATE` / `FOR KEY SHARE`). Trigger `core_domainevent_append_only` (BEFORE UPDATE OR DELETE) calls `core_reject_change()`, whose `search_path` is pinned to `pg_catalog`; it also binds the owner role. Migrations `core.0007` (table, unique `event_id` and `dedupe_key`, non-empty CHECKs, indexes) and `core.0008` (trigger) run as `coven_owner`. Verified by raw-SQL and runtime-role tests, and by `check_db_security` under both roles (0 errors).
