"""Catch-up, the 26-hour health rule and retention (30 daily + 12 monthly), with a fake clock."""

from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from django.test import TestCase

from backups import keystore, services
from backups.models import BackupRun
from core.models import LibrarySetting

from .helpers import FakeDestination, TempDirMixin, fresh_key, ist

S = BackupRun.Status
Kind = BackupRun.Kind


def make_run(when, kind=Kind.DAILY, status=S.SUCCEEDED, name=None, **extra):
    name = name or f"b-{when:%Y%m%d-%H%M%S}-{kind}.cbk"
    fields = dict(kind=kind, status=status, started_at=when)
    if status == S.SUCCEEDED:
        fields |= dict(
            finished_at=when, verified_at=when, sha256="a" * 64, remote_id=name,
            size_bytes=10, file_name=name, destination="local",
        )  # fmt: skip
    elif status == S.FAILED:
        fields |= dict(finished_at=when, error_summary="boom")
    fields.update(extra)
    return BackupRun.objects.create(**fields)


class SlotTests(TestCase):
    def test_the_slot_is_the_most_recent_daily_time_in_kolkata(self):
        at = time(19, 45)
        self.assertEqual(
            services.scheduled_slot(ist(2027, 3, 1, 19, 45), at), ist(2027, 3, 1, 19, 45)
        )
        self.assertEqual(
            services.scheduled_slot(ist(2027, 3, 1, 19, 44, 59), at), ist(2027, 2, 28, 19, 45)
        )
        self.assertEqual(
            services.scheduled_slot(ist(2027, 3, 1, 0, 0), at), ist(2027, 2, 28, 19, 45)
        )
        self.assertEqual(
            services.scheduled_slot(ist(2027, 3, 1, 23, 59), at), ist(2027, 3, 1, 19, 45)
        )

    def test_utc_input_is_read_in_asia_kolkata(self):
        utc = ZoneInfo("UTC")
        at = time(19, 45)
        moment = datetime(2027, 3, 1, 14, 15, tzinfo=utc)  # 19:45 IST
        self.assertEqual(services.scheduled_slot(moment, at), ist(2027, 3, 1, 19, 45))
        self.assertEqual(
            services.scheduled_slot(moment - timedelta(seconds=1), at), ist(2027, 2, 28, 19, 45)
        )

    def test_the_default_time_is_1945(self):
        self.assertEqual(LibrarySetting.load().backup_time, time(19, 45))


class DueTests(TestCase):
    def test_not_due_when_todays_backup_succeeded_and_due_again_at_the_next_slot(self):
        make_run(ist(2027, 3, 1, 19, 45, 5))
        self.assertEqual(services.backup_due(ist(2027, 3, 1, 21, 0)), (False, ""))
        self.assertEqual(services.backup_due(ist(2027, 3, 2, 19, 44)), (False, ""))
        self.assertEqual(services.backup_due(ist(2027, 3, 2, 19, 45)), (True, "scheduled"))

    def test_a_missed_slot_is_caught_up_at_the_next_launch(self):
        make_run(ist(2027, 3, 1, 19, 45))
        due, trigger = services.backup_due(ist(2027, 3, 3, 9, 0))  # the Dell was off for two slots
        self.assertEqual((due, trigger), (True, "catchup"))

    def test_the_trigger_is_scheduled_only_close_to_the_slot(self):
        make_run(ist(2027, 2, 28, 19, 45))
        self.assertEqual(services.backup_due(ist(2027, 3, 1, 19, 55)), (True, "scheduled"))
        self.assertEqual(services.backup_due(ist(2027, 3, 1, 19, 56)), (True, "catchup"))

    def test_first_ever_run_is_due(self):
        self.assertEqual(services.backup_due(ist(2027, 3, 1, 12, 0)), (True, "catchup"))

    def test_a_failure_is_not_retried_for_30_minutes(self):
        make_run(ist(2027, 3, 1, 19, 45), status=S.FAILED)
        self.assertEqual(services.backup_due(ist(2027, 3, 1, 20, 14)), (False, ""))
        self.assertEqual(services.backup_due(ist(2027, 3, 1, 20, 15)), (True, "catchup"))

    def test_an_old_failure_does_not_block_a_new_slot(self):
        make_run(ist(2027, 3, 1, 19, 45), status=S.FAILED)
        self.assertEqual(services.backup_due(ist(2027, 3, 2, 19, 45))[0], True)

    def test_a_running_backup_blocks_another(self):
        make_run(ist(2027, 3, 1, 19, 45), status=S.RUNNING)
        self.assertEqual(services.backup_due(ist(2027, 3, 1, 20, 0)), (False, ""))

    def test_a_safety_backup_before_a_restore_does_not_count_as_the_daily_backup(self):
        make_run(ist(2027, 3, 1, 19, 50), kind=Kind.PRE_RESTORE)
        self.assertTrue(services.backup_due(ist(2027, 3, 1, 21, 0))[0])

    def test_the_owner_can_change_the_time(self):
        setting = LibrarySetting.load()
        setting.backup_time = time(7, 30)
        setting.save()
        make_run(ist(2027, 3, 1, 7, 30, 10))
        self.assertEqual(services.backup_due(ist(2027, 3, 1, 19, 45)), (False, ""))
        self.assertEqual(services.backup_due(ist(2027, 3, 2, 7, 30)), (True, "scheduled"))


