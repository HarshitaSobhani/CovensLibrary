"""Commands, the daily job step, the dashboard and the settings screen."""

from datetime import time, timedelta
from io import StringIO
from pathlib import Path

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from audit.models import AuditEvent
from backups import crypto, keystore, services
from backups.models import BackupRun
from circulation.tests.helpers import make_owner
from core import dashboard
from core.models import LibrarySetting
from memberships.tests.helpers import PW

from .helpers import TempDirMixin, fresh_key, ist
from .test_schedule_retention import make_run

S = BackupRun.Status


def command(name, *args):
    out = StringIO()
    call_command(name, *args, stdout=out)
    return out.getvalue()


class KeyCommandTests(TestCase):
    def setUp(self):
        keystore.reset_memory_keystore()
        self.addCleanup(keystore.reset_memory_keystore)

    def test_setup_creates_the_key_prints_the_recovery_key_once_and_audits_only_the_fingerprint(
        self,
    ):
        out = command("setup_backup_key", "--yes")
        key = keystore.load_working_key()
        self.assertEqual(len(key), 32)
        self.assertIn(keystore.recovery_key_text(key), out)
        self.assertEqual(keystore.parse_recovery_key(out.split("\n")[1]), key)
        event = AuditEvent.objects.get(action="backup.key_created")
        self.assertEqual(event.summary["fingerprint"], crypto.fingerprint(key))
        self.assertNotIn(key.hex(), str(event.summary))
        self.assertNotIn(keystore.recovery_key_text(key), str(event.summary))

    def test_the_key_is_never_replaced(self):
        command("setup_backup_key", "--yes")
        first = keystore.load_working_key()
        with self.assertRaisesRegex(CommandError, "already exists"):
            command("setup_backup_key", "--yes")
        self.assertEqual(keystore.load_working_key(), first)

    def test_show_recovery_key_needs_a_key_and_is_audited_without_the_value(self):
        with self.assertRaisesRegex(CommandError, "No backup key"):
            command("show_recovery_key", "--yes")
        command("setup_backup_key", "--yes")
        out = command("show_recovery_key", "--yes")
        self.assertEqual(out.strip(), keystore.recovery_key_text(keystore.load_working_key()))
        event = AuditEvent.objects.get(action="backup.recovery_key_shown")
        self.assertNotIn(out.strip(), str(event.summary))

    def test_a_wrong_typed_confirmation_cancels(self):
        from unittest import mock

        with mock.patch("builtins.input", return_value="nope"):
            with self.assertRaisesRegex(CommandError, "Cancelled"):
                command("setup_backup_key")
        self.assertIsNone(keystore.load_working_key())


class CommandTests(TempDirMixin, TestCase):
    def setUp(self):
        self.make_dirs()
        self.key = fresh_key()
        self.addCleanup(keystore.reset_memory_keystore)

    def test_run_backup_command_succeeds_and_reports_verification(self):
        out = command("run_backup", f"--destination=local:{self.dest_dir}")
        self.assertIn("Backup succeeded", out)
        self.assertIn("verified", out)
        self.assertEqual(len(list(Path(self.dest_dir).glob("*.cbk"))), 1)

    def test_run_backup_command_fails_loudly(self):
        keystore.reset_memory_keystore()
        with self.assertRaisesRegex(CommandError, "no backup key"):
            command("run_backup", f"--destination=local:{self.dest_dir}")
        with self.assertRaisesRegex(
            Exception, "BACKUP_DESTINATION|drive"
        ):  # a bad destination spec
            command("run_backup", "--destination=nowhere")

    def test_status_and_retention_commands(self):
        out = command("backup_status")
        self.assertIn("No backup has ever succeeded.", out)
        self.assertIn("Backup due now: yes", out)
        command("run_backup", f"--destination=local:{self.dest_dir}")
        out = command("backup_status")
        self.assertIn("Last successful backup", out)
        self.assertIn("Backup due now: no", out)
        self.assertIn("Retention:", command("apply_backup_retention", "--dry-run"))

    def test_the_daily_job_runs_the_backup_step_once(self):
        with override_settings(BACKUP_DESTINATION=f"local:{self.dest_dir}"):
            first = command("run_daily_jobs")
            second = command("run_daily_jobs")
        self.assertRegex(first, r"Backup \((scheduled|catchup)\): succeeded")
        self.assertIn("Backup: not due.", second)
        self.assertEqual(BackupRun.objects.filter(status=S.SUCCEEDED).count(), 1)

    def test_a_failing_backup_does_not_stop_the_daily_job(self):
        keystore.reset_memory_keystore()
        with override_settings(BACKUP_DESTINATION=f"local:{self.dest_dir}"):
            out = command("run_daily_jobs")
        self.assertIn("Notifications:", out)  # the earlier steps ran
        self.assertRegex(out, r"Backup \(\w+\): failed - no backup key")


