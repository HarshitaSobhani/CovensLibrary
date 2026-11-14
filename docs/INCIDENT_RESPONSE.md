# If you think the library has been compromised — what to do

Written for the owner, in plain language. **Stay calm: the records of money, tokens, loans and history cannot be edited or erased by the application, so what happened can be found out.** Do the steps in order. Write down what you did and when.

Signs: a computer you do not recognise signed in, an unexpected e-mail about a password reset, a "recovery key shown" or export you did not do, backups missing in Google Drive, the computer behaving strangely, a lost or stolen Dell, or anyone else knowing your Windows password.

## 1. Stop the application
Double-click **Stop Library**. If it says it could not stop, restart the computer.

## 2. Cut the network if you suspect someone is inside the computer
Turn off Wi-Fi / unplug the network cable. (The application is only ever reachable from the computer itself, so this protects your Google and e-mail accounts, not the library page.)

## 3. Keep the evidence — do not delete anything
Copy the folder `%LOCALAPPDATA%\CovensLibrary\logs` and the folder `%LOCALAPPDATA%\CovensLibrary\run` to a USB stick. Do not run clean-up tools, do not reinstall Windows yet. If the Dell was stolen, skip to step 4 from another computer.

## 4. Cut off access from other places (do this from another, trusted device)
1. **Google Drive:** sign in to your Google account → *Security* → *Third-party apps with account access* → remove "Coven's Library". Change the Google password.
2. **E-mail used for reminders:** change its password (and the app password if you use one).
3. **Windows:** change the Windows account password **yourself, when signed in** (do not let an administrator "reset" it: that makes the saved secrets unreadable). Sign out other users.

## 5. Check that the backups are good
Backups are encrypted, so a thief cannot read them without the key — but they can be deleted or damaged. Ask your support person to run `backup_status` and a practice restore into a scratch database (`docs/OPERATIONS.md` section 6.2). A backup that restores correctly is a known-good backup. Prefer the newest one made **before** the suspicious event.

## 6. Replace the secrets (support person, after the computer is trusted again)
| Secret | How | Note |
|---|---|---|
| Database password of `coven_app` | `ALTER ROLE coven_app PASSWORD '…'` as `postgres`, then `store_secrets.py --replace DB_PASSWORD` | the old one is useless afterwards |
| Django secret key | `store_secrets.py --replace DJANGO_SECRET_KEY` | signs everyone out and cancels password-reset links |
| E-mail password | change at the provider, then `store_secrets.py --replace EMAIL_HOST_PASSWORD` | |
| Owner login password | `issue_recovery_code --username …` then use the recovery page | the code is single-use and expires in 15 minutes |
| Google Drive | `connect_drive` again after step 4 | new consent |
| **Backup encryption key** | `rotate_backup_key --dry-run`, then `rotate_backup_key` (typed confirmations; you need the CURRENT paper recovery key and a pen for the new one). The old key is kept and proven, so old backups still open; an interrupted rotation is finished with `--resume`; `--set-current` rolls back; `--abort` is refused while any backup at the destination uses the new key (use `--resume`); a missing key is restored from paper with `import_backup_key`. Full procedure: `docs/KEY_ROTATION_AND_RECOVERY.md`. **Rotation protects NEW backups**: if the old key may have been seen, old backups on Drive stay readable by anyone who gets those files until retention removes them, so revoke Drive access first (step 4) and delete old Drive backups deliberately once enough new verified ones exist | never delete the only good backup before a new one is verified; keep every paper key until `prune_backup_keys` (which inspects the real destination and refuses when unsure) says it is not needed |

Never type secrets into chat, e-mail or a ticket.

## 7. Restore from a known-good backup (only if data was damaged or you cannot trust the computer)
Follow `docs/OPERATIONS.md` section 6 on a clean, trusted Windows 11 computer (recovery key from your paper copy). The restore refuses a damaged or foreign backup and makes a safety copy first. **Recovery has not yet been rehearsed on the Dell: until it has, treat this step as unproven.**

## 8. Check the database is still protected
`check_db_security` as the owner role and as the runtime role must both say 0 errors. Run `verify_runtime.ps1`. Ask your support person to review the audit trail (it is a table in the database; there is no screen for it yet): logins and failed logins, "recovery key shown", exports, backups, and any adjustment or reversal you do not recognise. Money records cannot be altered, only added to — an unexpected new entry is the thing to look for.

## 9. Start again safely
Reconnect the network only after steps 4–8. Start with the desktop shortcut. Watch the dashboard for a day (backup status, failed e-mails). Keep your notes and the copied logs.

## What not to do
Do not try to "clean" the database by hand; do not delete the logs or the old backups; do not give your Windows or Google password to anyone who contacts you; do not restore over the live database without the safety copy.

Also: **if the Windows credential store cannot be read** the application refuses to start and creates nothing; do not reset the Windows password through an administrator.

*Support contact: write your support person's name and number here:* ____________________
