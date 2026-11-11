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
cp .env.example .env          # edit: set DJANGO_SECRET_KEY and DB_PASSWORD (never commit .env)
```
Create the database and an application role (role must be able to create the test database):
```sql
CREATE ROLE coven_app LOGIN PASSWORD '<choose-a-password>' CREATEDB;
CREATE DATABASE coven_library OWNER coven_app;
```
The `btree_gist` extension (needed from Phase 3) is a *trusted* extension in PostgreSQL 13+, so the database owner can create it: `CREATE EXTENSION IF NOT EXISTS btree_gist;` — verified in `tests/test_integration.py`.

## Daily commands
```bash
source .venv/bin/activate
python manage.py migrate
python manage.py createsuperuser              # the one owner account
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

## Notes
- Settings: `config.settings.local` (development, loads `.env`), `config.settings.production` (real environment variables only), `config.settings.test`.
- Email defaults to the console backend; set `EMAIL_BACKEND`/`EMAIL_HOST*` for SMTP. Credentials only through the environment.
- Never run the app with `DEBUG` on outside development. Django admin is intentionally not installed.
