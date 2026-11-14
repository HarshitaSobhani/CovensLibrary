# Backup-key rotation and recovery on a replacement computer

Decision S-04 (owner): a safe backup-key rotation exists **before** production use. Decision S-01: a missing, unreadable or invalid backup key blocks start-up. Everything here was built and tested on the development machine only (disposable databases, a memory key store). **The Windows Credential Manager, the Dell, real Google Drive and a clean-machine restore are UNVERIFIED**; no part of this is a claim that recovery works on the Dell until the rehearsal in section 7 has been done and recorded.

## 1. How backup encryption and keys work (the architecture this builds on)
- Every backup is one `.cbk` file: AES-256-GCM, chunked, authenticated. Its header names the key that made it by a **fingerprint** (16 hex characters, public, not secret).
- Keys live in the **Windows Credential Manager** (service "Coven's Library"), in three kinds of slot:
  | Slot | Holds | Used for |
  |---|---|---|
  | `backup-working-key` | the **current** key | all NEW backups |
  | `backup-retired-<fingerprint>` (+ a list `backup-retired-index`) | an **old** key | opening the backups it made |
  | `backup-pending-key` | a **new** key during a rotation, before it is committed | recovery after an interruption |
- The **recovery key** of a key is the same 32 bytes written as printable groups with a checksum (`ABCDE-FGHIJ-…`). It is shown once, on paper, away from the computer. **One paper key per key.** Losing a paper key *and* the computer loses every backup made with that key.
- Restore finds the key by the file's fingerprint in the slots; if it is not there it asks for that key's recovery key, checks the fingerprint, and (after "RESTORE KEY") saves it: **into the working slot only if that slot is empty, otherwise as a retired key. It never overwrites a key that is already there.**
- The database records each backup's key fingerprint. "The current key" means the key of the newest verified daily backup.

## 2. Rules that cannot be bypassed
1. The Credential Manager failing (locked, damaged, unavailable) **stops** the operation. Nothing falls back to a file, a variable or a made-up key.
2. A replacement encryption key is **never created silently**: only `setup_backup_key` (when no key exists at all) and the confirmed `rotate_backup_key` create keys; an unreadable existing key is an error, not "no key".
3. An existing credential is **never replaced silently**: `save_working_key`, every key slot, the runtime secrets (`store_secrets.py` needs `--replace NAME`) and the Google connection (`connect_drive` needs `--replace`) refuse. The only sanctioned replacements are the typed-confirmation commands below.
4. The audit log gets **fingerprints only**, never key material (tested by searching every audit and history row for the key and its recovery text).
5. The old key and its paper key are kept until `prune_backup_keys` shows that no retained backup needs them, and even then only on your typed confirmation.

## 3. Rotating the key (when and how)
When: after any suspicion that the key or its paper copy was seen (INCIDENT_RESPONSE), when staff who knew the paper key change, or on a schedule you choose. **Never during a restore, an upgrade or a backup failure.** Do not rotate in production until it has been rehearsed on a copy.

You need: the **current** paper recovery key, a pen and paper for the new one, a working Drive/destination, and about 10 minutes. Run as the owner's Windows account (the Credential Manager belongs to it).

1. **Preflight (changes nothing):** `deploy\windows\Invoke-Manage.ps1 rotate_backup_key --dry-run` — checks that the current key reads, no rotation is in progress, the credential store can store and return a test value, a **verified backup with the current key exists from the last 26 hours** (run `run_backup` first if not), and that backup is still intact at the destination. Fix every `FAILED` line first.
2. **Rotate:** `Invoke-Manage.ps1 rotate_backup_key`. It asks you to type `ROTATE BACKUP KEY`, then the **current recovery key** from paper (hidden — proof you still have the old way back). It then:
   1. creates the new key and saves it in the **pending** slot (persisted before anything else) and records `backup.key_rotation_started` (fingerprints only);
   2. shows the **new recovery key once** — write it on paper, label it with its fingerprint and today's date — and asks you to type it back; a wrong entry stops with nothing committed;
   3. keeps the old key as a verified **retired** copy;
   4. **proves the retired copy** by downloading the newest old-key backup and decrypting it;
   5. makes a **verification backup with the new key**, verifies it at the destination, downloads it again and decrypts it with the pending key;
   6. only then **commits** (the new key becomes the working key, read back) and records `backup.key_rotation_committed`, then removes the pending slot.