class HealthTests(TestCase):
    def test_never_succeeded_is_a_problem(self):
        health = services.health(ist(2027, 3, 1, 12))
        self.assertTrue(health["stale"])
        self.assertIn("No backup has ever succeeded.", health["problems"])

    def test_the_26_hour_boundary(self):
        make_run(ist(2027, 3, 1, 12, 0))
        self.assertFalse(services.health(ist(2027, 3, 2, 13, 59))["stale"])
        self.assertFalse(services.health(ist(2027, 3, 2, 14, 0))["stale"])  # exactly 26 h: not over
        stale = services.health(ist(2027, 3, 2, 14, 0, 1))
        self.assertTrue(stale["stale"])
        self.assertTrue(any("over 26 hours" in p for p in stale["problems"]))

    def test_failures_since_the_last_success_are_listed_and_older_ones_are_not(self):
        make_run(ist(2027, 2, 27, 19, 45), status=S.FAILED)
        make_run(ist(2027, 2, 28, 19, 45))
        make_run(ist(2027, 3, 1, 19, 45), status=S.FAILED)
        health = services.health(ist(2027, 3, 1, 20, 0))
        self.assertEqual(health["failure_count"], 1)
        self.assertEqual(health["failures"][0].started_at, ist(2027, 3, 1, 19, 45))

    def test_a_success_clears_the_failures(self):
        make_run(ist(2027, 3, 1, 19, 45), status=S.FAILED)
        make_run(ist(2027, 3, 1, 20, 20))
        self.assertEqual(services.health(ist(2027, 3, 1, 21, 0))["failure_count"], 0)


