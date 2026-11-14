# Security audit — Coven's Library

Date of this pass: 14 November 2026. Scope: the application as committed at `a710b68` plus the changes of this pass (uncommitted). Method: reading the code, the existing tests, **and attacking the running application** with automated probes written for this audit (they are now permanent regression tests). **This is a developer-run assessment, not an independent penetration test.** Nothing here was run on the Dell: every Windows 11 item is marked UNVERIFIED.

Legend — *Status*: **Fixed + tested** (code changed, a test fails without the fix), **Accepted** (a decision, residual risk documented), **Open** (needs an action or approval), **Windows-unverified**.

## 1. What was verified by attack (no finding)

| Area | What was done | Result | Where it lives |
|---|---|---|---|
| Unauthenticated access | every route × GET/POST/PUT/DELETE as an anonymous visitor (about 150 requests) | always redirected to login, 403 or 405; no data, no side effect | `tests/test_security_attacks.py::Unauthenticated` |
| Account enumeration | wrong user vs wrong password; reset request for known vs unknown address | identical responses; no email to unknown addresses | same, `PasswordReset` |
| CSRF | POST without a token to every route while logged in (64 routes); a token from another session | all 403; nothing written | `Csrf` |
| Session fixation / replay | planted session id before login; replay after logout; replay after expiry; cookie lifetime | new key on login; old/replayed/expired sessions refused; cookie ≤ end of day, HttpOnly, SameSite=Lax | `SessionsAndRedirects` |
| Open redirect | `next=` with 7 hostile forms | never leaves the site | same |
| Login throttle | 5 failures with a *forged* `X-Forwarded-For` per attempt | still throttled (the limiter uses the socket address only) | same |
| Password reset | single use, rate limit (3/15 min), token not in the audit log | as designed | `PasswordReset` |
| XSS | script/img/svg/template payloads in member names, notes, titles, authors, search terms, filters, month, receipt, status on 15 pages | never reaches the HTML unescaped | `InputAttacks` |
| SQL injection | quotes, UNION, NUL, backslash in every search/filter parameter | handled as data; tables intact; no raw SQL in views (grep) | `InputAttacks` |
| Object access | absurd ids (`99999999999999999999999`) on member/receipt/transaction routes | 404, never 500. There is one owner: "object-level authorization" reduces to login | `InputAttacks` |
| Error pages | a forced crash with secret-looking text in the exception | generic 500; no traceback, path, setting or exception text | `ErrorPages` |
| Host header | foreign / look-alike hosts; `X-Forwarded-Host` | 400 for foreign hosts; forwarding header ignored and never used to build links | `ErrorPages` |
| Static files / traversal / dotfiles | 16 hostile paths against the real **production settings under waitress** (`/static/../.env`, encoded variants, `.git`, `/media`, `/admin`) | all 404; no file content | live probe (section 6) |
| Headers | real response of the live server | CSP, `X-Frame-Options: DENY`, nosniff, referrer, COOP/CORP, `no-store` on authenticated pages; no `Server`/`X-Powered-By` | live probe; `core/tests/test_hardening.py` |
| Methods / malformed requests | TRACE, OPTIONS, PUT, a 11 MB body, bad `Content-Length`, conflicting `Transfer-Encoding`, 70 KB URL | 405/403/400/reset; no crash | live probe |
| Uploads (spreadsheet import) | external-entity (XXE) workbook, entity-expansion bomb, non-workbooks, truncated zip, traversal member names, oversize | no local file read, bounded, refused, nothing written outside the batch | `catalogue/tests/test_import_attacks.py` |
| File downloads | names of downloads | fixed server-side names; never taken from input | `InputAttacks` |
| Database, as `coven_app` | server-side file read, `COPY ... PROGRAM`, `ALTER SYSTEM`, large objects, reading `pg_authid`, terminating other sessions, strong table locks, `SET SESSION AUTHORIZATION`, plus the 60+ attacks of `test_runtime_privileges.py` (DDL, trigger removal, truncate, grants, role changes) | all denied; the role owns nothing; no `PUBLIC` table grants; attributes minimal | `core/tests/test_db_attacks.py`, `core/tests/test_runtime_privileges.py` |
| Immutable history | UPDATE/DELETE/TRUNCATE on audit, ledger, payments, receipts, token ledger, loans, backup and restore history | rejected by privileges **and** triggers | same |
| Drift detection | extra grants, disabled/missing triggers, runtime CREATE, default privileges | detected by `check_db_security` (30+ tests) | `core/tests/test_dbsecurity.py` |
| Dependencies | `pip-audit` on `requirements/base.txt`, `dev.txt` and the new lock | no known vulnerabilities (14 Nov 2026) | scan |
| Secrets in the repository | `detect-secrets` (working tree), history search for `.env`/keys, tracked-file name scan | nothing real; matches are test fixtures and placeholders; `.env*` never tracked | scan |
| Static analysis | `bandit` medium+ over application code | nothing | scan |