3. **After:** keep **both** paper keys. `backup_status` shows every key and how many retained backups need it. New backups use the new key; old backups still open.

### If it is interrupted (power cut, crash, closed window)
The working key only changes at step 6, so the old key is always intact. `backup_status` says "A key rotation is IN PROGRESS".
- **Finish it:** `rotate_backup_key --resume` (it works out where it stopped: it re-asks for the new recovery key, reuses a verification backup that already succeeded, and finishes a half-written commit without recording it twice).
- **Cancel it (only if no backup uses the new key):** `rotate_backup_key --abort [--destination ...]` (type `ABORT KEY ROTATION`). Abort first reads the REAL destination: every file's header fingerprint, plus the history and any running backup. It refuses, and tells you to use `--resume`, if any backup uses the new key (for example the verification backup), if the destination cannot be fully listed or any item cannot be inspected, or if a backup is running. The new key is never deleted in those cases. When it proceeds, the old key stays the working key and any retired copy is kept.
- **New key missing from the credential store, but a backup needs it:** `import_backup_key --as pending` (hidden prompt for the paper recovery key, checksum-validated, must open a real backup; type `IMPORT BACKUP KEY`; never overwrites a slot; prints the fingerprint only). Then `rotate_backup_key --resume`. For an old retired key use `--as retired`.

### Rollback (undo a rotation, or fix the key after restoring)
`rotate_backup_key --set-current <fingerprint>` (type `SET CURRENT BACKUP KEY`): makes a retained key the working key; the key that was working becomes a retired key. Nothing is deleted and every backup still opens. Use it to go back to the old key, or after restoring on a new computer if `backup_status` shows the working key is not the newest backup's key.

### Removing an old key later (never automatic)
`prune_backup_keys` lists each retired key and how many retained backups need it. `prune_backup_keys --fingerprint <fp>` (type `REMOVE OLD BACKUP KEY`) removes it **only if the destination PROVES no backup needs it**: it lists the whole destination (every Drive page; trashed files count), reads every header, and must reconcile the destination with the history. It refuses on an unreadable or incomplete listing, a malformed header, an unknown file, a duplicate name, a history row missing at the destination, or an empty destination while history lists backups (for example after restoring an older database: reconcile by hand before pruning). Unknown files are never treated as irrelevant. Restore `--recovery-key-file` is discouraged (hidden prompt preferred); such a file must be outside the application folder and unreadable by others, and should be deleted after use (the daily/monthly retention eventually drops the old backups). Destroy the matching paper key only if you hold no other copy of a backup made with it (for example a USB copy).

### The honest limit
Rotation protects **new** backups. If an old key was exposed, every old backup made with it is readable by anyone who obtains those files, until retention removes them from Drive. For a real exposure: revoke Drive access (INCIDENT_RESPONSE step 4), rotate, and delete old backups from Drive deliberately once enough new, verified ones exist. Re-encrypting old backups is not built.

## 4. What to keep on paper (a table to fill in)
| Fingerprint | Role | Made backups from → to | Kept until |
|---|---|---|---|
| | current | | |
| | retired | | `prune_backup_keys` says "not needed" |

`show_recovery_key --all` prints every key the computer currently holds, each with its fingerprint and the backups it protects (it needs `SHOW` typed and is audited). Print it once to paper and store it with the BitLocker recovery key's safe place — **not** on the computer and **not** inside a backup.

## 5. Recovery on a replacement Windows 11 computer — exactly
You need: the **paper recovery key of the newest backup** (at least), a Google account login (or the `.cbk` file on a USB stick), the owner database password you choose for the new PostgreSQL, and the application files. Do these in order.