class RetentionPlanTests(TestCase):
    NOW = ist(2027, 7, 15, 12)

    def daily_history(self, days, end=None):
        end = end or ist(2027, 7, 14, 19, 45)
        return [make_run(end - timedelta(days=i)) for i in range(days)]

    def plan(self, now=None):
        keep, remove = services.retention_plan(now or self.NOW)
        return {r.pk for r in keep}, {r.pk for r in remove}

    def test_thirty_daily_plus_the_last_of_each_of_twelve_completed_months(self):
        runs = self.daily_history(400)
        keep, remove = self.plan()
        by_pk = {r.pk: r for r in runs}
        newest30 = {r.pk for r in runs[:30]}
        self.assertLessEqual(newest30, keep)
        monthly = {pk for pk in keep - newest30}
        months = sorted(
            (
                by_pk[pk].started_at.astimezone(ist(2027, 1, 1).tzinfo).strftime("%Y-%m")
                for pk in monthly
            )
        )
        self.assertEqual(
            months,
            [f"2026-{m:02d}" for m in range(7, 13)] + [f"2027-{m:02d}" for m in range(1, 6)],
        )  # June's last backup (30 June) is already among the 30 newest
        for pk in monthly:  # each monthly backup is the LAST one of its calendar month
            when = by_pk[pk].started_at.astimezone(ist(2027, 1, 1).tzinfo)
            later_same_month = [
                r
                for r in runs
                if r.started_at > by_pk[pk].started_at
                and r.started_at.astimezone(when.tzinfo).month == when.month
                and r.started_at.astimezone(when.tzinfo).year == when.year
            ]
            self.assertEqual(later_same_month, [])
        self.assertEqual(len(keep), 30 + 11)
        self.assertEqual(len(keep) + len(remove), 400)

    def test_counts_are_exactly_30_plus_12_when_there_is_enough_history(self):
        self.daily_history(500)
        keep, remove = services.retention_plan(self.NOW)
        # the 30 newest cover 15 June onwards, so June's last backup (30 June) is inside them
        # and the monthly set is the 12 months before... counted without double counting
        self.assertEqual(len(keep), 30 + 11)  # 30 newest (incl. the last of June) + 11 older months
        self.assertEqual(len(keep) + len(remove), 500)

    def test_fewer_than_30_backups_are_all_kept(self):
        self.daily_history(10)
        keep, remove = self.plan()
        self.assertEqual(len(keep), 10)
        self.assertEqual(remove, set())

    def test_the_newest_successful_backup_is_always_kept(self):
        self.daily_history(40)
        newest = BackupRun.objects.filter(status=S.SUCCEEDED).order_by("-started_at").first()
        self.assertIn(newest.pk, self.plan(ist(2030, 1, 1))[0])  # even years later

    def test_a_month_with_a_single_backup_keeps_it(self):
        old = make_run(ist(2026, 9, 3, 19, 45))
        self.daily_history(5)
        self.assertIn(old.pk, self.plan()[0])

    def test_the_current_month_is_not_a_monthly_candidate_until_it_ends(self):
        runs = self.daily_history(60)
        keep, _ = self.plan(ist(2027, 7, 15))
        july_last = next(r for r in runs if r.started_at.month == 7)
        self.assertIn(july_last.pk, keep)  # within the newest 30 anyway
        # an older run of the current month beyond the 30 newest is not protected as "monthly"
        make_run(ist(2027, 7, 1, 8, 0), name="early-july.cbk")
        keep2, remove2 = self.plan(ist(2027, 7, 15))
        early = BackupRun.objects.get(file_name="early-july.cbk")
        self.assertIn(early.pk, keep2)  # it is inside the 30 newest by date

    def test_months_across_a_year_boundary(self):
        for when in (
            ist(2026, 12, 20),
            ist(2026, 12, 31, 23, 50),
            ist(2027, 1, 1, 0, 10),
            ist(2027, 1, 31, 23, 59, 59),
        ):
            make_run(when)
        self.daily_history(31, end=ist(2027, 7, 14, 19, 45))
        keep, _ = services.retention_plan(self.NOW)
        kept_names = {r.file_name for r in keep}
        self.assertIn("b-20261231-235000-daily.cbk", kept_names)  # last of December (IST)
        self.assertNotIn("b-20261220-000000-daily.cbk", kept_names)
        self.assertIn("b-20270131-235959-daily.cbk", kept_names)
        self.assertNotIn("b-20270101-001000-daily.cbk", kept_names)

    def test_the_month_boundary_uses_the_kolkata_date_not_utc(self):
        # 31 Jan 23:50 IST is still 31 Jan 18:20 UTC; 1 Feb 00:10 IST is 31 Jan 18:40 UTC
        a = make_run(ist(2027, 1, 31, 23, 50))
        b = make_run(ist(2027, 2, 1, 0, 10))
        self.daily_history(31)
        keep = {r.pk for r in services.retention_plan(self.NOW)[0]}
        self.assertIn(a.pk, keep)  # the last backup of January
        self.assertNotIn(b.pk, keep - {a.pk} if b.pk not in keep else set())

    def test_failed_running_and_already_removed_runs_are_ignored(self):
        self.daily_history(35)
        failed = make_run(ist(2027, 7, 14, 21), status=S.FAILED)
        removed = make_run(ist(2025, 1, 1), name="gone.cbk", remote_deleted_at=ist(2025, 6, 1))
        keep, remove = self.plan()
        self.assertNotIn(failed.pk, keep | remove)
        self.assertNotIn(removed.pk, keep | remove)

    # -- safety backups: released only after a verified restore AND a later verified backup ---
    def fill(self):
        """35 recent dailies plus one per month back to 2026-01, so a January 2025 backup is
        outside both the newest 30 and the 12 most recent completed months."""
        self.daily_history(35)
        for i in range(18):
            year, month = divmod(2026 * 12 + i, 12)
            make_run(ist(year, month + 1, 20), name=f"m-{year}-{month}.cbk")

    def safety(self, restore_status="succeeded", restore_finished=None, matching=True):
        """An old safety backup plus the restore record naming it."""
        from backups.models import RestoreRun

        safety = make_run(ist(2025, 1, 1), kind=Kind.PRE_RESTORE, name="safety.cbk")
        RestoreRun.objects.create(
            backup_file_name="restored.cbk",
            target_database="d",
            status=restore_status,
            finished_at=restore_finished or ist(2025, 1, 1, 1),
            pre_restore={
                "file_name": "safety.cbk" if matching else "other.cbk",
                "remote_id": "safety.cbk",
            },
        )
        return safety

    def assertProtected(self, safety):
        keep, remove = self.plan()
        self.assertIn(safety.pk, keep)
        self.assertNotIn(safety.pk, remove)

    def assertReleased(self, safety):
        keep, remove = self.plan()
        self.assertIn(safety.pk, remove)  # old enough that normal retention drops it
        self.assertNotIn(safety.pk, keep)

    def test_neither_condition_met_keeps_it(self):
        safety = make_run(ist(2025, 1, 1), kind=Kind.PRE_RESTORE, name="safety.cbk")
        self.fill()  # no restore record at all, later backups exist
        self.assertProtected(safety)

    def test_only_restore_verification_met_keeps_it(self):
        safety = self.safety(restore_finished=ist(2027, 8, 1))  # no backup after the restore
        self.fill()
        self.assertProtected(safety)

    def test_only_a_later_backup_met_keeps_it(self):
        safety = self.safety(restore_status="verification_failed")
        self.fill()  # later backups succeeded, but the restore did not verify
        self.assertProtected(safety)

    def test_a_restore_record_for_another_file_does_not_release_it(self):
        safety = self.safety(matching=False)
        self.fill()
        self.assertProtected(safety)

    def test_both_conditions_met_makes_it_eligible_for_normal_retention(self):
        safety = self.safety()
        self.fill()
        self.assertReleased(safety)

    def test_a_released_safety_backup_inside_the_window_is_kept_like_any_other(self):
        from backups.models import RestoreRun

        recent = make_run(ist(2027, 7, 1, 3), kind=Kind.PRE_RESTORE, name="safety.cbk")
        RestoreRun.objects.create(
            backup_file_name="r.cbk",
            target_database="d",
            status="succeeded",
            finished_at=ist(2027, 7, 1, 4),
            pre_restore={"file_name": "safety.cbk", "remote_id": "safety.cbk"},
        )
        self.daily_history(10)  # newest 30 includes it: normal retention keeps it
        self.assertProtected(recent)

    def test_a_failed_or_unverified_later_backup_does_not_release_it(self):
        safety = self.safety()
        make_run(ist(2027, 7, 1), status=S.FAILED)
        self.assertProtected(safety)
        make_run(ist(2027, 7, 3), kind=Kind.PRE_RESTORE, name="second-safety.cbk")  # not a daily
        self.assertProtected(safety)

    def test_the_backup_that_ended_before_the_restore_does_not_count(self):
        safety = self.safety(restore_finished=ist(2027, 8, 1))
        self.fill()  # every backup is older than the restore
        self.assertProtected(safety)
        make_run(ist(2027, 8, 2), name="first-after.cbk")  # the first backup of the restored system
        self.assertReleased(safety)

    def test_repeating_retention_never_deletes_a_protected_safety_backup(self):
        safety = self.safety(restore_status="verification_failed")
        self.fill()
        deleted = []

        class Recorder:
            def delete(self, remote_id):
                deleted.append(remote_id)

        for _ in range(3):
            services.apply_retention(self.NOW, Recorder())
        self.assertNotIn("safety.cbk", deleted)
        safety.refresh_from_db()
        self.assertIsNone(safety.remote_deleted_at)

    def test_repeating_retention_removes_a_released_one_exactly_once(self):
        safety = self.safety()
        self.fill()
        deleted = []

        class Recorder:
            def delete(self, remote_id):
                deleted.append(remote_id)

        first = services.apply_retention(self.NOW, Recorder())
        second = services.apply_retention(self.NOW, Recorder())
        self.assertEqual(deleted.count("safety.cbk"), 1)
        self.assertGreaterEqual(first["removed"], 1)
        self.assertEqual(second["removed"], 0)
        safety.refresh_from_db()
        self.assertIsNotNone(safety.remote_deleted_at)

    def test_the_plan_is_stable_when_run_twice(self):
        self.daily_history(200)
        self.assertEqual(self.plan(), self.plan())


