# Operations — Coven's Library

For the owner and whoever helps them. Commands are run in PowerShell in the application folder with the virtual environment's Python (`.venv\Scripts\python manage.py ...`), as the runtime account unless a step says "owner". Written from the development machine; Windows-specific steps are UNVERIFIED until rehearsed on the Dell.

## 0. Every morning, in three lines (owner)
1. Double-click **Coven's Library** on the desktop. Wait for "Coven's Library is ready."; the browser opens by itself.
2. Work as usual. If a window ever says "Could not start the application. Contact support.", do not retry repeatedly: write down the first line of the message and call your support person (details are saved for them).
3. When you finish, double-click **Stop Library** (or sign out). Nothing needs to be saved first.
No PowerShell, no commands, no passwords to type except your login to the library page.

## 1. What runs by itself
The Task Scheduler jobs task (`docs/DEPLOYMENT.md` section 6): `run_daily_jobs` at logon and every 5 minutes (the application itself is started by the owner with the desktop shortcut; an optional logon task can start it too). Together they post day-8 forfeitures, send emails (and their retries), create the 09:00 reminders, and make the daily encrypted backup (default 19:45, changeable in Settings). If the computer was off, work happens at the next launch.

## 2. Every day (30 seconds)
Open the dashboard. Look at **Backup failures** (it says "No backup problems." when all is well; it warns when the last good backup is older than 26 hours or none exists), **Failed emails**, **Damaged / lost decisions** and **Token forfeitures**. A backup warning is the one to act on the same day (section 5).

## 3. Every month
Run `python manage.py backup_status` (key present, last success, retention plan). Once a quarter, rehearse a restore into a scratch database (section 6.2). After any Windows reinstall or account change, check that the backup key is still in the Credential Manager (`backup_status` shows `Backup key: present`).

## 4. Backups
- **What:** the whole database (`pg_dump -Fc`), the `media/` folder and the application assets, with a manifest of every file's hash, encrypted with AES-256-GCM and uploaded to the Google Drive folder "Coven's Library Backups"; the upload is verified (size and SHA-256 reported by Drive) before the backup counts.
- **Kept:** the newest 30 daily backups and the last backup of each of the 12 most recent completed months. Older ones are removed from Drive; the history rows stay.
- **Keys:** the working key is in the Credential Manager of the owner's Windows account. The **recovery key** is the same key as printable text, shown once at setup (`show_recovery_key` shows it again after a typed confirmation; both are audited). **Keep the recovery key on paper, away from this computer and away from any backup. If the recovery key and the computer are both lost, no backup can be opened — nobody can recover them.** The key is never replaced automatically.
- **Personal data in backups:** anonymising a member changes the live database only; earlier backups keep the earlier data until they expire (R-28).

## 5. When the dashboard shows a backup problem
1. `python manage.py backup_status` — read the problem and the failure messages.
2. "no backup key is set up": run `setup_backup_key` (first time) or restore the key from the recovery key (section 6.1).
3. "Google Drive is not connected" or an upload error: `connect_drive` again (consent may have been revoked), then `run_backup`.
4. "pg_dump was not found": set `PG_DUMP_PATH` or add PostgreSQL's `bin` folder to PATH.
5. After fixing, `python manage.py run_backup` and confirm "verified". The dashboard clears when a backup succeeds.

