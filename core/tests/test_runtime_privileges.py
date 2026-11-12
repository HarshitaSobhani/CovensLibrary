"""ACTUAL privilege tests: real statements executed over a real connection as the RUNTIME role.

The suite itself runs as the owner role (it creates the test database and flushes tables), so this
module opens a second connection to the same test database as ``coven_app`` using the runtime
credentials from DB_USER / DB_PASSWORD, and proves what that role can and cannot do. It runs in a
TransactionTestCase, so everything it writes is committed (the runtime connection must see it)
and flushed afterwards.
"""

import json
import os
import subprocess
import sys

import psycopg
from django.conf import settings
from django.db import connection
from django.test import TransactionTestCase
from psycopg import errors

from core import dbsecurity as dbs

RUNTIME = settings.DB_RUNTIME_ROLE


def runtime_connection():
    s = connection.settings_dict
    password = os.environ.get("DB_PASSWORD")
    if not RUNTIME or not password:
        raise AssertionError(
            "Runtime credentials (DB_USER / DB_PASSWORD) are required for these tests."
        )
    return psycopg.connect(
        host=s["HOST"],
        port=s["PORT"],
        dbname=s["NAME"],
        user=RUNTIME,
        password=password,
        autocommit=True,
    )


def owner_sql(sql, params=None):
    with connection.cursor() as cur:
        cur.execute(sql, params or [])