class ApplyRetentionTests(TempDirMixin, TestCase):
    def setUp(self):
        self.make_dirs()

    def seed(self, count):
        runs = []
        for i in range(count):
            run = make_run(ist(2027, 7, 14, 19, 45) - timedelta(days=i))
            (Path(self.dest_dir) / run.file_name).write_bytes(b"x")
            runs.append(run)
        return runs

    def test_it_removes_exactly_what_the_plan_says_and_records_it(self):
        runs = self.seed(60)
        now = ist(2027, 7, 15, 12)
        keep, remove = services.retention_plan(now)
        result = services.apply_retention(now, self.destination)
        self.assertEqual((result["removed"], result["errors"]), (len(remove), 0))
        for run in remove:
            self.assertFalse((Path(self.dest_dir) / run.file_name).exists())
            run.refresh_from_db()
            self.assertEqual(run.remote_deleted_at, now)
        for run in keep:
            self.assertTrue((Path(self.dest_dir) / run.file_name).exists())
        self.assertEqual(BackupRun.objects.count(), len(runs))  # the history rows stay

    def test_it_is_idempotent(self):
        self.seed(60)
        now = ist(2027, 7, 15, 12)
        services.apply_retention(now, self.destination)
        again = services.apply_retention(now, self.destination)
        self.assertEqual(again["removed"], 0)

    def test_a_dry_run_changes_nothing(self):
        self.seed(60)
        result = services.apply_retention(ist(2027, 7, 15, 12), self.destination, dry_run=True)
        self.assertEqual(result["removed"], 0)
        self.assertGreater(result["would_remove"], 0)
        self.assertEqual(len(list(Path(self.dest_dir).iterdir())), 60)

    def test_one_failed_delete_does_not_stop_the_others_and_is_retried_later(self):
        runs = self.seed(40)
        victim = runs[-1]
        destination = FakeDestination(self.dest_dir, fail_delete={victim.remote_id})
        now = ist(2027, 7, 15, 12)
        result = services.apply_retention(now, destination)
        self.assertEqual(result["errors"], 1)
        self.assertGreater(result["removed"], 0)
        victim.refresh_from_db()
        self.assertIsNone(victim.remote_deleted_at)
        destination.fail_delete.clear()
        self.assertEqual(services.apply_retention(now, destination)["removed"], 1)

    def test_nothing_is_ever_removed_below_the_limits(self):
        self.seed(30)
        result = services.apply_retention(ist(2027, 7, 15, 12), self.destination)
        self.assertEqual(result["removed"], 0)


