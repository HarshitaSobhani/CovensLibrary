-- Coven's Library - PostgreSQL role and database provisioning (idempotent).
--
-- Run as the SUPERUSER (postgres), connected to the maintenance database "postgres":
--   psql -U postgres -d postgres -v ON_ERROR_STOP=1 \
--        -v database=coven_library -v owner_createdb=off -f provision_roles.sql
-- Afterwards set each password in an INTERACTIVE psql session as postgres (never on a command line):
--   \password coven_owner
--   \password coven_app
--
-- Variables (all optional):
--   database        application database name (default coven_library)
--   owner_createdb  'on' only on DEVELOPMENT machines (the test suite creates a test database)
-- This script never sets or reads passwords: command-line variables would expose them in the
-- process list, shell history and possibly the server log. \password hashes client-side.
--
-- Roles:  coven_owner  owns the database and every object; runs migrations/grants/restores.
--         coven_app    runtime role: data access only (privileges come from `apply_db_grants`).
-- Re-running is safe: roles are normalised, nothing is dropped, existing data is untouched.

\set ON_ERROR_STOP on
\if :{?database}
\else
  \set database coven_library
\endif
\if :{?owner_createdb}
\else
  \set owner_createdb off
\endif

SELECT 'CREATE ROLE coven_owner LOGIN' WHERE NOT EXISTS
  (SELECT 1 FROM pg_roles WHERE rolname = 'coven_owner') \gexec
SELECT 'CREATE ROLE coven_app LOGIN' WHERE NOT EXISTS
  (SELECT 1 FROM pg_roles WHERE rolname = 'coven_app') \gexec

SELECT format('ALTER ROLE coven_owner LOGIN NOSUPERUSER NOCREATEROLE NOREPLICATION NOBYPASSRLS %s',
              CASE WHEN :'owner_createdb' = 'on' THEN 'CREATEDB' ELSE 'NOCREATEDB' END) \gexec
ALTER ROLE coven_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;

SELECT format('CREATE DATABASE %I OWNER coven_owner', :'database') WHERE NOT EXISTS
  (SELECT 1 FROM pg_database WHERE datname = :'database') \gexec

SELECT (pg_get_userbyid(datdba) = 'coven_owner') AS owned_by_owner
  FROM pg_database WHERE datname = :'database' \gset
\if :owned_by_owner
  \echo 'Database is owned by coven_owner. Next: run migrations as the owner (apply_db_grants runs automatically).'
\else
  \echo 'NOTICE: database is NOT owned by coven_owner (legacy single-role install).'
  \echo 'Back it up, then run convert_existing_database.sql while connected to that database.'
\endif
