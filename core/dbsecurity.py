"""Database security model: table classification, least-privilege grants and verification.

Two PostgreSQL roles (see docs/DATABASE_SECURITY.md):

* ``coven_owner`` owns the database and every object in it. Migrations, grants, extension
  management and restores run as this role. It is never used by the running application.
* ``coven_app`` is the runtime role. It only receives the data privileges listed here, per
  table class. It does not own anything, cannot create objects, cannot alter or drop triggers or
  trigger functions, cannot TRUNCATE, and cannot change privileges.

Every table must be classified in ``TABLE_POLICIES``. Nothing is granted automatically: an
unclassified table gets no privileges for the runtime role and ``check_db_security`` reports it as
an error (fail closed). ``ALTER DEFAULT PRIVILEGES`` is used only to REVOKE defaults (nothing is
granted automatically); the grants are reapplied explicitly (and idempotently) after every
migration.
"""

import re
from typing import NamedTuple

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from psycopg import sql

MUTABLE = "mutable"
NO_DELETE = "no_delete"
APPEND_ONLY = "append_only"
READ_ONLY = "read_only"

ALL_TABLE_PRIVILEGES = ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER")

# What the RUNTIME role may do per class. TRUNCATE / REFERENCES / TRIGGER are never granted.
POLICY_PRIVILEGES = {
    MUTABLE: ("SELECT", "INSERT", "UPDATE", "DELETE"),
    NO_DELETE: ("SELECT", "INSERT", "UPDATE"),
    APPEND_ONLY: ("SELECT", "INSERT"),
    READ_ONLY: ("SELECT",),
}

# Every table in the public schema. Add new tables here IN THE SAME CHANGE that creates them.
TABLE_POLICIES = {
    # --- accounts
    "accounts_user": NO_DELETE,  # the single owner account is never deleted
    "accounts_loginevent": MUTABLE,  # old events may be pruned
    "accounts_recoverycode": MUTABLE,  # used_at / expires_at are updated
    "accounts_user_groups": READ_ONLY,
    "accounts_user_user_permissions": READ_ONLY,
    # --- audit (history that must never change)
    "audit_auditevent": APPEND_ONLY,
    # --- core
    "core_idsequence": MUTABLE,
    "core_librarysetting": NO_DELETE,  # the singleton settings row
    # --- members (history: no hard deletion)
    "members_member": NO_DELETE,
    # --- catalogue
    "catalogue_category": MUTABLE,
    "catalogue_bookedition": NO_DELETE,
    "catalogue_bookcopy": NO_DELETE,
    "catalogue_acquisition": MUTABLE,
    "catalogue_copyevent": APPEND_ONLY,  # copy history
    "catalogue_labellayout": MUTABLE,
    "catalogue_importbatch": MUTABLE,
    "catalogue_importrow": MUTABLE,
    # --- Django framework tables (written only by migrations, i.e. by the owner)
    "django_migrations": READ_ONLY,
    "django_content_type": READ_ONLY,
    "auth_permission": READ_ONLY,
    "auth_group": READ_ONLY,
    "auth_group_permissions": READ_ONLY,
    "django_session": MUTABLE,
}

# Protections that must exist and be enabled: table -> trigger names.
REQUIRED_TRIGGERS = {
    "audit_auditevent": ("audit_event_append_only",),
    "catalogue_copyevent": ("catalogue_copyevent_append_only",),
    "members_member": ("members_member_no_immutable",),
}

ROLE_NAME_RE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


class Finding(NamedTuple):
    level: str  # "error" | "warning"
    code: str
    message: str


def validate_role_name(name):
    if not isinstance(name, str) or not ROLE_NAME_RE.match(name):
        raise ImproperlyConfigured("Database role names must match [a-z_][a-z0-9_]* (max 63).")
    return name


def _raw_cursor(connection):
    connection.ensure_connection()
    return connection.connection.cursor()  # psycopg cursor (accepts psycopg.sql objects)


def _public_tables(cur):
    cur.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY 1")
    return [r[0] for r in cur.fetchall()]


