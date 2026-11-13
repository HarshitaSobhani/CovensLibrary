# Deployment — Dell, Windows 11

Status: written in Phase 7c from the development machine (macOS/Linux). **Nothing in this file has been run on the Dell yet.** Every Windows-specific step (PowerShell scripts, Task Scheduler, Credential Manager, `pg_dump`/`pg_restore` on PATH, the browser check) is UNVERIFIED until `deploy\windows\verify_runtime.ps1` and the clean-machine rehearsal (`docs/OPERATIONS.md`) have been completed and recorded. The application listens on `127.0.0.1` only (decision B37); it is never exposed to the network.

## 1. Prerequisites (install once)
- Windows 11 (record the edition and build in the rehearsal record).
- PostgreSQL 16 (same major version as development), listening on `127.0.0.1` only; `pg_hba.conf` with `scram-sha-256` for local connections, no `trust`. Add PostgreSQL's `bin` folder to PATH, or set `PG_DUMP_PATH` to the full path of `pg_dump.exe` (`pg_restore.exe` must sit next to it).
- Python 3.12, then in the application folder: `python -m venv .venv`, `.venv\Scripts\python -m pip install -r requirements\base.txt`.
- A Windows account that is the owner's everyday account. The backup key lives in that account's Credential Manager, so the scheduled tasks run as that account while it is signed in (section 6).

## 2. Database roles and the empty database
Provision roles and the database (PowerShell, as `postgres`), without CREATEDB for the owner:
```powershell
& "C:\Program Files\PostgreSQL\16\bin\psql.exe" -U postgres -d postgres -v ON_ERROR_STOP=1 `
  -v database=coven_library -v owner_createdb=off -f deploy\postgres\provision_roles.sql
```
Set the two passwords interactively (`\password coven_owner`, `\password coven_app`). Details: `docs/DATABASE_SECURITY.md`.

## 3. Runtime environment (the Windows account that runs the application)
Set as user environment variables for that account only (never in a file in the repository): `DJANGO_SETTINGS_MODULE=config.settings.production`, `DJANGO_SECRET_KEY`, `DB_NAME=coven_library`, `DB_USER=coven_app`, `DB_PASSWORD`, `DJANGO_ALLOWED_HOSTS=127.0.0.1,localhost`, the SMTP variables (`EMAIL_BACKEND`, `EMAIL_HOST`, `EMAIL_PORT`, `EMAIL_USE_TLS`, `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD`, `DEFAULT_FROM_EMAIL`), `BACKUP_DESTINATION=drive`, and `PG_DUMP_PATH` if needed. **No owner credentials.** Cookie `Secure` flags stay off while the application is served over plain `http://127.0.0.1` (the Django `check --deploy` warnings about HTTPS are therefore expected); verify the session cookie behaviour in the Dell's browser.
- Owner password (decision D-5): a temporary `.env.owner` is allowed during upgrades only. Create it only when an upgrade is required, restrict its ACL to the Windows account running the upgrade (`icacls`), never place owner credentials in the runtime environment, delete it after migration and verification, never include it in logs or backups.
- CREATEDB (D-6) is never granted on the Dell; never granted to `coven_app` anywhere. Never set `DB_SECURITY_MODE=single_role_dev`; production refuses it.

## 4. First installation — in this order
1. **Back up first** if the database already holds data (`docs/OPERATIONS.md`).
2. Create `.env.owner` (owner role, temporary) and apply migrations as the OWNER:
   `python manage.py migrate --settings=config.settings.migrate`
   *Partial-migration caveat:* `migrate` applies pending migrations of every app in dependency order, each in its own transaction. If one migration refuses (for example `communications.0003`, below), the migrations that ran before it stay applied; only the refusing migration (and the ones after it) did not run. A refusal is therefore not proof that nothing changed: read the output.
