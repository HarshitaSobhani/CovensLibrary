"""Owner-role migration rollback/forward and backup/restore onto a disposable database.

Role used: the OWNER role (test settings), plus the runtime role for the restored-database checks.
"""

import os
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from unittest import skipUnless

import psycopg
from django.conf import settings
from django.core.management import call_command
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase

from core import dbsecurity as dbs

RUNTIME = settings.DB_RUNTIME_ROLE


def one(sql, params=None):
    with connection.cursor() as cur:
        cur.execute(sql, params or [])
        return cur.fetchone()[0]


def runtime_has(table, privilege):
    return one(
        "SELECT has_table_privilege(%s, ('public.'||quote_ident(%s))::regclass, %s)",
        [RUNTIME, table, privilege],
    )


class MigrationLifecycleTests(TransactionTestCase):
    def tearDown(self):
        call_command("migrate", verbosity=0)  # always leave the test database at head, secured
        super().tearDown()

    def test_owner_can_roll_back_and_forward_and_grants_are_reapplied(self):
        call_command("migrate", "core", "0001", verbosity=0)  # unapplies the search_path hardening
        self.assertIsNone(
            one("SELECT proconfig FROM pg_proc WHERE proname='audit_event_reject_change'")
        )
        call_command(
            "migrate", "audit", "zero", verbosity=0
        )  # drops audit table, trigger, function
        self.assertEqual(
            one("SELECT count(*) FROM pg_trigger WHERE tgname='audit_event_append_only'"), 0
        )
        call_command("migrate", "catalogue", "zero", verbosity=0)
        call_command("migrate", "members", "zero", verbosity=0)
        self.assertEqual(one("SELECT count(*) FROM pg_trigger WHERE NOT tgisinternal"), 0)

        call_command("migrate", verbosity=0)  # forward, as owner: post_migrate re-applies grants
        expected = sum(len(names) for names in dbs.REQUIRED_TRIGGERS.values())
        self.assertEqual(one("SELECT count(*) FROM pg_trigger WHERE NOT tgisinternal"), expected)
        self.assertEqual(dbs.inspect(connection, RUNTIME, "owner"), [])
        self.assertTrue(runtime_has("audit_auditevent", "INSERT"))
        self.assertFalse(runtime_has("audit_auditevent", "UPDATE"))
        self.assertFalse(runtime_has("members_member", "DELETE"))
        self.assertTrue(
            one(
                "SELECT proconfig IS NOT NULL FROM pg_proc "
                "WHERE proname='audit_event_reject_change'"
            )
        )

    def test_recreated_tables_fail_closed_until_grants_are_applied(self):
        call_command("migrate", "audit", "zero", verbosity=0)
        call_command("migrate", "catalogue", "zero", verbosity=0)
        executor = MigrationExecutor(connection)  # forward WITHOUT post_migrate / grants
        executor.migrate(executor.loader.graph.leaf_nodes())
        self.assertFalse(runtime_has("audit_auditevent", "SELECT"), "no automatic privileges")
        self.assertFalse(runtime_has("catalogue_copyevent", "INSERT"))
        findings = dbs.inspect(connection, RUNTIME, "owner")
        self.assertIn("coven.E003", {f.code for f in findings})
        dbs.apply_grants(connection, RUNTIME)
        self.assertEqual(dbs.inspect(connection, RUNTIME, "owner"), [])

    def test_flush_as_owner_keeps_the_database_secured(self):
        call_command("flush", verbosity=0, interactive=False)  # TRUNCATE as owner + post_migrate
        self.assertEqual(dbs.inspect(connection, RUNTIME, "owner"), [])


class _Adapter:
    """Minimal stand-in for a Django connection (dbsecurity only needs these two members)."""

    def __init__(self, conn):
        self.connection = conn

    def ensure_connection(self):
        return None


