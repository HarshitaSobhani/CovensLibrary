# Deployment — Dell, Windows 11

Status: written in Phase 7c and extended in the security and one-click-launcher pass, all from the development machine (macOS/Linux). **Nothing in this file has been run on the Dell yet, and the PowerShell scripts have not even been executed or syntax-checked by a PowerShell parser.** Every Windows-specific step (PowerShell scripts, Task Scheduler, Credential Manager, `pg_dump`/`pg_restore` on PATH, the browser check) is UNVERIFIED until `deploy\windows\verify_runtime.ps1` and the clean-machine rehearsal (`docs/OPERATIONS.md`) have been completed and recorded. The application listens on `127.0.0.1` only (decision B37); it is never exposed to the network.

## 1. Prerequisites (install once)
- Windows 11 (record the edition and build in the rehearsal record).
- PostgreSQL 16 (same major version as development), listening on `127.0.0.1` only; `pg_hba.conf` with `scram-sha-256` for local connections, no `trust`. Add PostgreSQL's `bin` folder to PATH, or set `PG_DUMP_PATH` to the full path of `pg_dump.exe` (`pg_restore.exe` must sit next to it).
- Python 3.12. The packages are installed by `Setup-CovensLibrary.ps1` from `requirements\production.lock` (every package pinned with SHA-256 hashes, `--require-hashes`); do not install from `base.txt` on the Dell.
- **BitLocker is MANDATORY before the Dell holds production data (decision S-05).** Turn on BitLocker (Windows 11 Pro) or device encryption (Home), wait until encryption has finished, and store its **recovery key OFF the computer** (printed and kept safe, or in your Microsoft account) — never on this computer, never inside a Coven's Library backup, never in the Credential Manager. An **administrator** verifies it with `Verify-BitLocker-Admin.ps1` (step 1 of section 10); the owner's setup (step 2) refuses to continue without that verification and without the owner's own YES. No script enables BitLocker, changes protectors or shows/logs a recovery key. See THREAT_MODEL scenario 6.
- A Windows account that is the owner's everyday account. The backup key lives in that account's Credential Manager, so the scheduled tasks run as that account while it is signed in (section 6).

## 2. Database roles and the empty database
Provision roles and the database (PowerShell, as `postgres`), without CREATEDB for the owner:
```powershell
& "C:\Program Files\PostgreSQL\16\bin\psql.exe" -U postgres -d postgres -v ON_ERROR_STOP=1 `
  -v database=coven_library -v owner_createdb=off -f deploy\postgres\provision_roles.sql