## 6. Restore and recovery
### 6.1 Replacement computer (the real recovery)
1. Install Windows updates, PostgreSQL 16 (same major version), Python 3.12, and the application (`docs/DEPLOYMENT.md` sections 1–3). Provision the roles and an EMPTY database owned by `coven_owner`.
2. Install the Drive client credentials again if you want to download from Drive (`connect_drive`, fresh consent), or copy the `.cbk` file from Drive by hand.
3. With a temporary `.env.owner`: `python manage.py restore_backup <file.cbk> --settings=config.settings.migrate`. It asks you to type `RESTORE <database>`; if the key is not in this computer's Credential Manager it asks for the **recovery key** (hidden) and for `RESTORE KEY`, then saves the key into the Credential Manager and reads it back.
4. The command decrypts and verifies every file, checks compatibility (PostgreSQL major version, migrations), restores the database and media, re-applies the runtime grants, and verifies row counts, migrations, ledger sums and database security. It prints each step.
5. `check_db_security` under both roles; `preflight_notifications`; run the application and look at the dashboard, a member and a report. Register the tasks. Delete `.env.owner`.
### 6.2 Rehearsal (do this before you need it)
Restore the newest backup into a scratch database on the same computer: create it as `postgres` (`CREATE DATABASE coven_rehearsal OWNER coven_owner;`), then `restore_backup <file> --target-database coven_rehearsal --confirm "RESTORE coven_rehearsal" --settings=config.settings.migrate`, compare a few figures with the live application, then drop the scratch database. The same command on a different, clean computer is the **clean-machine rehearsal**.
### 6.3 Replacing the live database (rare)
**A restore into the live database requires the application and all scheduled tasks to be stopped first** (end the "Coven's Library" application and run `register_tasks.ps1 -Remove`, or disable both tasks in Task Scheduler, and confirm no `python`/`waitress` process is running). The restore holds a database lock that stops backup *retention* from running during the restore (retention then skips and runs again later), and the restore refuses if retention is running. That lock does NOT stop a regular daily backup from starting during the restore, so stopping the tasks is the only protection against that. Register the tasks again after the restore has been verified.

`restore_backup <file> --replace --safety-destination local:<folder> --settings=config.settings.migrate`. A verified safety backup of what is being replaced is taken first; if it fails nothing changes. Then the old contents are removed and the chosen backup restored. If the restore fails after the removal, restore the safety backup.
### 6.4 Rehearsal record (copy and fill in; keep it)
Date / computer / Windows edition and build / PostgreSQL version / backup file / key source (Credential Manager or recovery key) / every step of `restore_backup` printed `ok` (yes/no) / `check_db_security` owner and runtime (0 errors?) / figures compared / problems found / signed. **Until a record for a clean Windows 11 computer exists, recovery is NOT verified.**

## 7. Passwords and access
Owner login: password reset by email, or the local CLI (`python manage.py issue_recovery_code`, a single-use code shown once, valid 15 minutes); five failed logins in 15 minutes are throttled (no lockout). The runtime database role cannot change the schema or edit history. The owner database password is typed (hidden) into `deploy\windows\Run-AsOwner.ps1` when needed and kept only in that process's memory; on a development machine a temporary `.env.owner` may be used. The runtime secrets are in the Windows Credential Manager only; a plaintext environment variable of the same name is refused at start-up.

**Backup-key rotation, rollback, pruning and recovery on a replacement computer** are in `docs/KEY_ROTATION_AND_RECOVERY.md` (decision S-04). Never delete an old paper recovery key until `prune_backup_keys` says no backup at the destination needs it (it now inspects the real destination and refuses when unsure). `import_backup_key` restores a missing key from paper. `backup_status` shows every key and whether a rotation is in progress.

## 8. Troubleshooting
- **Dashboard unreachable:** start it with the desktop shortcut (the optional "application" task only exists if it was registered with `-AutoStartApplication`); a failure message names the problem in plain words and the details are in `%LOCALAPPDATA%\CovensLibrary\logs\launcher.log`.
- **"The Windows credential store could not be read":** the application refuses to start and creates nothing. Sign in as the owner's Windows account (not another), check Windows Credentials, and contact support; never reset the Windows password through an administrator (the saved secrets and backup keys become unreadable).
- **"Database security" error at start:** the application refuses to run on an insecure database setup; run `check_db_security` and follow `docs/DATABASE_SECURITY.md`.
- **Emails not sent:** Reminders page → status and error; SMTP settings in the environment; "Send test email".
- **A task did nothing after the computer slept:** "start when available" runs it at wake; the jobs task also runs every 5 minutes while you are signed in.
- **Port in use:** another copy is running; end it in Task Manager.
