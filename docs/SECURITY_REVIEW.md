# Security review — end of Phase 7 (development machine)

Scope: the whole application as implemented through Phase 7c. Method: reading the code and configuration, the automated tests (1,000+, PostgreSQL-backed), `manage.py check --deploy`, `pip-audit` on `requirements/base.txt` (no known vulnerabilities on the review date), a secret scan of every tracked and new file (nothing found), and a real browser smoke test of the production settings under waitress on 127.0.0.1 (login, pages, theme switch, download, CSP blocking an injected inline script; Chromium on the development machine, NOT the Dell). **This review does not cover the Dell: every Windows 11 item below is UNVERIFIED.**

## Controls and their evidence
| Area | Control | Evidence |
|---|---|---|
| Network exposure (B37) | `serve_local` accepts only `127.0.0.1`; `ALLOWED_HOSTS` must be loopback (system check `coven.E010`); waitress binds IPv4 loopback; a foreign `Host` header gets 400 | `core/tests/test_hardening.py`; smoke test: connection to the machine's other address refused |
| Browser protections | Content-Security-Policy (own scripts/styles/images only, no framing, no plug-ins), `X-Frame-Options: DENY`, nosniff, referrer, permissions, COOP/CORP; authenticated pages `no-store`; templates scanned for inline scripts, handlers and remote resources | `SecurityHeaderTests`, `TemplatesFitThePolicyTests`; smoke test (an injected inline script is blocked) |
| Authentication | Single owner, password hashing, email reset (1 hour), CLI recovery code (single-use, hashed, 15 minutes), 5 failures / 15 minutes throttled and audited, no lockout, sessions end at day end; every route requires login except the login family (swept by a test) | Phase 1b/1c tests, `tests/test_access.py` |
| Sessions and CSRF | HttpOnly, SameSite=Lax session and CSRF cookies; CSRF on every POST (the ZIP export is POST-only) | settings tests; `test_csrf_is_enforced_for_the_export` |
| Database privileges (L6) | `coven_owner` (schema, migrations, restores) vs `coven_app` (runtime); no default grants; every table classified; append-only and no-delete tables enforced by privileges AND triggers with `search_path` pinned; the application refuses to start on an insecure setup | `core/tests/test_runtime_privileges.py` (an end-to-end flow including a full backup runs as `coven_app`), `check_db_security` under both roles |
| Immutability | ledger, receipts, token ledger, loans, notifications, audit, backups history, restore history are append-only or guarded | trigger tests per table |
| Backups | AES-256-GCM, chunked, authenticated (tamper, truncation, splice, reorder all detected); key only in the Credential Manager (never in files, database, logs, backups); recovery key shown once with a checksum; upload verified by size and SHA-256 | `backups/tests` (112 + 33 rehearsal tests) |
| Restore | safe extraction (no traversal, links, unexpected files), every file verified against the manifest, compatibility refusals before any change, safety backup before replacing, verification after | `backups/tests/test_restore.py` |
| Privacy | anonymisation erases notification content inside its transaction; reports/statements/exports mask member-linked free text and payment references for anonymised members; donor data and message text are never exported; formula injection escaped; strict phone exemption | `communications/tests`, `reports/tests`, `finance/tests/test_receipt_access.py` |
| Secrets | none in the repository; `.env.example` has placeholders; SMTP and Google credentials come from the environment or the Credential Manager; error texts are scrubbed of passwords and addresses | secret scan; tests for scrubbing |
| Dependencies | pinned in `requirements/base.txt`; `pip-audit`: no known vulnerabilities (review date) | `pip-audit` |
| Upload and request limits | spreadsheet imports 5 MB (zip-bomb checks), waitress request body 10 MB | import tests, `serve_local` test |

## Findings (open items and accepted risks)
1. **Windows 11 is unverified** (R-08, R-61, R-65): PowerShell scripts, Task Scheduler behaviour, Credential Manager, `pg_dump`/`pg_restore` on PATH, the browser check on the Dell. Run `deploy\windows\verify_runtime.ps1` and record it.
2. **Recovery is unverified until a clean-machine restore is done** (R-05, R-65). Everything restore-related is proved only in disposable databases on the development machine.
3. **Google Drive is untested against real Google** (R-60): OAuth consent, quotas, the checksum fields Drive reports, folder creation.
4. **No HTTPS, cookies not `Secure`** (accepted: local plain HTTP; the four `check --deploy` warnings are exactly these). Confirm in the Dell's browser that login and CSRF work with the current settings; set `SESSION_COOKIE_SECURE`/`CSRF_COOKIE_SECURE` only if the browser accepts them on `http://127.0.0.1`.
5. **The CSP allows inline styles** (`style-src 'unsafe-inline'`) because the templates use `style=` attributes; scripts are strict. Moving styles into the stylesheet would allow removing it (low priority).
6. **Owner database password in a temporary `.env.owner`** during upgrades (decision D-5, plaintext, ACL-restricted, deleted afterwards).
7. **Backups hold all personal data** (R-63) and earlier backups keep data of members anonymised later (R-28); protected by encryption and key custody only. Losing the recovery key together with the computer loses the backups (R-62).
8. **Scheduled tasks run as the signed-in owner account** because the key is in that account's Credential Manager: no backup runs while nobody is signed in (a catch-up runs at the next sign-in).
9. **Database-assisted retention floor** (R-55) and **receipt references** (R-59) are documented, accepted limitations.
10. **Replacing a database wipes before restoring** (R-64); a verified safety backup is mandatory.
11. **Not assessed:** penetration testing, the physical security of the Dell, Windows hardening, the owner's email account.

## Conclusion
On the development machine, nothing found requires a code change before the Dell installation. The project must not be described as "recoverable" or "deployed" until items 1–3 have been verified and recorded.