```
Set the two passwords interactively (`\password coven_owner`, `\password coven_app`). Details: `docs/DATABASE_SECURITY.md`.

## 3. Runtime configuration and secrets (the owner's Windows account)
No secret is kept in an environment variable or a file any more.
- **Secrets** (`DJANGO_SECRET_KEY`, the password of the database role `coven_app`, optionally the e-mail password) are saved **once** in the Windows Credential Manager by `deploy\windows\store_secrets.py` (called by the setup script; hidden prompts; the Django key is generated and never shown). **Decision S-02: the Credential Manager is the ONLY source in production.** The production settings refuse to load unless the launcher declared `COVEN_SECRET_STORE=credential-manager`; a plaintext environment variable with the name of a secret is **refused** (not used as a fallback); a store that cannot be read stops the application (**fail closed**, with a distinct message from "not saved"); an existing secret is never replaced unless you pass `--replace NAME`; the launcher never generates or substitutes a value.
- **Non-secret settings** live in `%LOCALAPPDATA%\CovensLibrary\config\app.env` (database name and role, host/port, allowed hosts, `PG_DUMP_PATH`, backup destination, SMTP host/user, cookie flags). The folder is private to the owner's account (`icacls`, inheritance removed). The launcher accepts only a fixed list of keys and **refuses a file that contains a password or secret**; it forces `DJANGO_SETTINGS_MODULE=config.settings.production`, `DJANGO_DEBUG=false` and the log and run folders.
- **Old method is REFUSED:** if a user environment variable named `DJANGO_SECRET_KEY`, `DB_PASSWORD` or `EMAIL_HOST_PASSWORD` exists, start-up stops (it is never used). Remove it once the secrets are saved: `[Environment]::SetEnvironmentVariable("DB_PASSWORD", $null, "User")` and likewise for the others. Development machines (Mac) are unaffected: they use `.env` with the development settings.
- The Credential Manager protects against reading the disk offline and against casual exposure; it does **not** protect against software running as the same signed-in user. **Never let an administrator reset this Windows password** (the saved secrets and the backup key become unreadable); change it yourself while signed in.
- **Owner-role commands without a file:** `deploy\windows\Run-AsOwner.ps1 <command>` (for example `migrate`, `check_db_security`, `restore_backup <file>`) asks for the owner password hidden, keeps it in that process's memory only and clears it afterwards. The production packages do not include `python-dotenv`, so on the Dell a `.env.owner` file is **not read** anyway; use this script. Runtime-role commands: `deploy\windows\Invoke-Manage.ps1 <command>` (same configuration and secrets as the launcher).
- Owner password (decision D-5, laptop/development): a temporary `.env.owner` is allowed during upgrades only. Create it only when an upgrade is required, restrict its ACL to the Windows account running the upgrade (`icacls`), never place owner credentials in the runtime environment, delete it after migration and verification, never include it in logs or backups. The launcher refuses to start if owner credentials are present in the environment.
- CREATEDB (D-6) is never granted on the Dell; never granted to `coven_app` anywhere. Never set `DB_SECURITY_MODE=single_role_dev`; production refuses it, and production also refuses to load with `DJANGO_DEBUG=true`.

## 4. First installation — in this order
1. **Back up first** if the database already holds data (`docs/OPERATIONS.md`).
2. Apply migrations as the OWNER: `deploy\windows\Run-AsOwner.ps1 migrate` (prompts for the owner password; on a development machine: a temporary `.env.owner` and `python manage.py migrate --settings=config.settings.migrate`):
   *Partial-migration caveat:* `migrate` applies pending migrations of every app in dependency order, each in its own transaction. If one migration refuses (for example `communications.0003`, below), the migrations that ran before it stay applied; only the refusing migration (and the ones after it) did not run. A refusal is therefore not proof that nothing changed: read the output.
3. **Populated-database guard (Phase 5).** Migration `communications.0003` permanently DROPS the address column of the notification attempt log and cannot restore it (reversing it fails when attempt rows exist). It therefore REFUSES to run when notification or attempt rows exist, unless `COVEN_ALLOW_NOTIFICATION_MIGRATION=1` is set for that run. Set the override only with a documented plan and a verified backup. A brand-new database needs no override.
4. **Notification preflight (before enabling notification processing).** `python manage.py preflight_notifications` (read-only; exit code 1 and a list if any anonymised member's notification records still hold a subject, body or address). If it reports anything, run `python manage.py preflight_notifications --apply` (redacts with the same logic as anonymisation, re-checks, fails if anything remains) and confirm the final line says `Verified: nothing remains`. Do not assume the anonymisation hook repaired historic records.
5. Verify under BOTH database roles: `python manage.py check_db_security --settings=config.settings.migrate` (owner) and `python manage.py check_db_security` (runtime); both must report 0 errors. Only then enable notification processing (the scheduled tasks, section 6).
6. Delete `.env.owner`.
7. `python manage.py collectstatic --noinput` (WhiteNoise serves the collected files), then `python manage.py create_owner --username <name> --email <address>`.
8. Backup key: `python manage.py setup_backup_key` — writes the working key into the Credential Manager and prints the RECOVERY KEY once. Write it on paper and keep it away from this computer and from the backups; if the recovery key and this computer are both lost, the backups cannot be opened.
9. Google Drive (needs an OAuth client you create in Google Cloud): `python manage.py connect_drive` (it asks for the client ID and the client secret — the secret is hidden; do **not** put it on the command line). Browser consent opens on this computer; the secret goes to the Credential Manager. The app creates its own folder "Coven's Library Backups" on the first backup (`drive.file` scope). Then `python manage.py run_backup` and confirm "stored at drive and verified".
10. Register the jobs task (section 6), then start the application from the desktop shortcut and run `deploy\windows\verify_runtime.ps1`; keep its output.

**BitLocker comes first:** do not store production data on a computer until an administrator has run `Verify-BitLocker-Admin.ps1` successfully (section 10, step 1); the owner's Setup checks that record and stops otherwise.

**Shortcut path:** steps 1–9 above are the *deliberate* part (roles, migrations, key, Drive). The daily tooling — virtual environment, pinned packages, folders, secrets, display files, desktop shortcuts — is created by `deploy\windows\Setup-CovensLibrary.ps1` (section 10); run it between step 1 and step 2 on a new computer, and again whenever the code is updated (it keeps existing settings and secrets).

## 5. Running the application
The owner double-clicks the **Coven's Library** desktop shortcut (section 10). Behind it, `Start-CovensLibrary.ps1` runs the same `python manage.py serve_local --host 127.0.0.1 --port 8000` as before: waitress on `127.0.0.1` only, after a launch catch-up of the daily jobs (overdue forfeitures, notifications, a missed backup). There is no second server start-up path: `start_app.ps1` now simply calls the launcher silently. The development `runserver` is never used on the Dell.

## 6. Scheduling (Task Scheduler)
`deploy\windows\register_tasks.ps1` (run as the owner's Windows account, not elevated) registers:
- **Coven's Library - jobs** (always): runs `run_daily_jobs` at logon and every 5 minutes. The command is idempotent: it posts due day-8 forfeitures, runs the notification cycle (09:00 reminders, SMTP retries at 5 minutes / 30 minutes / 2 hours), and makes the daily backup when due at the configured time (default 19:45, editable in Settings) or as a catch-up after the computer was off or asleep. "Start when available" makes Windows run a missed run when the computer is back. **It does not need the web server to be running**, so backups and reminders continue even if nobody opened the application that day.
- **Coven's Library - application** (optional, `-AutoStartApplication`): starts the application silently (no window, no browser) when the owner signs in. See section 13 for the recommendation (off by default).
The tasks run "only when the user is logged on" because the backup key and secrets are protected by that Windows account; if the account is signed out at 19:45 the backup happens at the next sign-in. `register_tasks.ps1 -Remove` deletes the tasks.

## 7. Upgrade procedure
See section 12 (safe update) — it includes the lock file, the migration guard and the checks.

## 8. Restore and replacement-computer recovery
Full runbook and the rehearsal checklist: `docs/OPERATIONS.md`. In short: provision PostgreSQL and the roles on the new computer, create the empty database owned by `coven_owner`, install the application, run `python manage.py restore_backup <file> --settings=config.settings.migrate` (it asks for the recovery key and the typed confirmations), then `check_db_security` under both roles. **Recovery is not claimed verified until a restore on a clean Windows 11 machine has been performed and recorded.**

## 9. Verification checklist for the Dell (not yet done)
`deploy\windows\verify_runtime.ps1` checks: Windows 11 build, Python 3.12, `pg_dump`/`pg_restore`, a complete runtime environment with no owner credentials, PostgreSQL bound to loopback, `check_db_security` and `check --deploy`, the notification preflight, the backup key in the Credential Manager, the registered tasks, the application answering on 127.0.0.1 and on no other address, and the security headers. Then open the application in the Dell's browser and confirm: login, theme switch, a receipt PDF, a report download, no browser console errors.

## 10. One-click launcher: the two-account installation, and what the owner does
**Why two steps.** Reading BitLocker needs administrator rights; the application's settings, secrets and shortcuts must belong to the OWNER's account. If one script did both, "Run as administrator" could run it as a different account and save the owner's secrets in that account's Credential Manager (and its `%LOCALAPPDATA%`, desktop and logs). So the administrator's step is tiny, standalone and writes only one machine-wide record; the owner's setup is never elevated and refuses to run elevated. Nothing silently switches accounts, nothing is stored in the administrator's profile and no secret ever goes into an environment variable.

**Step 1 — the administrator (any administrator account), once, in an elevated PowerShell:**
```powershell
powershell -ExecutionPolicy Bypass -File deploy\windows\Verify-BitLocker-Admin.ps1
```
It reads the BitLocker status of the system drive and confirms: protection ON, volume **fully** encrypted, and at least one **recovery protector** exists. It writes `%ProgramData%\CovensLibrary\bitlocker-attestation.json` (computer, drive, status, protector count, who and when — no key; readable by everyone, writable only by administrators) and prints what the owner must do next. It never enables BitLocker, never changes protectors or recovery settings, never displays, exports or logs a recovery key, and does not load any application file (`common.ps1`), so it creates nothing in the administrator's profile. If BitLocker is unavailable, off or still encrypting it writes no record, says in plain words what the owner must do (turn on Device encryption or BitLocker, wait, keep the key off the computer) and exits non-zero. Optional, also administrator-only: `Grant-DatabaseServiceControl-Admin.ps1 -OwnerAccount "COMPUTER\owner"` (names the owner explicitly; types the name again to confirm) lets the owner start and stop only the PostgreSQL service.

**Step 2 — the owner, signed in to HER OWN Windows account, in a NORMAL PowerShell (never "Run as administrator"):**
```powershell
powershell -ExecutionPolicy Bypass -File deploy\windows\Test-Syntax.ps1
powershell -ExecutionPolicy Bypass -File deploy\windows\Setup-CovensLibrary.ps1
```
The exact sequence it follows, each step stopping on failure with nothing half-created:
1. **Which account?** Refuses if elevated. Shows the account, the profile and the folders, states that the secrets go to *this* account's Credential Manager, and requires the owner to **type the account name**; a mismatch stops it before anything is created. It also checks `%LOCALAPPDATA%` is inside the account's profile.
2. Windows 11 build.
3. **BitLocker:** reads the administrator's record (recent, for this computer and drive, fully encrypted, protection on, recovery protector present). Missing or stale → stops and says which administrator step to run. Then the owner types **YES** that the recovery key is stored **off** the computer; this is recorded in the owner's own `config\bitlocker.attestation`.
4. Python 3.12 and PostgreSQL 16 (service and tools).
5. Writes the non-secret `app.env` in the owner's profile.
6. Creates `.venv` and installs the **hash-locked** packages (`--require-hashes`).
7. **Proves the owner's Credential Manager works** (`store_secrets.py --check`: a throw-away item is written, read back and deleted). If not, it stops: there is no fallback.
8. Saves the secrets in that Credential Manager (hidden prompts; an existing secret is never replaced).
9. Collects the display files; creates the **two shortcuts on the owner's desktop** (it refuses to run elevated).
10. **Starts the application under the owner's account** (read-only checks, then a trial start with no browser, a health check, and a stop). Until the database roles, migrations and the backup key exist (sections 2, 4, 5) the checks fail with a plain message and setup exits with code 2: expected; run it again afterwards.

It does **not** create database roles, run migrations, create or replace the backup key, restore anything, enable BitLocker, or touch environment variables.

**Every morning (owner):** double-click **Coven's Library**. A small window says *Starting Coven's Library… Checking the database… Starting the application… Coven's Library is ready.* and the browser opens. At the end of the day, double-click **Stop Library** (or just sign out / shut down; nothing is lost).

**What the launcher does on every start, in order** (all read-only until the server starts): already running → just open it; another program on the port → stop with a message; find the virtual environment and `app.env`; check the Python packages import; start the PostgreSQL service if it is stopped (and the account may); wait for the database port; `python -m core.preflight` (configuration, no owner credentials, no debug, secrets reachable, database reachable); `manage.py launcher_preflight` (database login, **all migrations applied**, database security, **backup key present** — never created —, display files); start `serve_local` hidden with output in protected logs; poll `/healthz/`; open the browser only when it answers healthy. It **never** runs migrations, creates or replaces keys, resets credentials, changes permissions, restores, deletes data or installs packages.

**Messages the owner may see** (details are in `%LOCALAPPDATA%\CovensLibrary\logs\launcher.log`, never on screen): "The database is not running" · "The application needs an update before it can start" (migrations pending) · "The backup protection key is missing on this computer" (the key is not in the Credential Manager: **do not create a new one**; see OPERATIONS section 5) · "The saved database or security settings are missing" · "Another program is using the place Coven's Library needs" · "Coven's Library is running but is not working correctly" · always followed by "Could not start the application. Contact support."

**A missing backup key blocks start-up** (decision S-01): running without backup protection silently accumulates unprotected data.

**Process control:** the server writes `server.json` (its pid, port, start time) in the run folder and removes it when it exits; a second launch refuses to start a second copy; a stale file from a crash is ignored and replaced. **Stop Library** writes `stop.request`; the server finishes running requests, closes and exits (tested on this development machine with real server processes). Only if the polite request fails after 25 seconds does the script end the one recorded process, and only after checking its pid, start time and command line; it never kills by name and never touches another Python.

## 11. Windows verification still required (nothing below has been run on a Windows machine)
**Exact validation sequence (all of it still requires the Dell, Windows 11; none of it has been done):**

**A. Administrator account**
1. `deploy\windows\Test-Syntax.ps1` — parses every PowerShell script with the real parser (the development machine could not run PowerShell). Keep its output; fix any parse error before anything else.
2. `deploy\windows\Verify-BitLocker-Admin.ps1` in an elevated PowerShell. Also run it where BitLocker is off or still encrypting: no record may be written.
3. Confirm `%ProgramData%\CovensLibrary\bitlocker-attestation.json` exists and `icacls` shows SYSTEM and Administrators full control, Users read and execute only.
4. Confirm no recovery key appears on screen, in the record or in any log, and that nothing was created in the administrator's profile.

**B. Owner account (own sign-in, normal PowerShell)**
5. `Setup-CovensLibrary.ps1` run elevated must be REFUSED; a wrong typed account name must stop it; a missing or stale attestation must stop it.
6. `Setup-CovensLibrary.ps1` unelevated completes: secrets are in THIS account's Credential Manager (`store_secrets.py --check` passes); both shortcuts are on THIS account's desktop; nothing is in the administrator's profile.
7. Start with the shortcut, check `/healthz/`, then **Stop Library** (graceful stop, `server.json` gone).

**C. Recovery**
8. Rotation with disposable keys and a test Drive folder: rotate, abort (before and after the verification backup), resume after an interruption, `import_backup_key`, `--set-current` rollback, prune (including a refusal caused by an extra old-key file).
9. Clean-machine restore (OPERATIONS section 6). **Until a record for it exists, recovery is NOT verified.**

| Item | How to verify | Expected |
|---|---|---|
| PowerShell scripts parse and run | run `Setup-CovensLibrary.ps1`, then each launcher path | no syntax or cmdlet errors |
| First start / second start | double-click twice | first starts; second says already running and opens the browser |
| PostgreSQL stopped | stop the service, double-click | service starts (if granted) or "The database is not running" |
| PostgreSQL unavailable (wrong port) | edit `DB_PORT` in `app.env` | "The database is not running"; log has the code |
| Missing configuration | rename `app.env` | "not set up correctly" |
| Missing secret | remove `runtime-DB_PASSWORD` from the Credential Manager | "saved … settings are missing" |
| Missing encryption key | remove `backup-working-key` (on a test machine only) | "backup protection key is missing"; **no key created** |
| Pending migration | test machine with an unapplied migration | "needs an update"; nothing applied |
| Port taken by another program | `python -m http.server 8000` first | "Another program is using the place…"; the other program untouched |
| Server start-up failure | break a setting | plain message; details only in the log |
| Graceful stop | **Stop Library** | stopped within seconds; `server.json` gone |
| Unexpected termination | end the python process in Task Manager, double-click | log notes the earlier end; new server starts |
| Browser opening | check timing | only after "ready" |
| Logs and permissions | `icacls %LOCALAPPDATA%\CovensLibrary`; read the logs | owner account only; no secrets |
| Scheduled backup compatibility | register the jobs task, sign out/in around 19:45 | backup runs or catches up; server state irrelevant |
| Credential Manager behaviour | entries visible under "Windows Credentials"; survive reboot | present |
| BitLocker (mandatory) | `Verify-BitLocker-Admin.ps1` as administrator; Setup (as the owner) stops without the record | record written to `%ProgramData%\CovensLibrary`; nothing written in the administrator's profile; owner typed YES in her own account; no key ever shown or logged |
| Two-account separation | after step 1 look in the administrator's profile; after step 2 look in the owner's | the administrator's `%LOCALAPPDATA%\CovensLibrary` does not exist; the owner's holds config, logs, shortcuts and Credential Manager entries |
| Setup refuses elevation | run Setup in an administrator PowerShell | it stops with the explanation; nothing is created |
| Account confirmation | type a different account name | it stops; nothing is created |
| Credential Manager fail-closed | make the entries unreadable (test machine) and start | "The Windows credential store could not be read"; nothing generated, nothing falls back |
| Plaintext secret refused | set a user variable `DB_PASSWORD` on a test machine | start-up stops naming the variable |
| Key rotation rehearsal | `docs/KEY_ROTATION_AND_RECOVERY.md` section 7 | all steps, incl. interruption and resume |
| Browser cookies on `http://127.0.0.1` | login, theme switch, a receipt PDF | works |
| Clean-machine restore | OPERATIONS section 6 | recorded rehearsal |