@skipUnless(
    shutil.which("pg_dump") and shutil.which("pg_restore"), "pg_dump / pg_restore are not on PATH"
)
class BackupRestoreTests(TransactionTestCase):
    """Back up as the runtime role; restore as the owner role into a disposable database."""

    def setUp(self):
        s = connection.settings_dict
        self.host, self.port = str(s["HOST"]), str(s["PORT"])
        self.owner, self.owner_pw = s["USER"], s["PASSWORD"]
        self.runtime_pw = os.environ["DB_PASSWORD"]
        self.source_db = s["NAME"]
        self.tmp = tempfile.mkdtemp(prefix="coven-restore-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        with connection.cursor() as cur:
            cur.execute(
                "INSERT INTO members_member (membership_no, full_name, mobile_e164, email, "
                "joined_on, notes, status, needs_new_subscription, created_at, updated_at) "
                "VALUES ('COV-M-800001','B','+910000000099','',current_date,'','active',false,"
                "now(),now())"
            )
            cur.execute(
                "INSERT INTO audit_auditevent (action, entity_type, entity_id, summary, "
                "occurred_at) VALUES ('backup-test','','','{}',now())"
            )

    def pg(self, tool, *args, user, password):
        env = {**os.environ, "PGPASSWORD": password}
        done = subprocess.run(
            [tool, "-h", self.host, "-p", self.port, "-U", user, *args],
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(done.returncode, 0, f"{tool} failed: {done.stderr[-500:]}")

    def connect(self, db, user, password):
        return psycopg.connect(
            host=self.host, port=self.port, dbname=db, user=user, password=password, autocommit=True
        )

    def restore_and_verify(self, extra_restore_args):
        dump = str(Path(self.tmp) / "backup.dump")
        # 1. backup with the RUNTIME credentials only (no owner credentials needed for backups)
        self.pg(
            "pg_dump", "-Fc", "-f", dump, self.source_db, user=RUNTIME, password=self.runtime_pw
        )
        self.assertGreater(Path(dump).stat().st_size, 1000)
        # 2. restore as the OWNER into a disposable database (roles already exist)
        target = f"restore_check_{uuid.uuid4().hex[:10]}"
        admin = self.connect("postgres", self.owner, self.owner_pw)
        admin.execute(f'CREATE DATABASE "{target}" OWNER "{self.owner}"')
        try:
            self.pg(
                "pg_restore",
                "--no-owner",
                *extra_restore_args,
                "-d",
                target,
                dump,
                user=self.owner,
                password=self.owner_pw,
            )
            owner_conn = self.connect(target, self.owner, self.owner_pw)
            try:
                counts = owner_conn.execute(
                    "SELECT (SELECT count(*) FROM members_member), "
                    "(SELECT count(*) FROM audit_auditevent)"
                ).fetchone()
                self.assertEqual(counts, (1, 1))
                triggers = {
                    r[0]
                    for r in owner_conn.execute(
                        "SELECT tgname FROM pg_trigger WHERE NOT tgisinternal"
                    ).fetchall()
                }
                self.assertEqual(
                    triggers, {name for names in dbs.REQUIRED_TRIGGERS.values() for name in names}
                )
                # 3. re-apply the grants as the owner and verify against the policy
                dbs.apply_grants(_Adapter(owner_conn), RUNTIME)
                self.assertEqual(dbs.inspect(_Adapter(owner_conn), RUNTIME, "owner"), [])
            finally:
                owner_conn.close()
            # 4. the runtime role works on the restored database, within its limits
            rt = self.connect(target, RUNTIME, self.runtime_pw)
            try:
                self.assertEqual(rt.execute("SELECT count(*) FROM members_member").fetchone()[0], 1)
                rt.execute(
                    "INSERT INTO audit_auditevent (action, entity_type, entity_id, summary, "
                    "occurred_at) VALUES ('after-restore','','','{}',now())"
                )
                with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                    rt.execute("UPDATE audit_auditevent SET action='x'")
                with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                    rt.execute("DELETE FROM members_member")
                self.assertEqual(dbs.inspect(_Adapter(rt), RUNTIME, "runtime"), [])
            finally:
                rt.close()
        finally:
            # The runtime session closed just above may still be shutting down on the server;
            # FORCE cannot terminate another role's session, so wait for it to disappear.
            for _ in range(50):
                try:
                    admin.execute(f'DROP DATABASE IF EXISTS "{target}" WITH (FORCE)')
                    break
                except (psycopg.errors.InsufficientPrivilege, psycopg.errors.ObjectInUse):
                    time.sleep(0.2)
            else:  # pragma: no cover
                admin.execute(f'DROP DATABASE IF EXISTS "{target}"')
            admin.close()

    def test_restore_with_privileges_in_the_dump(self):
        self.restore_and_verify([])

    def test_restore_without_privileges_then_grants_reapplied(self):
        """--no-acl simulates a restore where no privileges came along: nothing is granted
        automatically (fail closed) until apply_grants runs."""
        self.restore_and_verify(["--no-acl"])