def _sequences_of(cur, table):
    cur.execute(
        """
        SELECT DISTINCT pg_get_serial_sequence(quote_ident(%s), a.attname)
        FROM pg_attribute a
        WHERE a.attrelid = ('public.' || quote_ident(%s))::regclass
          AND a.attnum > 0 AND NOT a.attisdropped
          AND pg_get_serial_sequence(quote_ident(%s), a.attname) IS NOT NULL
        """,
        [table, table, table],
    )
    return [
        r[0] for r in cur.fetchall()
    ]  # schema-qualified names, e.g. public.members_member_id_seq


def _seq_ident(qualified):
    schema, _, name = qualified.partition(".")
    return sql.Identifier(schema, name)


def plan_grants(connection, runtime_role):
    """The ordered, idempotent SQL that makes the runtime role's privileges exactly the policy."""
    validate_role_name(runtime_role)
    role = sql.Identifier(runtime_role)
    with _raw_cursor(connection) as cur:
        cur.execute("SELECT current_database(), current_user")
        database, owner = cur.fetchone()
        tables = _public_tables(cur)
        statements = [
            sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(sql.Identifier(database)),
            sql.SQL("REVOKE ALL ON DATABASE {} FROM {}").format(sql.Identifier(database), role),
            sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(sql.Identifier(database), role),
            sql.SQL("REVOKE ALL ON SCHEMA public FROM PUBLIC"),
            sql.SQL("REVOKE ALL ON SCHEMA public FROM {}").format(role),
            sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(role),
            # Fail closed: never let future objects inherit privileges automatically.
            sql.SQL(
                "ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA public REVOKE ALL ON TABLES FROM {}"
            ).format(sql.Identifier(owner), role),
            sql.SQL(
                "ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA public "
                "REVOKE ALL ON SEQUENCES FROM {}"
            ).format(sql.Identifier(owner), role),
            sql.SQL("REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {}").format(role),
            sql.SQL("REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM {}").format(role),
        ]
        for table in tables:
            policy = TABLE_POLICIES.get(table)
            if policy is None:
                continue  # unclassified: no privileges (fail closed)
            privileges = sql.SQL(", ").join(sql.SQL(p) for p in POLICY_PRIVILEGES[policy])
            statements.append(
                sql.SQL("GRANT {} ON TABLE {} TO {}").format(
                    privileges, sql.Identifier("public", table), role
                )
            )
            for sequence in _sequences_of(cur, table):
                # nextval() (USAGE) only where the runtime role may insert; SELECT everywhere so
                # that pg_dump (backups run as the runtime role) can read every sequence.
                privileges = "SELECT" if policy == READ_ONLY else "USAGE, SELECT"
                statements.append(
                    sql.SQL("GRANT {} ON SEQUENCE {} TO {}").format(
                        sql.SQL(privileges), _seq_ident(sequence), role
                    )
                )
        return statements


def render(connection, statements):
    with _raw_cursor(connection) as cur:
        return [s.as_string(cur) for s in statements]


def apply_grants(connection, runtime_role):
    """Apply the plan atomically. Must run as the owner role (not as the runtime role)."""
    validate_role_name(runtime_role)
    with _raw_cursor(connection) as cur:
        cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", [runtime_role])
        if cur.fetchone() is None:
            raise ImproperlyConfigured(
                f"The runtime role '{runtime_role}' does not exist. Provision the roles first "
                "(deploy/postgres/provision_roles.sql, docs/DATABASE_SECURITY.md)."
            )
        cur.execute(
            "SELECT current_user = pg_get_userbyid(datdba) FROM pg_database "
            "WHERE datname = current_database()"
        )
        if not cur.fetchone()[0]:
            raise ImproperlyConfigured(
                "Grants must be applied by the database owner role (use "
                "--settings=config.settings.migrate)."
            )
        cur.execute("SELECT current_user = %s", [runtime_role])
        if cur.fetchone()[0]:
            raise ImproperlyConfigured(
                "Grants cannot be applied while connected as the runtime role."
            )
    statements = plan_grants(connection, runtime_role)
    with _raw_cursor(connection) as cur:
        for statement in statements:
            cur.execute(statement)
    return statements


