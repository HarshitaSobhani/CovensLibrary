"""Full-stack checks across phases 1a-2: real PostgreSQL, real HTTP flow, schema guarantees."""

import io

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test import TestCase
from django.urls import reverse
from openpyxl import Workbook

from audit.models import AuditEvent
from catalogue.models import BookCopy, BookEdition, Category
from members.models import Member

PW = "Correct-Horse-9!"


def workbook_bytes():
    wb = Workbook()
    ws = wb.active
    ws.title = "Books"
    ws.append(["Title", "Author", "ISBN", "Category", "Copies"])
    ws.append(["Imported One", "Some Author", "9780261102217", "Fiction", 2])
    ws.append(["Imported Two", "Unknown author", "", "Poetry", 1])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


class EndToEndOwnerFlowTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            "owner", email="o@example.com", password=PW
        )

    def test_owner_session_from_login_to_import_and_logout(self):
        c = self.client
        # anonymous access is refused everywhere
        for url in ("/", "/members/", "/books/", "/books/import/"):
            self.assertEqual(c.get(url).status_code, 302, url)
        # login
        r = c.post(reverse("accounts:login"), {"username": "owner", "password": PW})
        self.assertRedirects(r, "/", fetch_redirect_response=False)
        self.assertContains(c.get("/"), "Coven&#x27;s Library")
        # catalogue: category, edition, copies, labels
        c.post(reverse("catalogue:category_list"), {"name": "Fiction", "is_active": "on"})
        cat = Category.objects.get(name="Fiction")
        r = c.post(
            reverse("catalogue:edition_create"),
            {"title": "Manual Entry", "category": cat.pk, "author": "Some Author"},
        )
        edition = BookEdition.objects.get(title="Manual Entry")
        self.assertRedirects(r, reverse("catalogue:edition_detail", args=[edition.pk]))
        c.post(
            reverse("catalogue:copies_add", args=[edition.pk]), {"count": 2, "source": "purchased"}
        )
        self.assertEqual(edition.copies.count(), 2)
        pdf = c.get(reverse("catalogue:labels_pdf", args=[edition.pk]))
        self.assertTrue(pdf.content.startswith(b"%PDF"))
        # member
        r = c.post(
            reverse("members:create"),
            {
                "full_name": "Asha Rao",
                "region": "IN",
                "national_number": "9876543210",
                "joined_on": "2026-11-11",
            },
        )
        member = Member.objects.get()
        self.assertEqual(
            (member.membership_no, member.mobile_e164), ("COV-M-000001", "+919876543210")
        )
        # excel import (create missing category, generate copy ids)
        r = c.post(
            reverse("catalogue:import_start"),
            {"file": SimpleUploadedFile("b.xlsx", workbook_bytes())},
        )
        batch_url = r["Location"]
        options = {
            "copy_id_mode": "generate",
            "create_missing_categories": "on",
            "isbn_warnings_acknowledged": "on",
        }
        c.post(batch_url, {**options, "refresh": "1"})  # change options: preview regenerated
        self.assertEqual(BookEdition.objects.count(), 1)  # nothing imported yet
        token = c.get(batch_url).context["preview_token"]
        c.post(batch_url, {**options, "confirm": "1", "preview_token": token})
        self.assertEqual(BookEdition.objects.count(), 3)
        self.assertTrue(Category.objects.filter(name="Poetry").exists())
        codes = list(BookCopy.objects.values_list("copy_code", flat=True))
        self.assertEqual(codes, [f"COV-C-{i:06d}" for i in range(1, 6)])
        # audit trail covers the whole session
        actions = set(AuditEvent.objects.values_list("action", flat=True))
        for expected in (
            "auth.login",
            "category.create",
            "edition.create",
            "copy.add",
            "member.create",
            "import.stage",
            "import.confirm",
        ):
            self.assertIn(expected, actions)
        # logout, then protected pages are locked again; logging in again works
        c.post(reverse("accounts:logout"))
        self.assertEqual(c.get("/members/").status_code, 302)
        r = c.post(reverse("accounts:login"), {"username": "owner", "password": PW})
        self.assertEqual(r.status_code, 302)


class SchemaGuaranteeTests(TestCase):
    """Verifies guarantees at the PostgreSQL level (not only in Python)."""

    def scalar(self, sql, params=None):
        with connection.cursor() as cur:
            cur.execute(sql, params or [])
            return cur.fetchone()[0]

    def test_running_on_postgresql_16_or_newer(self):
        self.assertEqual(connection.vendor, "postgresql")
        self.assertGreaterEqual(connection.pg_version // 10000, 16)

    def test_instants_stored_in_utc_and_converted_to_kolkata(self):
        # Django stores aware datetimes in UTC (connection timezone UTC); application dates and
        # "end of day" are computed in Asia/Kolkata.
        from datetime import datetime
        from datetime import timezone as dt_timezone

        from django.utils import timezone

        self.assertEqual(self.scalar("SHOW TIMEZONE"), "UTC")
        self.assertEqual(timezone.get_current_timezone_name(), "Asia/Kolkata")
        instant = datetime(2026, 11, 11, 20, 0, tzinfo=dt_timezone.utc)  # 01:30 next day in India
        self.assertEqual(timezone.localtime(instant).date().isoformat(), "2026-11-12")

    def test_expected_triggers_exist(self):
        names = {r[0] for r in self._rows("SELECT tgname FROM pg_trigger WHERE NOT tgisinternal")}
        for expected in (
            "audit_event_append_only",
            "catalogue_copyevent_append_only",
            "members_member_no_immutable",
        ):
            self.assertIn(expected, names)

    def test_expected_constraints_exist(self):
        names = {r[0] for r in self._rows("SELECT conname FROM pg_constraint")}
        for expected in (
            "copy_poor_requires_withdrawn",
            "copy_available_or_borrowed_requires_good",
            "copy_lost_condition_matches_status",
            "copy_damaged_condition_matches_status",
            "library_setting_singleton",
            "id_sequence_unique_name_year",
            "import_row_unique",
        ):
            self.assertIn(expected, names)

    def test_btree_gist_is_managed_by_the_owner_role_not_the_runtime_role(self):
        # Needed from Phase 3 for the subscription non-overlap constraint. The suite runs as the
        # OWNER role, which may create the extension (trusted in PostgreSQL 13+) and is not a
        # superuser; the runtime role's inability is proven in the core privilege tests.
        with connection.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")
            cur.execute("SELECT 1 FROM pg_extension WHERE extname = 'btree_gist'")
            self.assertEqual(cur.fetchone()[0], 1)
            cur.execute("SELECT rolsuper FROM pg_roles WHERE rolname = current_user")
            self.assertFalse(cur.fetchone()[0], "the owner role must not be a superuser")
            cur.execute(
                "SELECT current_user = pg_get_userbyid(datdba) FROM pg_database "
                "WHERE datname = current_database()"
            )
            self.assertTrue(cur.fetchone()[0], "the suite runs as the database owner role")

    def _rows(self, sql):
        with connection.cursor() as cur:
            cur.execute(sql)
            return cur.fetchall()