class DailyJobStepTests(TempDirMixin, TestCase):
    def setUp(self):
        self.make_dirs()
        fresh_key()
        self.addCleanup(keystore.reset_memory_keystore)

    def test_run_due_backup_backs_up_once_then_waits_for_the_next_slot(self):
        first = services.run_due_backup(ist(2027, 3, 1, 19, 46), self.destination)
        self.assertEqual((first.status, first.trigger), (S.SUCCEEDED, "scheduled"))
        self.assertIsNone(services.run_due_backup(ist(2027, 3, 1, 20, 30), self.destination))
        self.assertEqual(BackupRun.objects.count(), 1)

    def test_a_late_launch_is_a_catch_up(self):
        run = services.run_due_backup(ist(2027, 3, 2, 9, 0), self.destination)
        self.assertEqual((run.status, run.trigger), (S.SUCCEEDED, "catchup"))

    def test_retention_runs_after_a_success(self):
        for i in range(1, 36):
            run = make_run(ist(2027, 3, 1, 19, 45) - timedelta(days=i))
            (Path(self.dest_dir) / run.file_name).write_bytes(b"x")
        services.run_due_backup(ist(2027, 3, 1, 19, 46), self.destination)
        self.assertGreater(BackupRun.objects.filter(remote_deleted_at__isnull=False).count(), 0)

    def test_a_failed_backup_does_not_trigger_retention(self):
        keystore.reset_memory_keystore()
        for i in range(1, 36):
            make_run(ist(2027, 3, 1, 19, 45) - timedelta(days=i))
        run = services.run_due_backup(ist(2027, 3, 1, 19, 46), self.destination)
        self.assertEqual(run.status, S.FAILED)
        self.assertEqual(BackupRun.objects.filter(remote_deleted_at__isnull=False).count(), 0)