class DashboardTests(TestCase):
    def setUp(self):
        self.owner = make_owner()
        self.client.login(username="owner", password=PW)

    def section(self, now=None):
        return dashboard.backup_failures(now)

    def test_never_succeeded_is_shown_as_a_problem(self):
        data = self.section()
        self.assertTrue(data["available"])
        self.assertEqual(data["count"], 1)
        page = self.client.get(reverse("core:dashboard")).content.decode()
        self.assertIn("No backup has ever succeeded.", page)
        self.assertIn("Backup failures", page)

    def test_a_healthy_backup_shows_the_last_success_and_no_problem(self):
        run = make_run(timezone.now() - timedelta(hours=2), destination="drive")
        data = self.section()
        self.assertEqual(data["count"], 0)
        page = self.client.get(reverse("core:dashboard")).content.decode()
        self.assertIn("Last successful backup", page)
        self.assertIn("verified", page)
        self.assertIn("No backup problems.", page)
        self.assertNotIn("over 26 hours", page)
        del run

    def test_stale_and_failed_runs_are_listed(self):
        make_run(timezone.now() - timedelta(hours=30))
        make_run(
            timezone.now() - timedelta(hours=1),
            status=S.FAILED,
            error_summary="upload failed: network",
        )
        data = self.section()
        self.assertEqual(data["count"], 2)  # one staleness problem + one failed run
        page = self.client.get(reverse("core:dashboard")).content.decode()
        self.assertIn("over 26 hours", page)
        self.assertIn("upload failed: network", page)

    def test_the_section_can_be_hidden_like_any_other(self):
        setting = LibrarySetting.load()
        setting.dashboard_config = {"visible": {"backup_failures": False}}
        setting.save()
        self.assertNotIn(
            "Backup failures", self.client.get(reverse("core:dashboard")).content.decode()
        )

    def test_no_secret_or_path_appears_on_the_dashboard(self):
        make_run(
            timezone.now() - timedelta(hours=1), status=S.FAILED, error_summary="pg_dump failed: x"
        )
        page = self.client.get(reverse("core:dashboard")).content.decode()
        self.assertNotIn(keystore.WORKING_KEY, page)


class SettingsTests(TestCase):
    def setUp(self):
        make_owner()
        self.client.login(username="owner", password=PW)
        self.url = reverse("core:settings")

    def test_the_default_and_the_form(self):
        page = self.client.get(self.url).content.decode()
        self.assertIn("Daily backup", page)
        self.assertIn('value="19:45"', page)

    def test_the_owner_can_change_the_time_and_it_is_audited(self):
        self.client.post(self.url, {"action": "backup_time", "backup_time": "07:30"})
        self.assertEqual(LibrarySetting.load().backup_time, time(7, 30))
        event = AuditEvent.objects.get(action="backup.time_changed")
        self.assertEqual(event.summary, {"from": "19:45", "to": "07:30"})
        self.client.post(self.url, {"action": "backup_time", "backup_time": "07:30"})
        self.assertEqual(AuditEvent.objects.filter(action="backup.time_changed").count(), 1)

    def test_invalid_times_are_refused_and_nothing_changes(self):
        for bad in ("", "25:00", "19:75", "7pm", "19", "19:45:00", "ab:cd", "-1:30"):
            response = self.client.post(
                self.url, {"action": "backup_time", "backup_time": bad}, follow=True
            )
            self.assertContains(response, "HH:MM")
            self.assertEqual(LibrarySetting.load().backup_time, time(19, 45), bad)

    def test_login_is_required(self):
        self.client.logout()
        self.assertEqual(
            self.client.post(
                self.url, {"action": "backup_time", "backup_time": "07:30"}
            ).status_code,
            302,
        )
        self.assertEqual(LibrarySetting.load().backup_time, time(19, 45))


class CatchUpEndToEndTests(TempDirMixin, TestCase):
    """A Dell that was off at 19:45 backs up at the next launch, with the right trigger, and the
    daily job then stays quiet until the next slot."""

    def setUp(self):
        self.make_dirs()
        fresh_key()
        self.addCleanup(keystore.reset_memory_keystore)

    def test_off_at_the_slot_then_launched_the_next_morning(self):
        services.run_due_backup(ist(2027, 3, 1, 19, 46), self.destination)  # day 1: on time
        # day 2: the computer was off at 19:45 and is switched on at 08:00 on day 3
        run = services.run_due_backup(ist(2027, 3, 3, 8, 0), self.destination)
        self.assertEqual((run.status, run.trigger), (S.SUCCEEDED, "catchup"))
        self.assertIsNone(services.run_due_backup(ist(2027, 3, 3, 8, 30), self.destination))
        nxt = services.run_due_backup(ist(2027, 3, 3, 19, 45), self.destination)
        self.assertEqual(nxt.trigger, "scheduled")
        self.assertEqual(BackupRun.objects.filter(status=S.SUCCEEDED).count(), 3)
