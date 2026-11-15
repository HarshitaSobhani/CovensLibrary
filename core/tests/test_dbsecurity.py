"""Table classification, grant planning and verification (runs as the OWNER role)."""

from io import StringIO
from unittest import mock

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.db.utils import OperationalError
from django.test import SimpleTestCase, TestCase, override_settings

from core import dbsecurity as dbs
from core.apps import apply_grants_after_migrate
from core.checks import database_security
from core.tests.secret_checks import checked_secret

RUNTIME = settings.DB_RUNTIME_ROLE


def run(sql, params=None):
    with connection.cursor() as cur:
        cur.execute(sql, params or [])
        return cur.fetchall() if cur.description else None


def inspect_owner():
    return dbs.inspect(connection, RUNTIME, "owner")


def codes(findings, level="error"):
    return sorted({f.code for f in findings if f.level == level})


class RegistryTests(TestCase):
    def test_every_table_is_classified_and_nothing_stale_is_listed(self):
        tables = {r[0] for r in run("SELECT tablename FROM pg_tables WHERE schemaname='public'")}
        self.assertEqual(tables - set(dbs.TABLE_POLICIES), set(), "unclassified tables")
        self.assertEqual(
            set(dbs.TABLE_POLICIES) - tables, set(), "classified tables that do not exist"
        )

    def test_only_known_classes_and_expected_membership(self):
        self.assertEqual(
            set(dbs.TABLE_POLICIES.values()),
            {dbs.MUTABLE, dbs.NO_DELETE, dbs.APPEND_ONLY, dbs.READ_ONLY},
        )
        policies = dbs.TABLE_POLICIES
        for table in ("audit_auditevent", "catalogue_copyevent"):
            self.assertEqual(policies[table], dbs.APPEND_ONLY, table)
        for table in ("members_member", "catalogue_bookedition", "catalogue_bookcopy"):
            self.assertEqual(policies[table], dbs.NO_DELETE, table)

    def test_policy_privileges_are_exactly_as_approved(self):
        p = dbs.POLICY_PRIVILEGES
        self.assertEqual(p[dbs.APPEND_ONLY], ("SELECT", "INSERT"))
        self.assertEqual(p[dbs.NO_DELETE], ("SELECT", "INSERT", "UPDATE"))
        self.assertEqual(p[dbs.MUTABLE], ("SELECT", "INSERT", "UPDATE", "DELETE"))
        self.assertEqual(p[dbs.READ_ONLY], ("SELECT",))
        for privileges in p.values():
            for never in ("TRUNCATE", "REFERENCES", "TRIGGER"):
                self.assertNotIn(never, privileges)

    def test_append_only_tables_have_required_triggers_registered(self):
        for table, policy in dbs.TABLE_POLICIES.items():
            if policy == dbs.APPEND_ONLY:
                self.assertIn(table, dbs.REQUIRED_TRIGGERS, table)

    def test_every_table_grants_match_its_class_for_the_runtime_role(self):
        for table, policy in dbs.TABLE_POLICIES.items():
            actual = {
                priv
                for priv in dbs.ALL_TABLE_PRIVILEGES
                if run(
                    "SELECT has_table_privilege(%s, ('public.'||quote_ident(%s))::regclass, %s)",
                    [RUNTIME, table, priv],
                )[0][0]
            }
            self.assertEqual(actual, set(dbs.POLICY_PRIVILEGES[policy]), f"{table} ({policy})")


class BaselineTests(TestCase):
    def test_database_matches_the_model_for_the_owner_connection(self):
        self.assertEqual(inspect_owner(), [])

    def test_system_check_is_clean(self):
        self.assertEqual(database_security(None), [])

    def test_check_command_succeeds_and_prints_no_credentials(self):
        out = StringIO()
        call_command("check_db_security", stdout=out)
        text = out.getvalue()
        self.assertIn("0 error(s)", text)
        self.assertNotIn(checked_secret(self, settings.DATABASES["default"]["PASSWORD"]), text)

    def test_json_output(self):
        out = StringIO()
        call_command("check_db_security", "--json", stdout=out)
        self.assertEqual(out.getvalue().strip(), "[]")


