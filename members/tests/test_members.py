from django.contrib.auth import get_user_model
from django.db import DatabaseError, IntegrityError, connection, transaction
from django.test import TestCase
from django.urls import reverse

from audit.models import AuditEvent
from core.errors import RuleViolation
from members import services
from members.models import Member

PW = "Correct-Horse-9!"


def make(actor, name="Asha Rao", number="9876543210", region="IN", **kw):
    return services.create_member(
        actor=actor, full_name=name, national_number=number, region=region, **kw
    )


class ServiceTests(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_user("owner", password=PW)

    def test_ids_are_sequential_and_formatted(self):
        a, b = make(self.owner), make(self.owner, "Bo", "9876543211")
        self.assertEqual((a.membership_no, b.membership_no), ("COV-M-000001", "COV-M-000002"))

    def test_phone_normalised_to_e164_with_country_picker(self):
        self.assertEqual(make(self.owner).mobile_e164, "+919876543210")
        us = make(self.owner, "Sam", "(415) 555-2671", region="US")
        self.assertEqual(us.mobile_e164, "+14155552671")

    def test_same_number_entered_differently_is_a_duplicate(self):
        make(self.owner)
        for variant in ("98765 43210", "+91 98765 43210", "09876543210"):
            with self.assertRaises(RuleViolation) as ctx:
                make(self.owner, "Other", variant)
            self.assertIn(ctx.exception.code, {"mobile_duplicate", "mobile_invalid"})

    def test_duplicate_mobile_rejected_with_message(self):
        make(self.owner)
        with self.assertRaises(RuleViolation) as ctx:
            make(self.owner, "Other")
        self.assertEqual(ctx.exception.code, "mobile_duplicate")

    def test_database_enforces_unique_mobile_and_member_no(self):
        m = make(self.owner)
        with self.assertRaises(IntegrityError), transaction.atomic():
            Member.objects.create(
                membership_no="COV-M-999999",
                full_name="x",
                mobile_e164=m.mobile_e164,
                joined_on="2026-01-01",
            )
        with self.assertRaises(IntegrityError), transaction.atomic():
            Member.objects.create(
                membership_no=m.membership_no,
                full_name="x",
                mobile_e164="+10000000001",
                joined_on="2026-01-01",
            )

    def test_invalid_number_rejected(self):
        with self.assertRaises(RuleViolation) as ctx:
            make(self.owner, number="12345")
        self.assertEqual(ctx.exception.code, "mobile_invalid")

    def test_member_id_is_immutable_in_database(self):
        m = make(self.owner)
        with self.assertRaises(DatabaseError), transaction.atomic():
            Member.objects.filter(pk=m.pk).update(membership_no="COV-M-000777")
        with connection.cursor() as cur, self.assertRaises(DatabaseError), transaction.atomic():
            cur.execute("UPDATE members_member SET membership_no='X' WHERE id=%s", [m.pk])
        m.refresh_from_db()
        self.assertEqual(m.membership_no, "COV-M-000001")

    def test_no_hard_delete(self):
        m = make(self.owner)
        with self.assertRaises(RuleViolation):
            m.delete()
        self.assertEqual(Member.objects.count(), 1)

    def test_edit_keeps_id_and_audits_changes_only(self):
        m = make(self.owner)
        services.update_member(
            m,
            actor=self.owner,
            full_name="Asha R",
            national_number="9876543210",
            region="IN",
            email="a@example.com",
        )
        m.refresh_from_db()
        self.assertEqual((m.membership_no, m.full_name), ("COV-M-000001", "Asha R"))
        ev = AuditEvent.objects.get(action="member.update")
        self.assertEqual(ev.summary["changed_fields"], ["full_name", "email"])
        self.assertNotIn("a@example.com", str(ev.summary))

    def test_edit_to_another_members_number_rejected(self):
        make(self.owner)
        b = make(self.owner, "Bo", "9876543211")
        with self.assertRaises(RuleViolation):
            services.update_member(
                b, actor=self.owner, full_name="Bo", national_number="9876543210", region="IN"
            )

    def test_status_transitions_and_audit(self):
        m = make(self.owner)
        services.suspend_member(m, actor=self.owner)
        self.assertEqual(m.status, "suspended")
        with self.assertRaises(RuleViolation):
            services.suspend_member(m, actor=self.owner)
        services.reactivate_member(m, actor=self.owner, mode="restore")
        self.assertEqual((m.status, m.needs_new_subscription), ("active", False))
        services.archive_member(m, actor=self.owner)
        services.reactivate_member(m, actor=self.owner, mode="new_subscription")
        self.assertEqual((m.status, m.needs_new_subscription), ("active", True))
        actions = list(AuditEvent.objects.values_list("action", flat=True).order_by("id"))
        self.assertEqual(
            actions,
            [
                "member.create",
                "member.suspend",
                "member.reactivate",
                "member.archive",
                "member.reactivate",
            ],
        )

    def test_reactivation_requires_valid_mode(self):
        m = make(self.owner)
        services.archive_member(m, actor=self.owner)
        with self.assertRaises(RuleViolation):
            services.reactivate_member(m, actor=self.owner, mode="whatever")

    def test_anonymise_requires_confirmation_and_reason(self):
        m = make(self.owner, email="a@example.com", notes="private")
        with self.assertRaises(RuleViolation):
            services.anonymise_member(m, actor=self.owner, reason="x", confirm=False)
        with self.assertRaises(RuleViolation):
            services.anonymise_member(m, actor=self.owner, reason=" ", confirm=True)
        services.anonymise_member(m, actor=self.owner, reason="Requested by member", confirm=True)
        m.refresh_from_db()
        self.assertEqual(
            (m.membership_no, m.full_name, m.email, m.notes, m.status),
            ("COV-M-000001", "Anonymised member", "", "", "archived"),
        )
        self.assertTrue(m.mobile_e164.startswith("anon-"))
        self.assertIsNotNone(m.anonymised_at)
        self.assertTrue(AuditEvent.objects.filter(action="member.anonymise").exists())
        with self.assertRaises(RuleViolation):
            services.anonymise_member(m, actor=self.owner, reason="again", confirm=True)
        with self.assertRaises(RuleViolation):
            services.reactivate_member(m, actor=self.owner, mode="restore")

    def test_search(self):
        make(self.owner, "Asha Rao", "9876543210")
        make(self.owner, "Bo Lee", "9876543211")
        self.assertEqual(services.search_members("asha").count(), 1)
        self.assertEqual(services.search_members("COV-M-000002").count(), 1)
        self.assertEqual(services.search_members("+919876543211").count(), 1)
        self.assertEqual(services.search_members("").count(), 2)
        services.archive_member(Member.objects.get(full_name="Bo Lee"), actor=self.owner)
        self.assertEqual(services.search_members("", "archived").count(), 1)


class ViewTests(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_user("owner", password=PW)

    def test_all_member_pages_require_login(self):
        m = make(self.owner)
        urls = [
            reverse("members:list"),
            reverse("members:create"),
            reverse("members:detail", args=[m.pk]),
            reverse("members:edit", args=[m.pk]),
            reverse("members:anonymise", args=[m.pk]),
        ]
        for url in urls:
            self.assertEqual(self.client.get(url).status_code, 302, url)
        r = self.client.post(reverse("members:action", args=[m.pk, "archive"]))
        self.assertEqual(r.status_code, 302)
        m.refresh_from_db()
        self.assertEqual(m.status, "active")

    def test_create_flow_and_duplicate_keeps_entered_data(self):
        self.client.force_login(self.owner)
        data = {
            "full_name": "Asha Rao",
            "region": "IN",
            "national_number": "9876543210",
            "email": "",
            "joined_on": "2026-11-11",
            "notes": "hello",
        }
        r = self.client.post(reverse("members:create"), data)
        self.assertEqual(r.status_code, 302)
        r = self.client.post(reverse("members:create"), {**data, "full_name": "Second"})
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "already exists")
        self.assertContains(r, 'value="Second"')
        self.assertContains(r, "hello")
        self.assertEqual(Member.objects.count(), 1)

    def test_list_pagination_and_empty_state(self):
        self.client.force_login(self.owner)
        self.assertContains(self.client.get(reverse("members:list")), "No members found")
        for i in range(30):
            make(self.owner, f"M{i}", f"98765{40000 + i}")
        r = self.client.get(reverse("members:list"))
        self.assertContains(r, "Page 1 of 2")
        self.assertEqual(len(r.context["page"]), 25)

    def test_anonymise_page_discloses_backup_behaviour(self):
        self.client.force_login(self.owner)
        m = make(self.owner)
        r = self.client.get(reverse("members:anonymise", args=[m.pk]))
        self.assertContains(r, "Backups made before anonymisation")
        r = self.client.post(
            reverse("members:anonymise", args=[m.pk]), {"reason": "Requested", "confirm": "on"}
        )
        self.assertEqual(r.status_code, 302)
        m.refresh_from_db()
        self.assertIsNotNone(m.anonymised_at)

    def test_reactivate_choice_via_view(self):
        self.client.force_login(self.owner)
        m = make(self.owner)
        self.client.post(reverse("members:action", args=[m.pk, "archive"]))
        self.client.post(
            reverse("members:action", args=[m.pk, "reactivate"]), {"mode": "new_subscription"}
        )
        m.refresh_from_db()
        self.assertEqual((m.status, m.needs_new_subscription), ("active", True))
