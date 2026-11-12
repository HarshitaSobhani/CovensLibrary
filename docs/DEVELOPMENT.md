# Development setup — macOS (MacBook M3)

Development environment only. The Dell (Windows 11) runtime setup is written separately in `docs/DEPLOYMENT.md` (Phase 7c).

## Prerequisites
- Python 3.12
- PostgreSQL 16 (same major version as the Dell) listening on localhost — Homebrew or Postgres.app
- `git`

## One-time setup
```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements/dev.txt
cp .env.example .env          # edit: SECRET KEY, DB_PASSWORD, and DJANGO_DEBUG=true (never commit .env)
```
Create the two roles and database (see `docs/DATABASE_SECURITY.md`):
```bash
psql -U postgres -d postgres -v ON_ERROR_STOP=1 -v database=coven_library -v owner_createdb=on \
  -f deploy/postgres/provision_roles.sql
```
Set both passwords afterwards in an interactive psql session (`\password coven_owner`, `\password coven_app`); never put passwords on a command line. `.env` holds runtime credentials (`coven_app`) only. `.env.owner` (copy `.env.owner.example`, mode 0600, git-ignored) holds `DB_OWNER_USER`/`DB_OWNER_PASSWORD`; only migrate and test settings read it. Protect it: plaintext, never commit, never copy to backups or synced folders, never put it in the runtime environment. `coven_owner` has CREATEDB on this machine so the suite can create its test database. `btree_gist` is created by the owner role, never the runtime role.

## Daily commands
```bash
source .venv/bin/activate
python manage.py migrate --settings=config.settings.migrate   # as coven_owner; reapplies grants
python manage.py check_db_security                            # as coven_app
python manage.py create_owner --username <name> --email <address>   # the one owner account
python manage.py runserver 127.0.0.1:8000     # localhost only
python manage.py test --settings=config.settings.test
python manage.py makemigrations --check --dry-run
ruff check . && ruff format --check .
```

## Password recovery (local)
```bash
python manage.py issue_recovery_code --username <owner-username>
```
Prints a single-use code (hashed in the database, shown once, valid 15 minutes); enter it at `/accounts/recover/`.

## Startup procedure (development)
1. `source .venv/bin/activate`
2. In `.env` set `DJANGO_DEBUG=true`: with `DEBUG` off, `runserver` does not serve the CSS/JS (production static-file serving is added with the Windows runtime setup in Phase 7c).
3. `python manage.py migrate --settings=config.settings.migrate`
4. `python manage.py create_owner --username <name> --email <address>` — the one and only account (the database refuses a second one; do not use `createsuperuser`).
5. `python manage.py runserver 127.0.0.1:8000`
6. Optional housekeeping: `python manage.py clearsessions` removes expired sessions.

## Settings selection
- Development: leave `DJANGO_SETTINGS_MODULE` unset; `manage.py` then uses `config.settings.local`, which loads `.env` (needs the dev-only `python-dotenv`).
- Runtime (Dell, Windows 11): set `DJANGO_SETTINGS_MODULE=config.settings.production` and real environment variables; `python-dotenv` is not installed there. If neither is available, `manage.py` stops with an explanatory message instead of guessing.
- Tests: `--settings=config.settings.test` (loads `.env`; needs a role that can create the test database).

## Notes
- Settings: `config.settings.local` (development, loads `.env`), `config.settings.production` (real environment variables only), `config.settings.test`.
- Email defaults to the console backend; set `EMAIL_BACKEND`/`EMAIL_HOST*` for SMTP. Credentials only through the environment.
- Never run the app with `DEBUG` on outside development. Django admin is intentionally not installed.
