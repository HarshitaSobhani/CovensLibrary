# Deployment — Dell, Windows 11 (database part; not yet tested on Windows)

Status: database role setup only. Waitress, Task Scheduler, backup and restore steps arrive in Phase 7. Windows 11 runtime testing is pending (Phase 7c).

## PostgreSQL
- Same major version as development. Listen on `127.0.0.1` only. `pg_hba.conf`: `scram-sha-256` for local connections; no `trust`.
- Provision roles (PowerShell, as `postgres`), without CREATEDB for the owner:
```powershell
& "C:\Program Files\PostgreSQL\16\bin\psql.exe" -U postgres -d postgres -v ON_ERROR_STOP=1 `
  -v database=coven_library -v owner_createdb=off -f deploy\postgres\provision_roles.sql
```
Set passwords interactively with `\password coven_owner` and `\password coven_app`.

## Credentials
- Runtime environment (Task Scheduler / service account): `DJANGO_SETTINGS_MODULE=config.settings.production`, `DB_USER=coven_app`, `DB_PASSWORD`, `DJANGO_SECRET_KEY`, `DB_NAME`. **No owner credentials.**
- Owner password (decision D-5): a temporary `.env.owner` is allowed during upgrades only.
  - Create it only when an upgrade is required.
  - Restrict its ACL to the Windows account running the upgrade (`icacls`).
  - Never place owner credentials in the Task Scheduler runtime environment.
  - Delete it after successful migration and verification.
  - Never include it in logs or backups.
- CREATEDB (decision D-6): never granted on the Dell (`owner_createdb=off`); never granted to `coven_app` anywhere.
- Never set `DB_SECURITY_MODE=single_role_dev`; production refuses it.

## Upgrade procedure
```powershell
python manage.py migrate --settings=config.settings.migrate
python manage.py check_db_security --settings=config.settings.migrate
python manage.py check_db_security
```
The first two run as `coven_owner`; the last as `coven_app`. Back up first.

## Restore
Provision roles, create the database owned by `coven_owner`, restore as `coven_owner`, run `apply_db_grants --settings=config.settings.migrate`, then both checks. Details: `docs/DATABASE_SECURITY.md`.