3. **Populated-database guard (Phase 5).** Migration `communications.0003` permanently DROPS the address column of the notification attempt log and cannot restore it (reversing it fails when attempt rows exist). It therefore REFUSES to run when notification or attempt rows exist, unless `COVEN_ALLOW_NOTIFICATION_MIGRATION=1` is set for that run. Set the override only with a documented plan and a verified backup. A brand-new database needs no override.
4. **Notification preflight (before enabling notification processing).** `python manage.py preflight_notifications` (read-only; exit code 1 and a list if any anonymised member's notification records still hold a subject, body or address). If it reports anything, run `python manage.py preflight_notifications --apply` (redacts with the same logic as anonymisation, re-checks, fails if anything remains) and confirm the final line says `Verified: nothing remains`. Do not assume the anonymisation hook repaired historic records.
5. Verify under BOTH database roles: `python manage.py check_db_security --settings=config.settings.migrate` (owner) and `python manage.py check_db_security` (runtime); both must report 0 errors. Only then enable notification processing (the scheduled tasks, section 6).
6. Delete `.env.owner`.
7. `python manage.py collectstatic --noinput` (WhiteNoise serves the collected files), then `python manage.py create_owner --username <name> --email <address>`.
8. Backup key: `python manage.py setup_backup_key` — writes the working key into the Credential Manager and prints the RECOVERY KEY once. Write it on paper and keep it away from this computer and from the backups; if the recovery key and this computer are both lost, the backups cannot be opened.
9. Google Drive (needs an OAuth client you create in Google Cloud): `python manage.py connect_drive --client-id ... --client-secret ...` (browser consent on this computer; the secret goes to the Credential Manager). The app creates its own folder "Coven's Library Backups" on the first backup (`drive.file` scope). Then `python manage.py run_backup` and confirm "stored at drive and verified".
10. Register the tasks (section 6) and run `deploy\windows\verify_runtime.ps1`; keep its output.

## 5. Running the application
`deploy\windows\start_app.ps1` runs `python manage.py serve_local`: waitress on `127.0.0.1:8000` (the command refuses any other address), after a launch catch-up of the daily jobs (overdue forfeitures, notifications, a missed backup). Open `http://127.0.0.1:8000/` in the browser.

## 6. Scheduling (Task Scheduler)
`deploy\windows\register_tasks.ps1` (run as the owner's Windows account, not elevated) registers:
- **Coven's Library - application:** starts the application at logon.
- **Coven's Library - jobs:** runs `run_daily_jobs` at logon and every 5 minutes. The command is idempotent: it posts due day-8 forfeitures, runs the notification cycle (09:00 reminders, SMTP retries at 5 minutes / 30 minutes / 2 hours), and makes the daily backup when due at the configured time (default 19:45, editable in Settings) or as a catch-up after the computer was off or asleep. "Start when available" makes Windows run a missed run when the computer is back.
The tasks run "only when the user is logged on" because the backup key is protected by that Windows account; if the account is signed out at 19:45 the backup happens at the next sign-in. `register_tasks.ps1 -Remove` deletes both tasks.

## 7. Upgrade procedure
```powershell
# back up first (python manage.py run_backup), then, with a temporary .env.owner:
python manage.py migrate --settings=config.settings.migrate
python manage.py check_db_security --settings=config.settings.migrate
python manage.py check_db_security
python manage.py preflight_notifications
python manage.py collectstatic --noinput
```
The first two run as `coven_owner`; the third as `coven_app`. Delete `.env.owner` afterwards. Restart the application task.

## 8. Restore and replacement-computer recovery
Full runbook and the rehearsal checklist: `docs/OPERATIONS.md`. In short: provision PostgreSQL and the roles on the new computer, create the empty database owned by `coven_owner`, install the application, run `python manage.py restore_backup <file> --settings=config.settings.migrate` (it asks for the recovery key and the typed confirmations), then `check_db_security` under both roles. **Recovery is not claimed verified until a restore on a clean Windows 11 machine has been performed and recorded.**

## 9. Verification checklist for the Dell (not yet done)
`deploy\windows\verify_runtime.ps1` checks: Windows 11 build, Python 3.12, `pg_dump`/`pg_restore`, a complete runtime environment with no owner credentials, PostgreSQL bound to loopback, `check_db_security` and `check --deploy`, the notification preflight, the backup key in the Credential Manager, the registered tasks, the application answering on 127.0.0.1 and on no other address, and the security headers. Then open the application in the Dell's browser and confirm: login, theme switch, a receipt PDF, a report download, no browser console errors.