## 12. Updating the application safely
1. Run **Stop Library**. Make sure a recent backup succeeded (`backup_status`) — `run_backup` if in doubt.
2. Get the new code (on the Dell: `git pull origin main` by the maintainer).
3. `deploy\windows\Setup-CovensLibrary.ps1` again (owner account, not elevated): it reinstalls from `requirements\production.lock` **only** (it keeps `app.env` and secrets) and recollects the display files.
4. If the release contains migrations: `deploy\windows\Run-AsOwner.ps1 migrate`, then `Run-AsOwner.ps1 check_db_security` and `Invoke-Manage.ps1 check_db_security` (both must report 0 errors), `Invoke-Manage.ps1 preflight_notifications` (see section 4 for the Phase 5 guard). No password file is created. The launcher will refuse to start with "needs an update" until this is done — that is the safety net, not an error.
5. `deploy\windows\verify_runtime.ps1`, then start with the shortcut and look at the dashboard.
6. **Changing dependencies:** edit `requirements\base.txt` on the development machine, regenerate the lock (`uv pip compile requirements/base.txt --universal --generate-hashes --python-version 3.12 --no-header -o requirements/production.lock`, keeping the header comment), run the **full test suite in a clean environment built only from the lock**, run `pip-audit -r requirements/production.lock`, and only then commit it. Do not upgrade packages on the Dell.

## 13. Should the application start automatically at sign-in? (decision S-06: disabled by default)
**Decided: automatic start stays OFF by default; the owner uses the desktop shortcut; the jobs task is automatic.**
- The jobs task (always registered) already covers everything that must happen without the owner: forfeitures, reminders, e-mail retries and the daily backup with catch-up. It does not need the web server.
- Starting the server at sign-in uses memory and keeps a local service open all day for an application used for minutes at a time; a deliberate start also makes the owner see the start-up checks (missing key, pending update) at a moment she can act.
- If you want it anyway: `register_tasks.ps1 -AutoStartApplication` starts the same launcher silently (no window, no browser, only after sign-in, loopback only). A second copy is impossible (the launcher and server refuse one), the server stops with **Stop Library**, and the jobs task and the launch catch-up are unaffected (`run_daily_jobs` is idempotent).
- A **Windows service for Django is not recommended**: services run before sign-in, outside the owner's Credential Manager, so the backup key and secrets would not be reachable; it would need a stored password or a second key store, and it adds a service-account attack surface.
