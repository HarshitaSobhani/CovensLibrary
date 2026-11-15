"""Extra attacks from the runtime role (the credentials an attacker would hold if they read the
Django process's database password). Complements test_runtime_privileges: server-side file and
program access, server settings, other sessions, lock-based denial, and what the role can read."""

import psycopg
from django.db import connection
from django.test import TransactionTestCase
from psycopg import errors

from core.tests.test_runtime_privileges import RUNTIME, runtime_connection

DENIED = (errors.InsufficientPrivilege, errors.FeatureNotSupported, errors.UndefinedFile)


class RuntimeRoleAttackTests(TransactionTestCase):
    def setUp(self):
        self.rt = runtime_connection()
        self.addCleanup(self.rt.close)

    def run_rt(self, sql, params=None):
        with self.rt.cursor() as cur:
            cur.execute(sql, params or [])
            return cur.fetchall() if cur.description else []

    def assertDenied(self, sql):
        with self.assertRaises(DENIED, msg=sql):
            self.run_rt(sql)

    def test_the_role_has_no_server_side_file_program_or_settings_access(self):
        for sql in (
            "SELECT pg_read_file('/etc/passwd')",
            "SELECT pg_read_binary_file('/etc/passwd')",
            "SELECT pg_ls_dir('/')",
            "SELECT pg_stat_file('/etc/passwd')",
            "SELECT lo_import('/etc/passwd')",
            "SELECT lo_export(1, '/tmp/zz_lo')",
            "COPY members_member FROM '/etc/passwd'",
            "COPY members_member TO PROGRAM 'id'",
            "ALTER SYSTEM SET log_statement = 'all'",
            "SELECT pg_reload_conf()",
            "SELECT pg_rotate_logfile()",
            "SELECT pg_switch_wal()",
            "CREATE PUBLICATION zz FOR ALL TABLES",
            "CREATE SUBSCRIPTION zz CONNECTION 'host=127.0.0.1' PUBLICATION p",
            "SET SESSION AUTHORIZATION postgres",
        ):
            self.assertDenied(sql)

    def test_password_hashes_and_server_internals_are_not_readable(self):
        for sql in (
            "SELECT rolpassword FROM pg_authid",
            "SELECT passwd FROM pg_shadow",
            "SELECT current_setting('data_directory')",
            "SELECT * FROM pg_hba_file_rules",
            "SELECT * FROM pg_file_settings",
        ):
            self.assertDenied(sql)

    def test_the_role_cannot_stop_other_sessions_or_hold_exclusive_locks(self):
        # Safety guard: a superuser CAN terminate backends, so running this as one would kill
        # sessions for real. Refuse to run the destructive probe in that configuration.
        if self.run_rt("SELECT rolsuper FROM pg_roles WHERE rolname = current_user")[0][0]:
            self.skipTest("The runtime role is a superuser (single-role dev); probe not run.")
        # The victim is a dedicated throwaway session, never the test's own Django connection.
        s = connection.settings_dict
        victim = psycopg.connect(
            host=s["HOST"],
            port=s["PORT"],
            dbname=s["NAME"],
            user=s["USER"],
            password=s["PASSWORD"],
            autocommit=True,
        )
        self.addCleanup(victim.close)
        pid = victim.info.backend_pid
        connection.ensure_connection()
        self.assertNotEqual(pid, connection.connection.info.backend_pid)
        self.assertNotEqual(pid, self.rt.info.backend_pid)
        for sql in (
            f"SELECT pg_terminate_backend({pid})",
            f"SELECT pg_cancel_backend({pid})",
        ):
            self.assertDenied(sql)
        self.assertEqual(victim.execute("SELECT 1").fetchone(), (1,))  # still alive
        # a table lock stronger than a read would block the whole application: refused
        with self.rt.cursor() as cur:
            cur.execute("BEGIN")
            for mode in ("ACCESS EXCLUSIVE", "EXCLUSIVE", "SHARE ROW EXCLUSIVE"):
                with self.assertRaises(errors.InsufficientPrivilege, msg=mode):
                    cur.execute(f"LOCK TABLE audit_auditevent IN {mode} MODE")
                cur.execute("ROLLBACK")
                cur.execute("BEGIN")
            cur.execute("ROLLBACK")

    def test_the_role_owns_nothing_and_public_holds_no_table_privileges(self):
        owned = self.run_rt(
            "SELECT count(*) FROM pg_class c JOIN pg_roles r ON r.oid = c.relowner "
            "WHERE r.rolname = %s AND c.relnamespace = 'public'::regnamespace",
            [RUNTIME],
        )[0][0]
        self.assertEqual(owned, 0)
        public_grants = self.run_rt(
            "SELECT count(*) FROM information_schema.role_table_grants "
            "WHERE grantee = 'PUBLIC' AND table_schema = 'public'"
        )[0][0]
        self.assertEqual(public_grants, 0)
        functions = self.run_rt(
            "SELECT count(*) FROM pg_proc p JOIN pg_roles r ON r.oid = p.proowner "
            "WHERE r.rolname = %s",
            [RUNTIME],
        )[0][0]
        self.assertEqual(functions, 0)

    def test_the_role_attributes_are_minimal(self):
        row = self.run_rt(
            "SELECT rolsuper, rolcreaterole, rolcreatedb, rolreplication, rolbypassrls, "
            "rolcanlogin FROM pg_roles WHERE rolname = %s",
            [RUNTIME],
        )[0]
        self.assertEqual(row, (False, False, False, False, False, True))
        members_of = self.run_rt(
            "SELECT count(*) FROM pg_auth_members m JOIN pg_roles r ON r.oid = m.member "
            "WHERE r.rolname = %s",
            [RUNTIME],
        )[0][0]
        self.assertEqual(members_of, 0)  # a member of no other role (no pg_* privileges either)

    def test_financial_history_survives_every_write_attack_the_role_can_try(self):
        """Threat 12: even holding the runtime password, history cannot be rewritten or erased."""
        owner = connection.cursor()
        owner.execute(
            "INSERT INTO audit_auditevent (action, entity_type, entity_id, summary, occurred_at) "
            "VALUES ('attack-probe','','', '{}', now())"
        )
        for sql in (
            "UPDATE audit_auditevent SET action = 'forged'",
            "DELETE FROM audit_auditevent",
            "TRUNCATE audit_auditevent",
            "UPDATE finance_financialtransaction SET amount_paise = 1",
            "DELETE FROM finance_financialtransaction",
            "UPDATE finance_paymententry SET amount_paise = 1",
            "DELETE FROM finance_tokenledgerentry",
            "UPDATE finance_receiptdocument SET snapshot = '{}'",
            "DELETE FROM backups_backuprun",
            "UPDATE backups_restorerun SET status = 'succeeded'",
            "DELETE FROM circulation_loan",
        ):
            with self.assertRaises(DENIED + (errors.RaiseException,), msg=sql):
                self.run_rt(sql)
        self.assertEqual(
            self.run_rt("SELECT count(*) FROM audit_auditevent WHERE action = 'attack-probe'")[0][
                0
            ],
            1,
        )