class RuntimePrivilegeTests(TransactionTestCase):
    def setUp(self):
        self.rt = runtime_connection()
        self.addCleanup(self.rt.close)
        # flushes between TransactionTestCases remove the migration-seeded plans
        owner_sql(
            "INSERT INTO memberships_subscriptionplan (code, name, duration_months, price_paise, "
            "is_active) VALUES ('6m','6-month subscription',6,80000,true),"
            "('12m','12-month subscription',12,150000,true) ON CONFLICT (code) DO NOTHING"
        )
        # a user, member, edition, copy and an audit row created by the OWNER connection
        owner_sql(
            "INSERT INTO members_member (membership_no, full_name, mobile_e164, email, joined_on, "
            "notes, status, needs_new_subscription, created_at, updated_at) VALUES "
            "('COV-M-900001','M','+910000000001','',current_date,'','active',false,now(),now())"
        )
        owner_sql("INSERT INTO catalogue_category (name, is_active) VALUES ('Cat', true)")
        owner_sql(
            "INSERT INTO catalogue_bookedition (title, author, publisher, edition_label, isbn, "
            "description, notes, is_archived, created_at, category_id) SELECT 'E','','','','','',"
            "'',false,now(),id FROM catalogue_category LIMIT 1"
        )
        owner_sql(
            "INSERT INTO catalogue_bookcopy (copy_code, barcode, condition, status, shelf_location, "
            "notes, legacy_ref, created_at, edition_id) SELECT 'C1','C1','good','available','',"
            "'','',now(),id FROM catalogue_bookedition LIMIT 1"
        )
        owner_sql(
            "INSERT INTO audit_auditevent (action, entity_type, entity_id, summary, occurred_at) "
            "VALUES ('seed','','','{}',now())"
        )
        owner_sql(
            "INSERT INTO catalogue_copyevent (event_type, from_value, to_value, note, event_date, "
            "processed_at, copy_id) SELECT 'acquired','','','',current_date,now(),id "
            "FROM catalogue_bookcopy LIMIT 1"
        )

    def run_rt(self, sql, params=None):
        with self.rt.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall() if cur.description else None

    def assertDenied(self, sql, params=None):
        with self.assertRaises(errors.InsufficientPrivilege, msg=sql):
            self.run_rt(sql, params)

    # ---- identity and role attributes -----------------------------------------------------
    def test_connection_is_the_runtime_role_with_no_special_attributes(self):
        self.assertEqual(self.run_rt("SELECT current_user")[0][0], RUNTIME)
        attrs = self.run_rt(
            "SELECT rolsuper, rolcreatedb, rolcreaterole, rolreplication, "
            "rolbypassrls FROM pg_roles WHERE rolname = current_user"
        )[0]
        self.assertEqual(attrs, (False, False, False, False, False))
        owner = self.run_rt(
            "SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname = current_database()"
        )[0][0]
        self.assertNotEqual(owner, RUNTIME)
        self.assertFalse(
            self.run_rt("SELECT pg_has_role(current_user, %s, 'MEMBER')", [owner])[0][0]
        )
        self.assertEqual(
            self.run_rt(
                "SELECT count(*) FROM pg_tables WHERE schemaname='public' "
                "AND tableowner = current_user"
            )[0][0],
            0,
        )

    def test_security_inspection_passes_when_run_as_the_runtime_role(self):
        """The same verification used by check_db_security / the startup guard, as coven_app."""

        class Adapter:  # dbsecurity only needs ensure_connection() and a psycopg connection
            def __init__(self, conn):
                self.connection = conn

            def ensure_connection(self):
                return None

        self.assertEqual(dbs.inspect(Adapter(self.rt), RUNTIME, "runtime"), [])

    # ---- what the application needs (allowed) ---------------------------------------------
    def test_normal_application_operations_are_allowed(self):
        rt = self.run_rt
        rt(
            "INSERT INTO audit_auditevent (action, entity_type, entity_id, summary, occurred_at) "
            "VALUES ('rt','','','{}',now())"
        )
        self.assertGreaterEqual(rt("SELECT count(*) FROM audit_auditevent")[0][0], 2)
        rt(
            "INSERT INTO catalogue_copyevent (event_type, from_value, to_value, note, event_date, "
            "processed_at, copy_id) SELECT 'condition_changed','a','b','',current_date,now(),id "
            "FROM catalogue_bookcopy LIMIT 1"
        )
        rt("UPDATE members_member SET full_name = 'Renamed' WHERE membership_no = 'COV-M-900001'")
        rt("UPDATE catalogue_bookcopy SET status = 'withdrawn' WHERE copy_code = 'C1'")
        rt("UPDATE catalogue_bookedition SET is_archived = true")
        rt(
            "INSERT INTO accounts_loginevent (username_tried, username_key, outcome, occurred_at) "
            "VALUES ('', '', 'failed', now())"
        )
        rt("DELETE FROM accounts_loginevent")  # mutable: pruning is allowed
        rt(
            "INSERT INTO django_session (session_key, session_data, expire_date) "
            "VALUES ('k','d',now())"
        )
        rt("DELETE FROM django_session")
        rt("UPDATE core_idsequence SET last_value = last_value WHERE false")
        self.assertEqual(rt("SELECT count(*) FROM django_migrations")[0][0] > 0, True)
        self.assertGreater(rt("SELECT nextval('members_member_id_seq')")[0][0], 0)

    # ---- append-only tables ---------------------------------------------------------------
    def test_append_only_tables_reject_update_delete_and_truncate(self):
        for table in (
            "audit_auditevent",
            "catalogue_copyevent",
            "finance_financialtransaction",
            "finance_paymententry",
            "finance_receiptdocument",
            "finance_tokenaccount",
            "finance_tokencycle",
            "finance_tokenledgerentry",
            "circulation_loanrenewal",
            "core_settingchangeevent",
            "core_domainevent",
            "communications_notificationattempt",
        ):
            self.assertDenied(f"UPDATE {table} SET id = id")
            self.assertDenied(f"DELETE FROM {table}")
            self.assertDenied(f"TRUNCATE {table}")
            self.assertDenied(f"TRUNCATE {table} CASCADE")
            self.assertDenied(f"SELECT id FROM {table} FOR UPDATE")
            self.assertDenied(f"SELECT id FROM {table} FOR KEY SHARE")
        self.assertDenied(
            "INSERT INTO audit_auditevent (id, action, entity_type, entity_id, summary, "
            "occurred_at) VALUES (1,'x','','','{}',now()) "
            "ON CONFLICT (id) DO UPDATE SET action='y'"
        )

    # ---- no-delete tables -----------------------------------------------------------------
    def test_no_delete_tables_reject_delete_but_allow_insert_and_update(self):
        for table in ("members_member", "catalogue_bookedition", "catalogue_bookcopy"):
            self.assertDenied(f"DELETE FROM {table}")
            self.assertDenied(f"TRUNCATE {table}")
        self.assertDenied("DELETE FROM memberships_subscriptionplan")
        self.assertDenied("DELETE FROM memberships_membersubscription")
        self.assertDenied("DELETE FROM circulation_loan")
        self.assertDenied("DELETE FROM circulation_pendingforfeiture")
        self.assertDenied("DELETE FROM communications_notification")
        self.assertDenied("TRUNCATE communications_notification")
        self.assertDenied("TRUNCATE circulation_pendingforfeiture")
        self.assertDenied("TRUNCATE circulation_loan")
        self.assertDenied("TRUNCATE memberships_membersubscription")
        self.assertDenied("TRUNCATE memberships_subscriptionplan")
        self.assertDenied("DELETE FROM accounts_user")
        self.assertDenied("DELETE FROM core_librarysetting")
        self.run_rt("UPDATE members_member SET notes = 'ok'")
        self.assertEqual(self.run_rt("SELECT notes FROM members_member")[0][0], "ok")

    def test_read_only_tables_are_read_only(self):
        for table in (
            "django_migrations",
            "django_content_type",
            "auth_permission",
            "auth_group",
            "accounts_user_groups",
        ):
            self.assertDenied(f"DELETE FROM {table}")
            self.assertDenied(
                f"UPDATE {table} SET id = id"
            ) if table != "django_migrations" else None
            self.run_rt(f"SELECT count(*) FROM {table}")
        self.assertDenied(
            "INSERT INTO django_migrations (app, name, applied) VALUES ('x','y',now())"
        )

    def test_truncate_is_denied_on_every_table(self):
        for table in dbs.TABLE_POLICIES:
            self.assertDenied(f"TRUNCATE {table}")

    # ---- protections cannot be altered or removed -----------------------------------------
    def test_triggers_and_trigger_functions_cannot_be_altered_or_removed(self):
        for sql in (
            "DROP TRIGGER audit_event_append_only ON audit_auditevent",
            "ALTER TABLE audit_auditevent DISABLE TRIGGER audit_event_append_only",
            "ALTER TABLE audit_auditevent DISABLE TRIGGER ALL",
            "ALTER TABLE audit_auditevent DISABLE TRIGGER USER",
            "ALTER TABLE catalogue_copyevent DISABLE TRIGGER ALL",
            "ALTER TABLE members_member DISABLE TRIGGER members_member_no_immutable",
            "ALTER TABLE audit_auditevent ENABLE ALWAYS TRIGGER audit_event_append_only",
            "ALTER TRIGGER audit_event_append_only ON audit_auditevent RENAME TO x",
            "CREATE TRIGGER x BEFORE INSERT ON audit_auditevent FOR EACH ROW "
            "EXECUTE FUNCTION audit_event_reject_change()",
            "CREATE OR REPLACE FUNCTION audit_event_reject_change() RETURNS trigger AS $$ BEGIN "
            "RETURN NEW; END; $$ LANGUAGE plpgsql",
            "DROP FUNCTION audit_event_reject_change() CASCADE",
            "ALTER FUNCTION audit_event_reject_change() RESET search_path",
            "ALTER FUNCTION audit_event_reject_change() OWNER TO current_user",
        ):
            self.assertDenied(sql)

    def test_triggers_still_fire_for_the_runtime_role(self):
        with self.assertRaises(errors.RaiseException) as ctx:
            self.run_rt("UPDATE members_member SET membership_no = 'COV-M-999999'")
        self.assertIn("permanent", str(ctx.exception))

    def test_schema_ownership_and_object_creation_are_denied(self):
        for sql in (
            "ALTER TABLE audit_auditevent OWNER TO current_user",
            "ALTER TABLE members_member ADD COLUMN x int",
            "ALTER TABLE members_member DROP COLUMN notes",
            "DROP TABLE audit_auditevent",
            "DROP TABLE members_member CASCADE",
            "CREATE TABLE zz_new (a int)",
            "CREATE VIEW zz_v AS SELECT 1",
            "CREATE FUNCTION zz_f() RETURNS int AS 'select 1' LANGUAGE sql",
            "CREATE SCHEMA zz_schema",
            "CREATE TEMP TABLE zz_tmp (a int)",
            "CREATE EXTENSION IF NOT EXISTS pg_trgm",
            "CREATE EXTENSION IF NOT EXISTS pgcrypto",
            "DROP EXTENSION btree_gist",
            "ALTER SEQUENCE members_member_id_seq RESTART",
            "DROP SEQUENCE members_member_id_seq CASCADE",
            f'ALTER DATABASE "{connection.settings_dict["NAME"]}" SET search_path = zz',
            f'DROP DATABASE "{connection.settings_dict["NAME"]}"',
        ):
            self.assertDenied(sql)

    def test_privilege_escalation_is_denied(self):
        owner = self.run_rt(
            "SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname = current_database()"
        )[0][0]
        for sql in (
            f'SET ROLE "{owner}"',
            "SET ROLE postgres",
            "SET session_replication_role = replica",
            f'GRANT "{owner}" TO "{RUNTIME}"',
            f'ALTER ROLE "{RUNTIME}" SUPERUSER',
            f'ALTER ROLE "{RUNTIME}" CREATEDB',
            f'ALTER ROLE "{RUNTIME}" CREATEROLE',
            "CREATE ROLE zz_role",
            f'REASSIGN OWNED BY "{owner}" TO "{RUNTIME}"',
            f'ALTER DEFAULT PRIVILEGES FOR ROLE "{owner}" IN SCHEMA public GRANT ALL ON TABLES TO "{RUNTIME}"',
            "COPY audit_auditevent TO PROGRAM 'true'",
            "COPY audit_auditevent TO '/tmp/zz_audit.csv'",
        ):
            with self.assertRaises(
                (errors.InsufficientPrivilege, errors.FeatureNotSupported), msg=sql
            ):
                self.run_rt(sql)

    def test_self_grant_has_no_effect(self):
        with self.rt.cursor() as cur:
            cur.execute(f'GRANT UPDATE, DELETE, TRUNCATE ON audit_auditevent TO "{RUNTIME}"')
            cur.execute(
                "SELECT has_table_privilege(current_user, 'audit_auditevent', 'UPDATE'), "
                "has_table_privilege(current_user, 'audit_auditevent', 'DELETE'), "
                "has_table_privilege(current_user, 'audit_auditevent', 'TRUNCATE')"
            )
            self.assertEqual(cur.fetchone(), (False, False, False))

    # ---- owner-only operations stay with the owner ----------------------------------------
    def test_extension_management_belongs_to_the_owner(self):
        self.assertDenied("CREATE EXTENSION IF NOT EXISTS pg_trgm")
        # btree_gist (needed by the subscription non-overlap constraint) is created by the
        # migrations, i.e. by the OWNER role; running it again as the owner is harmless.
        owner_sql("CREATE EXTENSION IF NOT EXISTS btree_gist")
        owner = self.run_rt(
            "SELECT pg_get_userbyid(extowner) FROM pg_extension WHERE extname='btree_gist'"
        )[0][0]
        self.assertNotEqual(owner, RUNTIME)
        self.assertDenied("DROP EXTENSION btree_gist")

    def test_owner_connection_is_still_bound_by_the_append_only_triggers(self):
        from django.db import DatabaseError, transaction

        for sql in (
            "UPDATE audit_auditevent SET action='x'",
            "DELETE FROM audit_auditevent",
            "UPDATE catalogue_copyevent SET note='x'",
            "DELETE FROM catalogue_copyevent",
        ):
            with self.assertRaises(DatabaseError, msg=sql), transaction.atomic():
                owner_sql(sql)

    # ---- the real application, end to end, as the runtime role ----------------------------
    def test_application_flow_works_as_the_runtime_role_without_owner_credentials(self):
        script = r"""
import io, json, os
import django
django.setup()
from django.conf import settings
settings.ALLOWED_HOSTS = ["*"]
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import DatabaseError, connection, transaction
from django.test import Client
from openpyxl import Workbook
from audit.models import AuditEvent
from catalogue import import_service as svc, services as cs
from catalogue.models import Category
from members import services as ms

assert "DB_OWNER_USER" not in os.environ and "DB_OWNER_PASSWORD" not in os.environ
out = {"role": connection.cursor().connection.info.user}
cursor = connection.cursor(); cursor.execute("SELECT current_user"); out["current_user"] = cursor.fetchone()[0]
User = get_user_model()
owner = User.objects.create_user("owner", email="o@example.com", password="Correct-Horse-9!")
c = Client()
out["login"] = c.post("/accounts/login/", {"username": "owner", "password": "Correct-Horse-9!"}).status_code
out["dashboard"] = c.get("/").status_code
out["phase5a"] = [
    c.get("/calendar/").status_code,
    c.get("/search/?q=asha").status_code,
    c.get("/settings/").status_code,
    c.post("/settings/", {"action": "down", "section": "overdue"}).status_code,
    c.post("/settings/", {"action": "save", "visible": ["counts"], "expiry_window_days": "20"}).status_code,
    c.get("/").status_code,
]
m = ms.create_member(actor=owner, full_name="Runtime", national_number="9811100001", region="IN")
cat = Category.objects.create(name="Fiction")
e = cs.create_edition(actor=owner, title="T", category=cat)
copies = cs.add_copies(e, 2, actor=owner, source="purchased")
cs.set_copy_state(copies[0], condition="poor", status="withdrawn", actor=owner)
wb = Workbook(); ws = wb.active; ws.title = "Books"; ws.append(svc.COLUMNS)
row = dict.fromkeys(svc.COLUMNS, ""); row.update({"Title": "Imp", "Category": "Fiction", "Copies": 2})
ws.append([row[k] for k in svc.COLUMNS]); b = io.BytesIO(); wb.save(b)
batch = svc.stage_import(SimpleUploadedFile("b.xlsx", b.getvalue()), actor=owner)
svc.set_options(batch, create_missing_categories=False, copy_id_mode="generate", isbn_warnings_acknowledged=True)
svc.execute_import(batch, actor=owner)
out["member"] = m.membership_no
out["copies"] = sum(1 for _ in cs.search_editions("", False).values_list("copies"))
out["pdf"] = c.get(f"/books/{e.pk}/labels.pdf").status_code
try:
    with transaction.atomic():
        AuditEvent.objects.update(action="tamper")
    out["tamper"] = "ALLOWED"
except DatabaseError as exc:
    out["tamper"] = "denied"
try:
    with transaction.atomic():
        ms.Member.objects.all()._raw_delete(using="default")
    out["delete_members"] = "ALLOWED"
except DatabaseError:
    out["delete_members"] = "denied"
from datetime import date
from finance.models import FinancialTransaction
from finance.services import Entry
from memberships import services as mem
reg = mem.register_member(
    actor=owner, member_fields={"full_name": "Paid", "national_number": "9811100002", "region": "IN"},
    plan_code="6m", subscription_entries=[Entry("cash", 30000), Entry("upi", 50000, reference="U")],
    token_entries=[Entry("cash", 50000)])
out["registered"] = [t.receipt_no for t in reg.transactions]
mem.renew_subscription(actor=owner, member=reg.member, plan_code="12m", entries=[Entry("cash", 150000)])
out["subscriptions"] = reg.member.subscriptions.count()
for name, action in (
    ("fin_update", lambda: FinancialTransaction.objects.update(reason="x")),
    ("fin_delete", lambda: FinancialTransaction.objects.all()._raw_delete(using="default")),
    ("sub_edit", lambda: reg.member.subscriptions.update(starts_on=date(2000, 1, 1))),
    ("sub_delete", lambda: reg.member.subscriptions.all()._raw_delete(using="default")),
):
    try:
        with transaction.atomic():
            action()
        out[name] = "ALLOWED"
    except DatabaseError:
        out[name] = "denied"
from datetime import timedelta
from django.utils import timezone
from circulation import forfeiture, services as loans
from circulation.models import Loan
from finance.models import FinancialTransaction as Txn
today = timezone.localdate()
book = cs.add_copies(e, 1, actor=owner, source="purchased")[0]
loan = loans.issue_loan(actor=owner, member=reg.member, copy=book)
out["loan_status"] = loan.status
renewal = loans.renew_loan(actor=owner, loan=loan)
out["renewal_due"] = (renewal.new_due_on - loan.due_on).days
job = forfeiture.process_overdue_forfeitures(today=renewal.new_due_on + timedelta(days=8))
out["job_forfeited"] = job["forfeited"]
txn = Txn.objects.get(kind="token_forfeiture", member=reg.member)
out["system_actor"] = [txn.recorded_by_id is None, txn.recorded_by_system]
for name, action in (
    ("loan_delete", lambda: Loan.objects.all()._raw_delete(using="default")),
    ("loan_reopen", lambda: Loan.objects.filter(pk=loan.pk).update(issued_on=today - timedelta(days=400))),
    ("loan_due_back", lambda: Loan.objects.filter(pk=loan.pk).update(due_on=today - timedelta(days=900))),
):
    try:
        with transaction.atomic():
            action()
        out[name] = "ALLOWED"
    except DatabaseError:
        out[name] = "denied"
out["audit_rows"] = AuditEvent.objects.count()
from core.models import DomainEvent
from communications import services as comms
from communications.models import Notification, NotificationAttempt
cycle = comms.run_cycle()
out["comms"] = [
    cycle["consumed"] > 0,
    Notification.objects.filter(status="smtp_accepted").count() > 0,
    NotificationAttempt.objects.count() > 0,
    comms.unconsumed_events().count(),
]
from members import services as msvc
reg2 = mem.register_member(
    actor=owner,
    member_fields={"full_name": "Erase Me", "national_number": "9811100003", "region": "IN", "email": "erase@example.com"},
    plan_code="6m", subscription_entries=[Entry("cash", 80000)], token_entries=[Entry("cash", 50000)])
comms.run_cycle()
mine = Notification.objects.filter(member=reg2.member)
out["before_anon"] = [mine.filter(status="smtp_accepted").count() > 0, mine.exclude(body="").exists()]
msvc.anonymise_member(reg2.member, actor=owner, reason="runtime check", confirm=True)
out["after_anon"] = [
    mine.exclude(body="").exists(),
    mine.exclude(subject="").exists(),
    sorted(set(mine.values_list("recipient_email", flat=True))),
]
out["redaction_audit"] = AuditEvent.objects.filter(action="notifications.redacted").count()
for name, action in (
    ("notif_delete", lambda: Notification.objects.all()._raw_delete(using="default")),
    ("notif_reopen", lambda: Notification.objects.filter(status="smtp_accepted").update(status="prepared")),
    ("attempt_edit", lambda: NotificationAttempt.objects.update(error="x")),
    ("attempt_delete", lambda: NotificationAttempt.objects.all()._raw_delete(using="default")),
):
    try:
        with transaction.atomic():
            action()
        out[name] = "ALLOWED"
    except DatabaseError:
        out[name] = "denied"
out["outbox_types"] = sorted(set(DomainEvent.objects.values_list("event_type", flat=True)))
for name, action in (
    ("outbox_update", lambda: DomainEvent.objects.update(event_type="x")),
    ("outbox_delete", lambda: DomainEvent.objects.all()._raw_delete(using="default")),
):
    try:
        with transaction.atomic():
            action()
        out[name] = "ALLOWED"
    except DatabaseError:
        out[name] = "denied"
print("RESULT" + json.dumps(out))
"""
        env = {k: v for k, v in os.environ.items() if not k.startswith("DB_OWNER")}
        env.update(
            {
                "DJANGO_SETTINGS_MODULE": "config.settings.local",
                "DB_NAME": connection.settings_dict["NAME"],
                "DB_HOST": str(connection.settings_dict["HOST"]),
                "DB_PORT": str(connection.settings_dict["PORT"]),
                "DB_USER": RUNTIME,
                "PYTHONDONTWRITEBYTECODE": "1",
            }
        )
        env.pop("DB_SECURITY_MODE", None)
        done = subprocess.run(
            [sys.executable, "-c", script],
            env=env,
            capture_output=True,
            text=True,
            cwd=settings.BASE_DIR,
        )
        self.assertEqual(done.returncode, 0, done.stderr[-3500:])
        result = json.loads(done.stdout.split("RESULT", 1)[1])
        self.assertEqual(result["current_user"], RUNTIME)
        self.assertEqual((result["login"], result["dashboard"], result["pdf"]), (302, 200, 200))
        self.assertEqual(result["phase5a"], [200, 200, 200, 302, 302, 200])
        self.assertEqual(result["member"], "COV-M-000001")
        self.assertEqual(result["tamper"], "denied")
        self.assertEqual(result["delete_members"], "denied")
        self.assertGreater(result["audit_rows"], 5)
        self.assertEqual(len(result["registered"]), 2)
        self.assertEqual(result["subscriptions"], 2)
        for name in ("fin_update", "fin_delete", "sub_edit", "sub_delete"):
            self.assertEqual(result[name], "denied", name)
        self.assertEqual((result["loan_status"], result["renewal_due"]), ("active", 30))
        self.assertEqual(result["job_forfeited"], 1)
        self.assertEqual(result["system_actor"], [True, "overdue_forfeiture_job"])
        for name in (
            "loan_delete",
            "loan_reopen",
            "loan_due_back",
            "outbox_update",
            "outbox_delete",
        ):
            self.assertEqual(result[name], "denied", name)
        for kind in ("member.registered", "loan.issued", "loan.renewed", "token.forfeited"):
            self.assertIn(kind, result["outbox_types"])