# ----------------------------------------------------------------------------------------------
def inspect(connection, runtime_role, kind):
    """Compare the real database with the policy. ``kind`` is 'runtime' or 'owner' (who we are)."""
    validate_role_name(runtime_role)
    findings = []

    def err(code, message):
        findings.append(Finding("error", code, message))

    def warn(code, message):
        findings.append(Finding("warning", code, message))

    strict = settings.DB_SECURITY_MODE == "strict"
    with _raw_cursor(connection) as cur:
        cur.execute(
            "SELECT current_user, pg_get_userbyid(datdba), current_database() "
            "FROM pg_database WHERE datname = current_database()"
        )
        me, owner, database = cur.fetchone()
        cur.execute(
            "SELECT rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls "
            "FROM pg_roles WHERE rolname = %s",
            [runtime_role],
        )
        attrs = cur.fetchone()
        if attrs is None:
            err("coven.E010", f"Runtime role '{runtime_role}' does not exist.")
            return findings

        if runtime_role == owner:
            if strict:
                err("coven.E005", "The runtime role owns the database (single-role setup).")
            else:
                warn(
                    "coven.W002", "Single-role development mode: database protections are reduced."
                )
            return findings
        if kind == "runtime" and me != runtime_role:
            err("coven.E006", f"Connected as '{me}', expected the runtime role.")
        if kind == "owner" and me != owner:
            err("coven.E007", "Connected role is not the database owner role.")
        if kind == "runtime" and me == owner:
            err("coven.E005", "The application is connected as the owner role.")

        names = ("superuser", "createdb", "createrole", "replication", "bypassrls")
        for flag, value in zip(names, attrs, strict=True):
            if value:
                err("coven.E004", f"Runtime role has the {flag} attribute.")
        cur.execute("SELECT pg_has_role(%s, %s, 'MEMBER')", [runtime_role, owner])
        if cur.fetchone()[0]:
            err("coven.E004", "Runtime role is a member of the owner role.")
        cur.execute("SELECT has_schema_privilege(%s, 'public', 'CREATE')", [runtime_role])
        if cur.fetchone()[0]:
            err("coven.E004", "Runtime role can create objects in schema public.")
        for privilege in ("CREATE", "TEMPORARY"):
            cur.execute(
                "SELECT has_database_privilege(%s, %s, %s)", [runtime_role, database, privilege]
            )
            if cur.fetchone()[0]:
                err("coven.E004", f"Runtime role has {privilege} on the database.")
        cur.execute(
            "SELECT count(*) FROM pg_default_acl d, LATERAL aclexplode(d.defaclacl) a "
            "WHERE a.grantee = (SELECT oid FROM pg_roles WHERE rolname = %s)",
            [runtime_role],
        )
        if cur.fetchone()[0]:
            err("coven.E002", "Default privileges for the runtime role exist (must be none).")
        cur.execute(
            "SELECT count(*) FROM pg_extension e WHERE pg_get_userbyid(e.extowner) = %s",
            [runtime_role],
        )
        if cur.fetchone()[0]:
            err("coven.E004", "The runtime role owns a database extension.")

        tables = _public_tables(cur)
        cur.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public' AND tableowner = %s",
            [runtime_role],
        )
        for (owned,) in cur.fetchall():
            err("coven.E004", f"Runtime role owns table {owned}.")
        cur.execute(
            "SELECT count(*) FROM pg_tables WHERE schemaname = 'public' AND tableowner <> %s",
            [owner],
        )
        if cur.fetchone()[0]:
            err("coven.E004", "Some tables are not owned by the database owner role.")

        for table in tables:
            policy = TABLE_POLICIES.get(table)
            actual = set()
            for privilege in ALL_TABLE_PRIVILEGES:
                cur.execute(
                    "SELECT has_table_privilege(%s, ('public.' || quote_ident(%s))::regclass, %s)",
                    [runtime_role, table, privilege],
                )
                if cur.fetchone()[0]:
                    actual.add(privilege)
            if policy is None:
                err("coven.E001", f"Table {table} is not classified in TABLE_POLICIES.")
                if actual:
                    err(
                        "coven.E002",
                        f"Unclassified table {table} grants the runtime role {sorted(actual)}.",
                    )
                continue
            expected = set(POLICY_PRIVILEGES[policy])
            if actual - expected:
                err(
                    "coven.E002",
                    f"{table} ({policy}): runtime role has excess privileges "
                    f"{sorted(actual - expected)}.",
                )
            if expected - actual:
                err(
                    "coven.E003",
                    f"{table} ({policy}): runtime role is missing privileges "
                    f"{sorted(expected - actual)}.",
                )

        for table, trigger_names in REQUIRED_TRIGGERS.items():
            for trigger in trigger_names:
                cur.execute(
                    "SELECT t.tgenabled, pg_get_userbyid(p.proowner), p.proconfig "
                    "FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
                    "JOIN pg_proc p ON p.oid = t.tgfoid "
                    "WHERE c.relname = %s AND t.tgname = %s AND NOT t.tgisinternal",
                    [table, trigger],
                )
                row = cur.fetchone()
                if row is None:
                    err("coven.E008", f"Required trigger {trigger} on {table} is missing.")
                    continue
                enabled, function_owner, config = row
                if enabled not in ("O", "A"):
                    err("coven.E008", f"Trigger {trigger} on {table} is disabled.")
                if function_owner != owner:
                    err(
                        "coven.E008",
                        f"Trigger function of {trigger} is not owned by the owner role.",
                    )
                if not config or not any(c.startswith("search_path=") for c in config):
                    warn("coven.W003", f"Trigger function of {trigger} has no fixed search_path.")

        expected_sequences = {}
        for table in tables:
            policy = TABLE_POLICIES.get(table)
            for qualified in _sequences_of(cur, table):
                expected_sequences[qualified.partition(".")[2]] = policy
        cur.execute(
            "SELECT s.relname FROM pg_class s JOIN pg_namespace n ON n.oid = s.relnamespace "
            "WHERE s.relkind = 'S' AND n.nspname = 'public'"
        )
        for (sequence,) in cur.fetchall():
            actual = set()
            for privilege in ("SELECT", "USAGE", "UPDATE"):
                cur.execute(
                    "SELECT has_sequence_privilege("
                    "%s, ('public.' || quote_ident(%s))::regclass, %s)",
                    [runtime_role, sequence, privilege],
                )
                if cur.fetchone()[0]:
                    actual.add(privilege)
            policy = expected_sequences.get(sequence)
            if policy is None:
                expected = set()
            else:
                expected = {"SELECT"} if policy == READ_ONLY else {"SELECT", "USAGE"}
            if actual - expected:
                err(
                    "coven.E002",
                    f"Runtime role has excess privileges {sorted(actual - expected)} "
                    f"on sequence {sequence}.",
                )
            if expected - actual:
                err(
                    "coven.E003",
                    f"Runtime role is missing privileges {sorted(expected - actual)} "
                    f"on sequence {sequence}.",
                )
    return findings


def summarise(findings):
    return "; ".join(f"{f.code} {f.message}" for f in findings if f.level == "error")


def assert_runtime_security():
    """Refuse to start a production-like process on an insecure database (shows no secrets)."""
    from django.db import connection
    from django.db.utils import OperationalError

    if settings.DB_ROLE != "runtime" or settings.DB_SECURITY_MODE != "strict":
        return
    try:
        findings = inspect(connection, settings.DB_RUNTIME_ROLE, "runtime")
    except OperationalError:
        raise ImproperlyConfigured(
            "The database is unreachable; cannot verify its security."
        ) from None
    errors = [f for f in findings if f.level == "error"]
    if errors:
        raise ImproperlyConfigured("Database security check failed: " + summarise(findings))