1. **Windows and BitLocker.** Install Windows 11; turn on BitLocker / device encryption and store *its* recovery key off the computer (decision S-05). `Setup-CovensLibrary.ps1` stops if BitLocker is not protecting the drive.
2. **PostgreSQL 16** as a service, listening on 127.0.0.1 only; provision the roles and create the **empty** database owned by `coven_owner` (DEPLOYMENT section 2). Choose new passwords.
3. **The application:** copy or clone it. First an administrator runs `Verify-BitLocker-Admin.ps1` on this computer (DEPLOYMENT section 10, step 1). Then, signed in as the owner and NOT elevated, run `deploy\windows\Setup-CovensLibrary.ps1`. It checks the BitLocker record, creates the environment from the hash-locked packages, creates the protected folders, **saves the new runtime secrets in the Credential Manager** (a new Django key and the new `coven_app` password: sessions and reset links from the old computer stop working, which is intended), collects the display files and makes the shortcuts. It will report that the backup key is missing: expected.
4. **Get the newest backup file.** Either `deploy\windows\Invoke-Manage.ps1 connect_drive` (a fresh Google consent is required; use the **same OAuth client** as before — keep the client ID/secret in your password manager — because with the `drive.file` scope the app only sees files its own client created) and `run_backup`'s folder "Coven's Library Backups"; or open Google Drive in a browser, open that folder and download the newest `coven-backup-….cbk`; or take it from your USB copy. *Unverified against real Google.*
5. **Restore the NEWEST backup first** (so its key becomes the working key): `deploy\windows\Run-AsOwner.ps1 restore_backup "C:\path\coven-backup-….cbk"`. Type the owner password (hidden), then `RESTORE <database name>`, paste/type the **recovery key** for that file (hidden) and `RESTORE KEY`. It decrypts and verifies every file, checks compatibility, restores the database and media, re-applies the runtime grants and verifies row counts, migrations, payment sums, token balances and database security. Every line must say `ok`. The key is saved as the **working** key because the store was empty.
6. **A key missing from the credential store** (for example after Windows was reinstalled but backups at the destination still use it): `import_backup_key --as retired` (hidden paper-key prompt; it must open a real backup) puts it back; nothing is overwritten. **Only if you need an older backup:** restore it into a scratch database (`--target-database`) with **its own paper key**; that key is kept as a **retired** key and never replaces the working one.
7. **Verify:** `Run-AsOwner.ps1 check_db_security` and `Invoke-Manage.ps1 check_db_security` (0 errors each); `Invoke-Manage.ps1 backup_status` (every key listed as "key available"; no rotation in progress); `Invoke-Manage.ps1 launcher_preflight` passes. If the working key is not the newest backup's key, `rotate_backup_key --set-current <fingerprint>`.
8. **Resume:** `register_tasks.ps1`, start with the shortcut, then `Invoke-Manage.ps1 run_backup` and confirm it says stored and verified. Record the whole procedure (OPERATIONS section 6.3). **Until this has been done on a clean Windows 11 computer and recorded, recovery is not verified.**

Failure cases: a wrong paper key is refused before anything is touched ("belongs to a different key"); a mistyped one is caught by its checksum; a damaged backup is refused before the database changes; the Credential Manager unreadable stops the restore with an error (nothing falls back).

## 6. Residual risks of this design
The new key is generated on the same computer that uses it; a same-account attacker can read all keys in the Credential Manager; losing a paper key loses the backups it protects; there is no automatic expiry of old keys; the maintenance lock is per database (a restore into another database is not excluded); real Credential Manager behaviour (entry limits, roaming, administrator password resets that make DPAPI entries unreadable) is unverified.

## 7. Windows rehearsal required before production
1. Rotate on a **copy**: `--dry-run`, a full rotation, `backup_status`, a restore of an old and a new backup on a second computer using only the paper keys.
2. Interrupt a rotation (close the window after step 2) and `--resume`; repeat and `--abort` before the verification backup (allowed) and after it (must be refused with "use --resume"). Use disposable test keys and a test Drive folder, never production keys.
3. `--set-current` back and forth; `prune_backup_keys --dry-run`, then a real prune on the test folder; place an extra old-key file in the test folder and confirm prune refuses.
3a. Delete the pending slot from the Credential Manager on the test machine and recover it with `import_backup_key --as pending`, then `--resume`.
4. Confirm the Credential Manager shows the slots `backup-working-key`, `backup-retired-…`, and that `backup-pending-key` is gone afterwards.