## 2. Findings

*Updated after the owner's decisions S-01 to S-06: SEC-01, SEC-08 and SEC-13 below reflect them. New in this pass: fail-closed Credential Manager handling and "never replace silently" for every key and credential (section 3b).*

| ID | Severity | Component | Evidence / reproduction | Impact | Remediation | Status |
|---|---|---|---|---|---|---|
| **SEC-01** | Medium | Runtime secrets (`DJANGO_SECRET_KEY`, `DB_PASSWORD`, SMTP password) | `docs/DEPLOYMENT.md` §3 told the maintainer to store them as Windows *user environment variables*; `config/settings/production.py` could only read the environment | Plain text in the user's registry hive: visible to every process of that account, to registry exports, and readable offline from an unencrypted disk. The runtime database password gives read access to all data; the secret key signs sessions and reset links | **Decision S-02:** the Windows Credential Manager is the **only** source in production. Production settings refuse to load without the launcher's `COVEN_SECRET_STORE=credential-manager`; a plaintext variable with a secret's name is **refused**, never used; an unreadable store stops start-up (fail closed, distinct message); `put` never replaces an existing secret without `--replace`; `deploy/windows/store_secrets.py` saves them (hidden prompts, key generated, never printed) | **Fixed + tested** on the development machine (stub `keyring`, fresh-process import of the production settings, 28 tests). **Windows-unverified**: the real Credential Manager. Not a defence against code running as the same signed-in user |
| **SEC-02** | Low | `connect_drive` | The OAuth client secret was a *required* command-line argument and the documentation showed it typed in | Visible in the process list, PowerShell history and transcripts | Arguments are optional and discouraged; both values are asked for (secret hidden) | **Fixed + tested** (`ConnectDriveSecretHandlingTests`) |
| **SEC-03** | Low | Production settings | `DJANGO_DEBUG=true` with `config.settings.production` produced `DEBUG=True` (the flag was read from base; production never checked it) | Tracebacks, settings and SQL shown in the browser; static files served from finders | Production settings refuse to load with DEBUG enabled; the launcher forces `DJANGO_DEBUG=false` and preflight rejects it | **Fixed + tested** (`ProductionSettingsTests`) |
| **SEC-04** | Medium | Logging | No `LOGGING` setting: Django's default prints to the console. PostgreSQL errors carry values (`Key (mobile_e164)=(+91…) already exists`, `Failing row contains (…)`), request logs carry paths and query strings, and there was no protected, persistent log for the Dell | Personal data and secrets could land in unprotected console captures; no diagnostics for support | `core/safelog.py`: every record (message, arguments, traceback) is scrubbed (credentials, reset links, e-mail addresses, phone numbers, DB key/row details, query strings) before any handler; rotating file in the protected log folder (`COVEN_LOG_DIR`) | **Fixed + tested** (14 tests incl. end to end through a configured logger). Residual: the launcher captures the server's stdout/stderr into the same protected folder; a Python start-up traceback there is not scrubbed (it contains no database values) |
| **SEC-05** | Low | `audit/redaction.py` | `scrub_text("Authorization: Bearer ya29.…")` returned `Authorization: [redacted] ya29.…` — only the word *Bearer* was hidden (reproduced by the new test before the fix) | A Google access token inside an error text could reach an audit event or backup-run message (low likelihood: `DriveDestination` never forwards exception text) | `Authorization` rule redacts the whole value (`Bearer`/`Basic`/`Token` forms) | **Fixed + tested** (`AuthorizationHeaderTests`) |
| **SEC-06** | Low | Dependencies | `requirements/base.txt` pins 12 direct packages; the installed environment has ~50. Transitive versions floated and nothing was hash-checked | A later install could pull a different or substituted transitive package | `requirements/production.lock`: all 50 packages exact, SHA-256 hashes, platform markers (Windows `pywin32-ctypes`, `tzdata`); Setup installs with `--require-hashes --only-binary=:all:`. The **full test suite was run on an environment built only from the lock** (result in §5) | **Fixed + tested**; `pip-audit` clean. Regenerate only deliberately (DEPLOYMENT §12) |
| **SEC-07** | Informational | CSRF cookie | Lifetime was Django's default of one year | A long-lived token cookie | `CSRF_COOKIE_AGE = None` (browser-session cookie) | **Fixed + tested** |
| **SEC-08** | Medium → gate | Disk at rest (Windows) | The database files, local backup folder, logs and `app.env` are plain files; nothing checked BitLocker | If the Dell is lost or stolen with an unencrypted disk, every record is readable without any password | **Decision S-05: BitLocker is mandatory**, verified in a **two-account** flow: `Verify-BitLocker-Admin.ps1` (administrator, standalone, read-only, writes one machine-wide record in `%ProgramData%`, nothing in the administrator's profile) and the owner's non-elevated `Setup-CovensLibrary.ps1` (reads the record, asks the owner to confirm the recovery key is stored off the computer, refuses to run elevated or as an unconfirmed account). No script enables BitLocker, changes protectors or shows/logs a key (static tests forbid it) | **Implemented + static-tested**; **Windows-unverified** (the check runs only on the Dell) |
| **SEC-09** | Informational | Server process control | Nothing prevented two servers (different ports) or recorded which process was ours | Duplicate instances; "stop" could only be done by guessing a process | `serve_local` records itself, refuses a second instance (exit 3) and a foreign program on the port (exit 4), stops cleanly on a request file or SIGTERM, clears stale files | **Fixed + tested** (real server processes) |
| **SEC-10** | Low | CSP | `style-src 'unsafe-inline'` because templates use `style=` | An injected style could alter the page's look; scripts stay strict | None now (low value to change); moving styles into the stylesheet would remove it | **Accepted** |
| **SEC-11** | Informational | Cookies | Session/CSRF cookies are not `Secure` (plain `http://127.0.0.1`); `check --deploy` warns W004/W008/W012/W016 | Only relevant to network sniffing, and the application is never on the network | None; confirm in the Dell's browser that login works, then optionally enable `Secure` | **Accepted**; browser behaviour on the Dell **unverified** |
| **SEC-12** | Medium (by design) | Runtime database credential | Held by `coven_app`: reads every table including `django_session` (session ids) and may update `accounts_user` | Someone holding the runtime password effectively controls the application: read all data, take over the signed-in session, reset the owner's password hash. They **cannot** change the schema, remove triggers, rewrite or delete financial/audit history, or touch other roles (proved by `test_db_attacks.py`) | SEC-01 keeps the password out of plain text; append-only history preserves the evidence | **Accepted** — inherent to a runtime role that must log people in. See THREAT_MODEL scenario 4 |
| **SEC-13** | Low | Backups | One AES key; a plaintext database dump exists transiently in the temporary folder while a backup is built or a restore is verified | A copy of all data on disk for seconds to minutes; a leaked key opens every backup made with it | **Decision S-04: safe key rotation implemented** (`docs/KEY_ROTATION_AND_RECOVERY.md`): preflight/dry-run, typed confirmations, old key kept and proven, interruption-safe resume/abort, rollback (`--set-current`), guarded pruning, audit by fingerprint only, replacement-computer recovery rehearsed on disposable databases. Temporary folders are per-run and removed (tests); BitLocker mitigates the rest | **Fixed + tested** on the development machine; **not rehearsed on the Dell**; rotation protects new backups only (old backups stay readable with an exposed old key until retention removes them) |
| **SEC-14** | Informational | Tracked files | Three macOS `.DS_Store` files are tracked | Folder-listing metadata of the developer's machine | Untouched at the owner's instruction | **Accepted** |
| **SEC-15** | Informational | Browser history | Search and filter forms use GET, so names typed into search boxes appear in browser history | Local, owner's browser only | None | **Accepted** |
| **SEC-16** | Informational | New endpoint | `GET /healthz/` is public (loopback only) so the launcher can ask "is it up?" | Reveals that the application is running to a local program; answers only `{"app","status"}` and never an error text | By design; tested for content, methods and failure behaviour | **Fixed + tested** (introduced and verified in this pass) |

**Added by the final release review (all fixed and retested on the development machine):**

| ID | Severity | Finding | Fix |
|---|---|---|---|
| **SEC-17** | High | `rotate_backup_key --abort` could delete the pending key AFTER the verification backup had been made with it (`abort` looked only at the key slots), leaving the newest backup without its key in the store: start-up blocked (`KEY_MISMATCH`) and recovery only by paper | `abort` now inspects the real destination (every file's header), the history and any running backup, and refuses with "use --resume" if any backup uses the pending key, or if the destination cannot be fully and safely inspected; `import_backup_key` restores a lost key from paper (hidden, validated, must open a real backup) |
| **SEC-18** | High | `prune` judged "needed" from the database history only: after restoring an older database, a backup at the destination with no history row was ignored and its key could be removed | `prune` is fail-closed: complete listing (all Drive pages, incomplete/unknown/short listings refused), every header readable, no duplicates, no backup at the destination or in the history needing the key, destination and history must agree (unknown files and missing files both refuse), an empty destination with history refuses |
| **SEC-19** | Medium | The owner's setup needed elevation for the BitLocker read, so it could run as another administrator account and save the owner's secrets, settings and shortcuts in the wrong profile | Split: standalone administrator verification + non-elevated owner setup with account confirmation and a Credential Manager proof (SEC-08) |
| **SEC-20** | Low | `BackupBusy` surfaced as a traceback from the rotation; recovery keys could be given in plain files and the new key was echoed when typed back | Controlled message ("paused, use --resume"); `--old-recovery-key-file` removed, `--recovery-key-file` for restore guarded (never inside the application folder, refused if other users can read it, warned) ; the typed-back key is hidden |

No Critical or High finding was identified. That statement is limited to what was attacked above; it is not a promise that none exist.

## 3. Verified protections vs. things that only exist as settings

A setting is not accepted as a protection unless something exercised it:
- **Loopback only** — `serve_local` refuses other hosts (tested with four forms), `ALLOWED_HOSTS` check `coven.E010`, and the kernel's socket table is read in the lifecycle test: the listening socket is bound to `127.0.0.1` only (Linux; on the Dell `verify_runtime.ps1` does the same with `Get-NetTCPConnection`, unverified). The Phase 7 browser test also showed the machine's other address refusing connections.
- **Session expiry at day end** — the cookie `Max-Age` and server-side expiry are asserted, and an expired session is refused.
- **Throttle without lockout** — failures counted per username and per socket address; a forged forwarding header does not help; the account stays active.
- **Append-only history** — enforced twice (privileges and triggers) and attacked from the runtime role.

## 3b. Added after the owner's decisions (verified by tests, development machine)
- **S-01:** start-up is blocked for a missing, unreadable or invalid backup key, and when the key of the library's newest backup is not in the store (`KEY_MISMATCH`).
- **S-02 / fail closed:** a broken credential store stops production start-up, the launcher preflight (`STORE_UNAVAILABLE`), `store_secrets.py` (nothing saved), `setup_backup_key` (never creates a key while the existing one cannot be read) and `connect_drive`.
- **Never silent:** `save_working_key`, every key slot, `secretstore.put` and `connect_drive` refuse to overwrite; restore never overwrites a different working key (it imports as a retired key); mutation checks confirmed the tests fail when these guards are removed.
- **Key material never in the audit log or history rows** (searched after a full rotation).

## 4. Not tested, or cannot be tested here

Windows 11 itself (launcher, Task Scheduler, shortcuts, message boxes, Credential Manager, services, ACLs, BitLocker), real Google Drive and OAuth revocation behaviour, PostgreSQL authentication over the Dell's `pg_hba.conf`, the Dell's browser (Secure cookies on loopback), anti-virus interaction, physical security, the owner's e-mail account, denial of service beyond simple oversize/malformed requests, timing side channels, and any attack by a person with the owner's Windows account (nothing here defends against that). PowerShell scripts were written but **could not be executed or even parsed** in this environment.

## 5. Results of the checks (14 November 2026, development machine)

| Check | Result |
|---|---|
| Full test suite, run on an environment built **only** from `requirements/production.lock` (plus the dev tools) | 1,213 tests, all passed (it was 1,109 before this pass; it also caught and fixed one regression and one hang caused by the changes) |
| Ruff check and format | clean |
| `check --deploy` (production settings) | four expected warnings only: HSTS, SSL redirect, secure session cookie, secure CSRF cookie (plain `http://127.0.0.1`, SEC-11) |
| `check_db_security`, runtime role / owner role | 0 errors, 0 warnings each |
| Migration drift | none |
| `pip-audit` on `base.txt`, `dev.txt`, `production.lock` | no known vulnerabilities |
| `bandit` (medium and above, application and scripts) | clean after replacing `urlopen` with a loopback-only `http.client` probe |
| Secret scan (`detect-secrets`, history search, tracked-file names) | nothing real; matches are fixtures, the test key and the words "secret keyword" in scripts that never hold a value |
| Chromium against production settings under waitress | login page loads with CSP; an injected inline script is blocked; `/healthz/` answers; the server stops cleanly on request; the protected log file is created |

## 6. Live probe (production settings, waitress, loopback)

The script is kept in the audit method only as a description: start `serve_local` with `config.settings.production` and a collected static folder, then send the requests of the table in section 1 over raw sockets. Observed: no `Server` header, strict CSP, `Cache-Control: no-store` on the login page, 404 for every traversal attempt, 400 for foreign Host headers, 405/403 for odd methods, connection reset for an 11 MB body, no secrets in the log (only `WARNING` lines with paths).