class GrantPlanTests(SimpleTestCase):
    databases = {"default"}

    def test_role_names_are_validated(self):
        for bad in ("", "Coven_App", "x; DROP TABLE y", "a b", "1abc", "a" * 64, None, 5):
            with self.assertRaises(ImproperlyConfigured, msg=repr(bad)):
                dbs.validate_role_name(bad)
        self.assertEqual(dbs.validate_role_name("coven_app"), "coven_app")

    def test_plan_has_no_default_privilege_grants_no_all_and_quotes_identifiers(self):
        text = "\n".join(dbs.render(connection, dbs.plan_grants(connection, RUNTIME)))
        self.assertNotIn("GRANT ALL", text)
        self.assertNotIn("TRUNCATE", text.replace("REVOKE ALL", ""))
        self.assertNotRegex(text, r"ALTER DEFAULT PRIVILEGES[^\n]*GRANT")
        self.assertIn('GRANT SELECT, INSERT ON TABLE "public"."audit_auditevent"', text)
        self.assertIn('GRANT SELECT, INSERT, UPDATE ON TABLE "public"."members_member"', text)
        self.assertNotIn('DELETE ON TABLE "public"."members_member"', text)
        self.assertNotIn(checked_secret(self, settings.DATABASES["default"]["PASSWORD"]), text)

    def test_plan_is_deterministic(self):
        a = dbs.render(connection, dbs.plan_grants(connection, RUNTIME))
        b = dbs.render(connection, dbs.plan_grants(connection, RUNTIME))
        self.assertEqual(a, b)


