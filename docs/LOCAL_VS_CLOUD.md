# Local-first (the Dell) versus cloud hosting — decision memo

Status: memo for the owner's decision; written from the development machine. **No cloud deployment has been built or tested.**

## Today: local on the Dell (the approved design)
The application runs on one Windows 11 computer and answers only on `127.0.0.1`. One owner, ~50 members, ~200 editions.
- **Cost:** none beyond the computer, electricity and the Google Drive space used by backups.
- **Personal data:** stays on that computer; backups are encrypted before they leave it.
- **Availability:** only when the Dell is on and the owner is signed in; scheduled work (reminders, retries, the day-8 forfeiture, the backup) happens at the next launch if it was off. There is no access from another place or device.
- **Security surface:** no network listener, no HTTPS needed, no public attack surface. The remaining risks are the computer itself: theft, loss, disk failure, a spilled drink (R-01), mitigated by encrypted daily backups and a rehearsed replacement-computer restore.
- **Effort:** the owner is the administrator (updates, backups, key custody).

## Cloud option (not built)
A small virtual machine or managed container with a managed PostgreSQL, behind HTTPS.
- **Cost:** a monthly hosting bill (server, database, backups, domain/certificate) that is paid for as long as the library runs; usually more than the local option.
- **Personal data:** member names, mobile numbers, emails and the finance ledger would sit in a data centre under the hosting provider's terms; the data location (country) and the provider's access need a decision. Anonymisation and backup disclosure (R-28) apply to the provider's snapshots too.
- **Availability:** always on, reachable from any browser; email retries and reminders no longer depend on the Dell being awake.
- **Security surface:** the application becomes reachable from the internet: it would then need HTTPS, `Secure` cookies and HSTS (currently off because the site is plain local HTTP), a real firewall, rate limiting beyond the login throttle, intrusion monitoring, and a review of every assumption that "localhost only" makes (B37). The owner-only login and the two-role database model remain, but the threat model changes completely.
- **Effort:** someone must patch the server, rotate secrets, watch the bill and the uptime.

## Migration path (designed to be cheap)
Cloud migration is **backup -> restore**: the same encrypted backup and `restore_backup` procedure restore into a managed PostgreSQL instance provisioned with the same two roles (`coven_owner`, `coven_app`). The code is OS-neutral; configuration comes from environment variables only. Before any cloud move: decide the hosting country and provider, add HTTPS and the cookie/HSTS settings, replace Task Scheduler with the provider's scheduler (the single idempotent `run_daily_jobs` command is the unit to schedule), replace the Windows Credential Manager key store with the provider's secret store (the key store is behind one small interface), and repeat the security review.

## Recommendation
Stay local while the library is one room, one owner and one computer, and spend the effort on the **clean-machine restore rehearsal**, which is what actually protects the data. Revisit cloud hosting only if the owner needs access from other places, the Dell proves unreliable, or the library grows beyond one owner. This memo does not change any approved decision; the choice is the owner's.
