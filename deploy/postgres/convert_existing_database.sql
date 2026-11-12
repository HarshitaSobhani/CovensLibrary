-- Coven's Library - convert an EXISTING single-role database to the two-role model.
--
-- Run as the SUPERUSER (postgres), connected to the APPLICATION database, AFTER:
--   1. a verified backup (pg_dump -Fc ... and pg_restore --list),
--   2. provision_roles.sql (creates coven_owner / normalises coven_app).
--   psql -U postgres -d coven_library -v ON_ERROR_STOP=1 -f convert_existing_database.sql
--
-- Effect: every object currently owned by coven_app (database, tables, sequences, trigger
-- functions, extensions) becomes owned by coven_owner. No row is read, changed or deleted.
-- Afterwards run:  python manage.py migrate --settings=config.settings.migrate
-- (applies pending migrations and the least-privilege grants).
-- Roll back (ONLY immediately after conversion, before coven_owner has created any other
-- objects, because it moves EVERYTHING coven_owner owns):
--   REASSIGN OWNED BY coven_owner TO coven_app;   (as superuser, in this database)

\set ON_ERROR_STOP on
SELECT current_database() AS db \gset
\echo 'Target database:' :db

BEGIN;
DO $$
BEGIN
  IF current_database() <> 'coven_library' THEN
    RAISE EXCEPTION 'refusing to run: current database is %, expected exactly coven_library',
      current_database();
  END IF;
END $$;
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'coven_owner') THEN
    RAISE EXCEPTION 'coven_owner does not exist: run provision_roles.sql first';
  END IF;
  IF EXISTS (SELECT 1 FROM pg_database
             WHERE pg_get_userbyid(datdba) = 'coven_app' AND datname <> current_database()) THEN
    RAISE EXCEPTION 'coven_app owns another database; REASSIGN OWNED would move it too';
  END IF;
END $$;

REASSIGN OWNED BY coven_app TO coven_owner;

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_tables WHERE schemaname = 'public' AND tableowner <> 'coven_owner') THEN
    RAISE EXCEPTION 'conversion incomplete: some tables are not owned by coven_owner';
  END IF;
  IF (SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname = current_database())
     <> 'coven_owner' THEN
    RAISE EXCEPTION 'conversion incomplete: database is not owned by coven_owner';
  END IF;
END $$;
COMMIT;
\echo 'Converted. Now run: python manage.py migrate --settings=config.settings.migrate'