class FailClosedAndDriftTests(TestCase):
    """DDL is transactional in PostgreSQL, so every drift below is rolled back after each test."""

    def test_unclassified_table_gets_no_privileges_and_is_reported(self):
        run("CREATE TABLE zz_unclassified (id int)")
        self.assertIn("coven.E001", codes(inspect_owner()))
        statements = "\n".join(dbs.render(connection, dbs.plan_grants(connection, RUNTIME)))
        self.assertNotIn("zz_unclassified", statements)
        dbs.apply_grants(connection, RUNTIME)
        self.assertFalse(
            run("SELECT has_table_privilege(%s, 'zz_unclassified', 'SELECT')", [RUNTIME])[0][0]
        )
        self.assertIn("coven.E001", codes(inspect_owner()))  # still reported until classified

    def test_new_table_does_not_inherit_privileges_from_default_acl(self):
        run("CREATE TABLE zz_new (id serial primary key)")
        self.assertFalse(run("SELECT has_table_privilege(%s, 'zz_new', 'INSERT')", [RUNTIME])[0][0])

    def test_default_privileges_for_runtime_are_detected_and_removed(self):
        run(
            "ALTER DEFAULT PRIVILEGES FOR ROLE CURRENT_USER IN SCHEMA public "
            f'GRANT SELECT ON TABLES TO "{RUNTIME}"'
        )
        self.assertIn("coven.E002", codes(inspect_owner()))
        dbs.apply_grants(connection, RUNTIME)
        self.assertEqual(codes(inspect_owner()), [])

    def test_excess_privilege_on_append_only_table_is_detected_and_repaired(self):
        run(f'GRANT UPDATE, DELETE, TRUNCATE ON audit_auditevent TO "{RUNTIME}"')
        findings = [f for f in inspect_owner() if f.code == "coven.E002"]
        self.assertTrue(any("audit_auditevent" in f.message for f in findings))
        dbs.apply_grants(connection, RUNTIME)
        self.assertEqual(inspect_owner(), [])

    def test_missing_privilege_is_detected_and_repaired(self):
        run(f'REVOKE INSERT ON catalogue_copyevent FROM "{RUNTIME}"')
        self.assertIn("coven.E003", codes(inspect_owner()))
        dbs.apply_grants(connection, RUNTIME)
        self.assertEqual(inspect_owner(), [])

    def test_delete_on_no_delete_tables_is_detected(self):
        for table in ("members_member", "catalogue_bookedition", "catalogue_bookcopy"):
            run(f'GRANT DELETE ON {table} TO "{RUNTIME}"')
        errors = [f.message for f in inspect_owner() if f.code == "coven.E002"]
        self.assertEqual(len(errors), 3)

    def test_disabled_trigger_is_detected(self):
        run("ALTER TABLE audit_auditevent DISABLE TRIGGER audit_event_append_only")
        self.assertIn("coven.E008", codes(inspect_owner()))

    def test_missing_trigger_is_detected(self):
        run("DROP TRIGGER members_member_no_immutable ON members_member")
        self.assertIn("coven.E008", codes(inspect_owner()))

    def test_unpinned_search_path_is_a_warning_not_an_error(self):
        run("ALTER FUNCTION audit_event_reject_change() RESET search_path")
        self.assertEqual(codes(inspect_owner(), "warning"), ["coven.W003"])
        self.assertEqual(codes(inspect_owner()), [])

    def test_runtime_create_privileges_are_detected(self):
        run(f'GRANT CREATE ON SCHEMA public TO "{RUNTIME}"')
        self.assertIn("coven.E004", codes(inspect_owner()))
        dbs.apply_grants(connection, RUNTIME)
        self.assertEqual(inspect_owner(), [])
        run(f'GRANT TEMPORARY ON DATABASE "{connection.settings_dict["NAME"]}" TO "{RUNTIME}"')
        self.assertIn("coven.E004", codes(inspect_owner()))
        dbs.apply_grants(connection, RUNTIME)
        self.assertEqual(inspect_owner(), [])

    def test_runtime_sequence_update_privilege_is_detected(self):
        run(f'GRANT UPDATE ON SEQUENCE members_member_id_seq TO "{RUNTIME}"')
        self.assertIn("coven.E002", codes(inspect_owner()))

    def test_sequence_privileges_follow_the_table_class(self):
        # writable tables: USAGE + SELECT; read-only tables: SELECT only (pg_dump needs it)
        self.assertEqual(inspect_owner(), [])
        has = "SELECT has_sequence_privilege(%s, %s::regclass, %s)"
        self.assertTrue(run(has, [RUNTIME, "members_member_id_seq", "USAGE"])[0][0])
        self.assertTrue(run(has, [RUNTIME, "accounts_user_groups_id_seq", "SELECT"])[0][0])
        self.assertFalse(run(has, [RUNTIME, "accounts_user_groups_id_seq", "USAGE"])[0][0])
        run(f'REVOKE SELECT ON SEQUENCE accounts_user_groups_id_seq FROM "{RUNTIME}"')
        self.assertIn("coven.E003", codes(inspect_owner()))
        run(f'GRANT USAGE ON SEQUENCE accounts_user_groups_id_seq TO "{RUNTIME}"')
        self.assertIn("coven.E002", codes(inspect_owner()))
        dbs.apply_grants(connection, RUNTIME)
        self.assertEqual(inspect_owner(), [])

    def test_system_check_reports_drift_with_error_ids(self):
        run(f'GRANT TRUNCATE ON audit_auditevent TO "{RUNTIME}"')
        result = database_security(None)
        self.assertTrue(result)
        self.assertTrue(all(r.id.startswith("coven.") for r in result))
        self.assertEqual({r.level for r in result}, {40})  # ERROR

    def test_apply_is_idempotent(self):
        def snapshot():
            return run(
                "SELECT relname, relacl::text FROM pg_class "
                "WHERE relnamespace = 'public'::regnamespace AND relkind IN ('r','S') ORDER BY 1"
            )

        dbs.apply_grants(connection, RUNTIME)
        first = snapshot()
        dbs.apply_grants(connection, RUNTIME)
        self.assertEqual(first, snapshot())
        self.assertEqual(inspect_owner(), [])

    def test_post_migrate_hook_restores_grants(self):
        run(f'REVOKE ALL ON ALL TABLES IN SCHEMA public FROM "{RUNTIME}"')
        self.assertTrue(codes(inspect_owner()))
        apply_grants_after_migrate(sender=None, using="default")
        self.assertEqual(inspect_owner(), [])


