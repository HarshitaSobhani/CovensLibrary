from unittest import mock

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError, transaction
from django.test import TestCase

from audit.models import AuditEvent


class SingleOwnerTests(TestCase):
    """L7: the database refuses a second account; create_owner is the supported way in."""

    def test_second_account_is_rejected_by_the_database(self):
        User = get_user_model()
        User.objects.create_user("owner", password="Correct-Horse-9!")
        with self.assertRaises(IntegrityError), transaction.atomic():
            User.objects.create_user("intruder", password="Correct-Horse-9!")
        with self.assertRaises(IntegrityError), transaction.atomic():
            User.objects.create_superuser("root", password="Correct-Horse-9!")
        self.assertEqual(User.objects.count(), 1)

    def test_create_owner_creates_one_account_and_audits(self):
        with mock.patch(
            "accounts.management.commands.create_owner.getpass", return_value="Correct-Horse-9!"
        ):
            call_command("create_owner", "--username", "owner", "--email", "o@example.com")
        user = get_user_model().objects.get()
        self.assertEqual(
            (user.username, user.email, user.is_staff, user.is_superuser),
            ("owner", "o@example.com", False, False),
        )
        self.assertTrue(user.check_password("Correct-Horse-9!"))
        ev = AuditEvent.objects.get(action="owner.create")
        self.assertNotIn("Correct-Horse", str(ev.summary) + ev.entity_id)

    def test_create_owner_refuses_when_an_account_exists(self):
        get_user_model().objects.create_user("owner", password="Correct-Horse-9!")
        with mock.patch(
            "accounts.management.commands.create_owner.getpass", return_value="Correct-Horse-9!"
        ):
            with self.assertRaises(CommandError) as ctx:
                call_command("create_owner", "--username", "second", "--email", "s@example.com")
        self.assertIn("already exists", str(ctx.exception))
        self.assertEqual(get_user_model().objects.count(), 1)

    def test_create_owner_rejects_mismatched_and_weak_passwords(self):
        answers = iter(["Correct-Horse-9!", "different"])
        with mock.patch(
            "accounts.management.commands.create_owner.getpass",
            side_effect=lambda *_: next(answers),
        ):
            with self.assertRaises(CommandError):
                call_command("create_owner", "--username", "o", "--email", "o@example.com")
        with mock.patch("accounts.management.commands.create_owner.getpass", return_value="123"):
            with self.assertRaises(CommandError):
                call_command("create_owner", "--username", "o", "--email", "o@example.com")
        self.assertEqual(get_user_model().objects.count(), 0)