class ModeTests(TestCase):
    def test_single_role_is_an_error_in_strict_mode(self):
        owner = run("SELECT current_user")[0][0]
        with override_settings(DB_SECURITY_MODE="strict"):
            self.assertIn("coven.E005", codes(dbs.inspect(connection, owner, "owner")))

    def test_single_role_is_only_a_warning_in_explicit_dev_mode(self):
        owner = run("SELECT current_user")[0][0]
        with override_settings(DB_SECURITY_MODE="single_role_dev"):
            findings = dbs.inspect(connection, owner, "owner")
        self.assertEqual((codes(findings), codes(findings, "warning")), ([], ["coven.W002"]))

    def test_runtime_process_connected_as_owner_is_refused(self):
        with override_settings(DB_ROLE="runtime"):
            with self.assertRaises(ImproperlyConfigured) as ctx:
                dbs.assert_runtime_security()
        self.assertIn("coven.E00", str(ctx.exception))
        self.assertNotIn(
            checked_secret(self, settings.DATABASES["default"]["PASSWORD"]), str(ctx.exception)
        )

    def test_startup_guard_is_skipped_for_owner_role_and_dev_mode(self):
        dbs.assert_runtime_security()  # DB_ROLE == owner under the test settings
        with override_settings(DB_ROLE="runtime", DB_SECURITY_MODE="single_role_dev"):
            dbs.assert_runtime_security()

    def test_unreachable_database_message_never_contains_credentials(self):
        secret = "SuperSecretPw-123"
        boom = OperationalError(f"password authentication failed with password {secret}")
        with (
            override_settings(DB_ROLE="runtime"),
            mock.patch.object(dbs, "inspect", side_effect=boom),
        ):
            with self.assertRaises(ImproperlyConfigured) as ctx:
                dbs.assert_runtime_security()
        self.assertNotIn(secret, str(ctx.exception))
        with mock.patch.object(dbs, "inspect", side_effect=boom):
            with self.assertRaises(CommandError) as ctx:
                call_command("check_db_security")
        self.assertNotIn(secret, str(ctx.exception))
        with mock.patch.object(dbs, "inspect", side_effect=boom):
            self.assertEqual(database_security(None)[0].id, "coven.W001")


class CommandGuardTests(TestCase):
    def test_migrate_is_refused_with_runtime_settings(self):
        with override_settings(DB_ROLE="runtime"):
            with self.assertRaises(CommandError) as ctx:
                call_command("migrate", verbosity=0)
        self.assertIn("config.settings.migrate", str(ctx.exception))

    def test_apply_db_grants_is_refused_with_runtime_settings(self):
        with override_settings(DB_ROLE="runtime"):
            with self.assertRaises(CommandError):
                call_command("apply_db_grants")

    def test_apply_db_grants_dry_run_prints_sql_only(self):
        out = StringIO()
        call_command("apply_db_grants", "--dry-run", stdout=out)
        self.assertIn('GRANT SELECT, INSERT ON TABLE "public"."audit_auditevent"', out.getvalue())
        self.assertEqual(inspect_owner(), [])

    def test_apply_db_grants_reports_missing_runtime_role_without_secrets(self):
        with override_settings(DB_RUNTIME_ROLE="no_such_role"):
            with self.assertRaises(CommandError) as ctx:
                call_command("apply_db_grants")
        self.assertIn("provision", str(ctx.exception))

    def test_apply_refuses_when_connected_as_the_runtime_role_name(self):
        me = run("SELECT current_user")[0][0]
        with override_settings(DB_RUNTIME_ROLE=me):
            with self.assertRaises(CommandError):
                call_command("apply_db_grants")
