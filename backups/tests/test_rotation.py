"""Backup-key rotation (decision S-04): every historical backup stays openable, a rotation survives
an interruption at every step, nothing is ever replaced or deleted silently, and no key material
reaches the audit log, the history tables or the screen."""

import io
from datetime import timedelta
from pathlib import Path
from unittest import mock

import psycopg
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test import TestCase
from django.utils import timezone

from audit.models import AuditEvent
from backups import crypto, keystore, locks, restore, rotation, services
from backups.destinations import LocalDirectoryDestination
from backups.models import BackupRun
from backups.tests.helpers import FakeDestination, TempDirMixin, fresh_key, query
from backups.tests.test_restore import RehearsalBase, seed_library
from core.management.commands.launcher_preflight import Command as Preflight

S = BackupRun.Status


def fp(key):
    return crypto.fingerprint(key)


class RotationBase(TempDirMixin, TestCase):
    def setUp(self):
        self.make_dirs()
        self.old = fresh_key()
        self.addCleanup(keystore.reset_memory_keystore)
        self.store = keystore.get_keystore()
        self.first = self.backup()  # a verified backup made with the old key

    def backup(self, **kw):
        run = services.run_backup(destination=self.destination, **kw)
        self.assertEqual(run.status, S.SUCCEEDED, run.error_summary)
        return run

    def rotate(self, typed=None, **kw):
        shown = []

        def prompt(text):
            shown.append(text)
            return typed(text) if callable(typed) else (text if typed is None else typed)

        kw.setdefault("confirm", rotation.CONFIRM_ROTATE)
        kw.setdefault("old_recovery_text", keystore.recovery_key_text(self.old))
        result = rotation.rotate(
            new_recovery_prompt=prompt, store=self.store, destination=self.destination, **kw
        )
        return result, shown

    def secrets_in(self, *keys):
        """Texts that must never appear in any audit event or history row."""
        return [t for k in keys for t in (keystore.encode_key(k), keystore.recovery_key_text(k))]

    def everything_stored(self):
        parts = [str(e.summary) + e.action for e in AuditEvent.objects.all()]
        parts += [str(vars(r)) for r in BackupRun.objects.all()]
        return "\n".join(parts)


class PreflightTests(RotationBase):
    def run_preflight(self, **kw):
        kw.setdefault("old_recovery_text", keystore.recovery_key_text(self.old))
        return rotation.preflight(store=self.store, destination=self.destination, **kw)

    def test_a_healthy_setup_passes_and_changes_nothing(self):
        before = (dict(self.store.items), BackupRun.objects.count(), AuditEvent.objects.count())
        report = self.run_preflight()
        self.assertTrue(report.ok, [(c.name, c.detail) for c in report.failed()])
        self.assertEqual(
            (dict(self.store.items), BackupRun.objects.count(), AuditEvent.objects.count()), before
        )

    def test_each_unsafe_condition_is_reported(self):
        cases = {}
        keystore.reset_memory_keystore()
        cases["no key"] = self.run_preflight()
        keystore.save_working_key(self.old)
        with mock.patch.object(rotation, "FRESH_BACKUP", timedelta(0)):  # every backup is "old"
            cases["stale backup"] = self.run_preflight()
        (Path(self.dest_dir) / self.first.file_name).write_bytes(b"damaged")
        cases["damaged backup"] = self.run_preflight()
        for name, report in cases.items():
            self.assertFalse(report.ok, name)
        self.assertIn("current backup key", cases["no key"].failed()[0].name)
        self.assertTrue(any("26 hours" in c.name for c in cases["stale backup"].failed()))
        self.assertTrue(any("intact" in c.name for c in cases["damaged backup"].failed()))

    def test_a_wrong_or_mistyped_current_recovery_key_fails(self):
        for typed in (keystore.recovery_key_text(keystore.generate_key()), "not a key", ""):
            report = self.run_preflight(old_recovery_text=typed)
            self.assertFalse(report.ok)
            self.assertTrue(any("CURRENT recovery key" in c.name for c in report.failed()))

    def test_a_credential_store_that_cannot_hold_a_value_fails_the_preflight(self):
        with mock.patch.object(self.store, "set", side_effect=OSError("locked")):
            report = self.run_preflight()
        self.assertTrue(any("test value" in c.name for c in report.failed()))

    def test_an_unreadable_store_fails_closed(self):
        with mock.patch.object(self.store, "get", side_effect=keystore.KeyStoreError("locked")):
            report = self.run_preflight()
        self.assertFalse(report.ok)
        self.assertNotIn("locked-key", str(report.checks))

    def test_a_rotation_already_in_progress_blocks_a_new_one(self):
        keystore.save_slot(keystore.PENDING_KEY, keystore.generate_key(), self.store)
        self.assertTrue(any("already in progress" in c.name for c in self.run_preflight().failed()))

    def test_the_dry_run_command_changes_nothing_and_says_so(self):
        out = io.StringIO()
        before = (dict(self.store.items), AuditEvent.objects.count())
        call_command(
            "rotate_backup_key", "--dry-run", f"--destination=local:{self.dest_dir}", stdout=out
        )
        self.assertIn("Dry run: nothing was changed. Ready to rotate.", out.getvalue())
        self.assertEqual((dict(self.store.items), AuditEvent.objects.count()), before)
        with mock.patch.object(rotation, "FRESH_BACKUP", timedelta(0)):
            with self.assertRaises(CommandError):
                call_command(
                    "rotate_backup_key",
                    "--dry-run",
                    f"--destination=local:{self.dest_dir}",
                    stdout=out,
                )


class RotationTests(RotationBase):
    def test_a_rotation_moves_new_backups_to_the_new_key_and_keeps_every_old_one_openable(self):
        result, shown = self.rotate()
        new = keystore.load_working_key(self.store)
        self.assertNotEqual(new, self.old)
        self.assertEqual(result["new"], fp(new))
        # the old key is retired, not deleted; the pending slot is gone
        self.assertEqual(keystore.load_retired_key(fp(self.old), self.store), self.old)
        self.assertIsNone(keystore.load_pending_key(self.store))
        # a real, verified backup with the new key exists; the library's current key is the new one
        self.assertEqual(rotation.current_fingerprint(), fp(new))
        newest = BackupRun.objects.filter(key_fingerprint=fp(new), status=S.SUCCEEDED).get()
        # both generations open with the keys the store holds
        for run in (self.first, newest):
            key = keystore.find_key(run.key_fingerprint, self.store)
            self.assertIsNotNone(key, run.file_name)
            crypto.decrypt_file(Path(self.dest_dir) / run.file_name, None, key)
        # new backups from now on use the new key
        later = self.backup(now=timezone.now() + timedelta(minutes=5))  # a later second
        self.assertEqual(later.key_fingerprint, fp(new))
        self.assertEqual(len(shown), 1)  # the recovery key was shown exactly once

    def test_no_key_material_reaches_the_audit_log_or_the_history_tables(self):
        _, shown = self.rotate()
        new = keystore.load_working_key(self.store)
        stored = self.everything_stored()
        for text in self.secrets_in(self.old, new):
            self.assertNotIn(text, stored)
        self.assertIn("backup.key_rotation_started", stored)
        self.assertIn("backup.key_rotation_committed", stored)
        self.assertIn(fp(self.old), stored)
        self.assertIn(fp(new), stored)

    def test_the_old_recovery_key_still_opens_the_old_backups_without_the_store(self):
        self.rotate()
        keystore.reset_memory_keystore()  # a new computer: only the paper keys exist
        key = keystore.parse_recovery_key(keystore.recovery_key_text(self.old))
        crypto.decrypt_file(Path(self.dest_dir) / self.first.file_name, None, key)

    def test_nothing_happens_without_the_typed_confirmation(self):
        for confirm in ("", "rotate backup key", "YES", "ROTATE"):
            with self.assertRaises(rotation.RotationError):
                self.rotate(confirm=confirm)
        self.assertEqual(self.store.items.keys(), {keystore.WORKING_KEY})
        self.assertEqual(AuditEvent.objects.filter(action__startswith="backup.key_rot").count(), 0)

    def test_a_wrong_current_recovery_key_is_refused_before_anything_is_created(self):
        wrong = keystore.recovery_key_text(keystore.generate_key())
        with self.assertRaisesRegex(rotation.RotationError, "Preflight failed"):
            self.rotate(old_recovery_text=wrong)
        self.assertEqual(self.store.items.keys(), {keystore.WORKING_KEY})
        self.assertEqual(keystore.load_working_key(self.store), self.old)

    def test_a_stale_backup_blocks_the_rotation(self):
        with mock.patch.object(rotation, "FRESH_BACKUP", timedelta(0)):
            with self.assertRaisesRegex(rotation.RotationError, "26 hours"):
                self.rotate()
        self.assertIsNone(keystore.load_pending_key(self.store))

    def test_the_maintenance_lock_stops_a_second_operation(self):
        cfg = connection.settings_dict
        other = psycopg.connect(
            host=cfg["HOST"], port=cfg["PORT"], dbname=cfg["NAME"], user=cfg["USER"],
            password=cfg["PASSWORD"], autocommit=True,
        )  # fmt: skip
        self.addCleanup(other.close)
        other.execute("SELECT pg_advisory_lock(%s)", [locks.MAINTENANCE_LOCK])
        with self.assertRaisesRegex(rotation.RotationError, "Another maintenance"):
            self.rotate()
        self.assertEqual(self.store.items.keys(), {keystore.WORKING_KEY})
        other.execute("SELECT pg_advisory_unlock(%s)", [locks.MAINTENANCE_LOCK])
        self.rotate()  # and it works once the other operation is done


class InterruptionTests(RotationBase):
    """A crash at any step is recoverable; the working key only changes at the commit."""

    def test_the_new_recovery_key_typed_wrongly_leaves_a_resumable_rotation(self):
        with self.assertRaisesRegex(rotation.RotationError, "typed back correctly"):
            self.rotate(typed="WRONG-WRONG")
        self.assertEqual(keystore.load_working_key(self.store), self.old)  # unchanged
        pending = keystore.load_pending_key(self.store)
        self.assertIsNotNone(pending)
        self.assertEqual(rotation.state(self.store)["in_progress"], True)
        # a second start is refused while one is in progress; resume finishes it
        with self.assertRaisesRegex(rotation.RotationError, "Preflight failed"):
            self.rotate()
        result = rotation.resume(
            new_recovery_prompt=lambda text: text, store=self.store, destination=self.destination
        )
        self.assertEqual(keystore.load_working_key(self.store), pending)
        self.assertTrue(result["new"] == fp(pending))
        self.assertIsNone(keystore.load_pending_key(self.store))

    def crash_at(self, name):
        with mock.patch.object(rotation, name, side_effect=RuntimeError("power cut")):
            with self.assertRaises(RuntimeError):
                self.rotate()

    def finish(self):
        return rotation.resume(
            new_recovery_prompt=lambda text: text, store=self.store, destination=self.destination
        )

    def test_a_crash_while_proving_the_old_key_is_resumed(self):
        self.crash_at("_prove_old_key")
        self.assertEqual(keystore.load_working_key(self.store), self.old)
        self.assertEqual(keystore.load_retired_key(fp(self.old), self.store), self.old)  # kept
        self.finish()
        self.assertNotEqual(keystore.load_working_key(self.store), self.old)

    def test_a_crash_during_the_verification_backup_is_resumed_without_losing_the_old_key(self):
        self.crash_at("_verification_backup")
        self.assertEqual(keystore.load_working_key(self.store), self.old)
        self.assertIsNotNone(keystore.load_pending_key(self.store))
        self.finish()
        new = keystore.load_working_key(self.store)
        self.assertEqual(rotation.current_fingerprint(), fp(new))
        self.assertEqual(keystore.load_retired_key(fp(self.old), self.store), self.old)

    def test_a_crash_after_the_commit_is_finished_and_the_commit_is_recorded_once(self):
        real = keystore.delete_slot
        calls = {"n": 0}

        def flaky(name, store):
            if name == keystore.PENDING_KEY and calls["n"] == 0:
                calls["n"] += 1
                raise RuntimeError("power cut")
            return real(name, store)

        with mock.patch.object(keystore, "delete_slot", flaky):
            with self.assertRaises(RuntimeError):
                self.rotate()
        working, pending = (
            keystore.load_working_key(self.store),
            keystore.load_pending_key(self.store),
        )
        self.assertEqual(working, pending)  # committed, not finished
        self.assertTrue(rotation.state(self.store)["committed_not_finished"])
        self.finish()
        self.assertIsNone(keystore.load_pending_key(self.store))
        self.assertEqual(
            AuditEvent.objects.filter(action="backup.key_rotation_committed").count(), 1
        )
        with self.assertRaisesRegex(rotation.RotationError, "No rotation is in progress"):
            self.finish()

    def test_a_failed_verification_backup_commits_nothing(self):
        broken = FakeDestination(self.dest_dir, fail_upload=True)
        with self.assertRaisesRegex(rotation.RotationError, "verification backup"):
            rotation.rotate(
                confirm=rotation.CONFIRM_ROTATE,
                old_recovery_text=keystore.recovery_key_text(self.old),
                new_recovery_prompt=lambda text: text,
                store=self.store,
                destination=_PreflightPasses(broken, self.destination),
            )
        self.assertEqual(keystore.load_working_key(self.store), self.old)
        self.assertEqual(
            BackupRun.objects.filter(status=S.SUCCEEDED, key_fingerprint=fp(self.old)).count(), 1
        )

    def test_an_unopenable_old_backup_stops_before_the_commit(self):
        (Path(self.dest_dir) / self.first.file_name).write_bytes(
            b"x" * 200
        )  # damaged after the check
        with mock.patch.object(rotation, "preflight", return_value=rotation.Report()):
            with self.assertRaisesRegex(rotation.RotationError, "could not open"):
                self.rotate()
        self.assertEqual(keystore.load_working_key(self.store), self.old)

    def test_a_store_that_cannot_be_read_back_stops_everything(self):
        original = self.store.get
        wrong = {"on": False}

        def lying(name):
            return "AAAA" if wrong["on"] and name == keystore.PENDING_KEY else original(name)

        with mock.patch.object(self.store, "get", lying):
            wrong["on"] = True
            with self.assertRaises(keystore.KeyStoreError):
                self.rotate()
        self.assertEqual(keystore.load_working_key(self.store), self.old)

    def test_abort_before_the_commit_keeps_the_old_key_working_and_keeps_the_retired_copy(self):
        self.crash_at("_verification_backup")
        with self.assertRaises(rotation.RotationError):
            rotation.abort(confirm="yes", destination=self.destination, store=self.store)
        result = rotation.abort(
            confirm=rotation.CONFIRM_ABORT, destination=self.destination, store=self.store
        )
        self.assertEqual(result["working"], fp(self.old))
        self.assertIsNone(keystore.load_pending_key(self.store))
        self.assertEqual(keystore.load_working_key(self.store), self.old)
        self.assertEqual(keystore.load_retired_key(fp(self.old), self.store), self.old)  # not lost
        self.assertEqual(AuditEvent.objects.filter(action="backup.key_rotation_aborted").count(), 1)
        self.backup(now=timezone.now() + timedelta(minutes=5))  # backups carry on, old key
        self.rotate()  # and a fresh rotation can be started

    def test_abort_is_refused_after_the_commit(self):
        self.rotate()
        keystore.save_slot(keystore.PENDING_KEY, keystore.load_working_key(self.store), self.store)
        with self.assertRaisesRegex(rotation.RotationError, "already the working key"):
            rotation.abort(
                confirm=rotation.CONFIRM_ABORT, destination=self.destination, store=self.store
            )


class _PreflightPasses:
    """A destination whose upload fails but whose verification of existing files works (so the
    preflight passes and the failure happens at the verification backup)."""

    def __init__(self, broken, working):
        self.broken, self.working = broken, working

    def verify(self, *args):
        return self.working.verify(*args)

    def __getattr__(self, name):
        return getattr(self.broken, name)


class RollbackAndPruneTests(RotationBase):
    def test_set_current_swaps_back_and_nothing_is_lost(self):
        self.rotate()
        new = keystore.load_working_key(self.store)
        with self.assertRaises(rotation.RotationError):
            rotation.set_current(fp(self.old), confirm="nope", store=self.store)
        result = rotation.set_current(
            fp(self.old), confirm=rotation.CONFIRM_SET_CURRENT, store=self.store
        )
        self.assertEqual(keystore.load_working_key(self.store), self.old)
        self.assertEqual(keystore.load_retired_key(fp(new), self.store), new)
        self.assertIsNone(keystore.load_retired_key(fp(self.old), self.store))
        self.assertEqual(result["retired"], fp(new))
        for run in BackupRun.objects.filter(status=S.SUCCEEDED):  # every backup still opens
            self.assertIsNotNone(keystore.find_key(run.key_fingerprint, self.store))
        event = AuditEvent.objects.get(action="backup.key_current_set")
        self.assertEqual(event.summary, {"from": fp(new), "to": fp(self.old)})

    def test_set_current_refuses_unknown_keys_and_a_rotation_in_progress(self):
        with self.assertRaisesRegex(rotation.RotationError, "not among the retired keys"):
            rotation.set_current("0" * 16, confirm=rotation.CONFIRM_SET_CURRENT, store=self.store)
        keystore.save_slot(keystore.PENDING_KEY, keystore.generate_key(), self.store)
        with self.assertRaisesRegex(rotation.RotationError, "in progress"):
            rotation.set_current(
                fp(self.old), confirm=rotation.CONFIRM_SET_CURRENT, store=self.store
            )

    def prune(self, target, confirm=rotation.CONFIRM_REMOVE):
        return rotation.prune(
            target, confirm=confirm, destination=self.destination, store=self.store
        )

    def test_an_old_key_is_kept_while_any_retained_backup_needs_it(self):
        self.rotate()
        assessment = rotation.assess_prune(
            fp(self.old), destination=self.destination, store=self.store
        )
        self.assertFalse(assessment.safe)
        self.assertTrue(any("need that key" in b for b in assessment.blockers))
        with self.assertRaisesRegex(rotation.RotationError, "the key was kept"):
            self.prune(fp(self.old))
        self.assertIsNotNone(keystore.load_retired_key(fp(self.old), self.store))

    def test_an_old_key_can_be_removed_only_when_no_retained_backup_needs_it(self):
        self.rotate()
        # retention removes the old backup: the file AND the history row, in step
        self.destination.delete(self.first.remote_id)
        BackupRun.objects.filter(key_fingerprint=fp(self.old)).update(
            remote_deleted_at=timezone.now()
        )
        with self.assertRaises(rotation.RotationError):
            self.prune(fp(self.old), confirm="REMOVE")
        self.assertIsNotNone(keystore.load_retired_key(fp(self.old), self.store))
        result = self.prune(fp(self.old))
        self.assertEqual(result["removed"], fp(self.old))
        self.assertIsNone(keystore.load_retired_key(fp(self.old), self.store))
        self.assertEqual(AuditEvent.objects.filter(action="backup.key_retired_removed").count(), 1)

    def test_the_working_key_and_unknown_keys_cannot_be_pruned(self):
        self.rotate()
        working = fp(keystore.load_working_key(self.store))
        for target in (working, "f" * 16):
            with self.assertRaises(rotation.RotationError):
                self.prune(target)
        self.assertIsNotNone(keystore.load_working_key(self.store))

    def test_prune_changes_nothing_by_default(self):
        self.rotate()
        out = io.StringIO()
        call_command("prune_backup_keys", f"--destination=local:{self.dest_dir}", stdout=out)
        self.assertIn(fp(self.old), out.getvalue())
        self.assertIn("KEPT", out.getvalue())
        self.assertIn("need that key", out.getvalue())
        self.assertIsNotNone(keystore.load_retired_key(fp(self.old), self.store))


class NeverSilentTests(TestCase):
    def setUp(self):
        keystore.reset_memory_keystore()
        self.addCleanup(keystore.reset_memory_keystore)
        self.store = keystore.get_keystore()

    def test_an_existing_working_key_is_never_overwritten_silently(self):
        first = keystore.generate_key()
        keystore.save_working_key(first, self.store)
        keystore.save_working_key(first, self.store)  # the same key again is harmless
        with self.assertRaisesRegex(keystore.KeyStoreError, "never replaced silently"):
            keystore.save_working_key(keystore.generate_key(), self.store)
        self.assertEqual(keystore.load_working_key(self.store), first)

    def test_no_key_slot_is_overwritten_silently(self):
        one, two = keystore.generate_key(), keystore.generate_key()
        keystore.save_slot("backup-retired-x", one, self.store)
        with self.assertRaises(keystore.KeyStoreError):
            keystore.save_slot("backup-retired-x", two, self.store)
        self.assertEqual(keystore.load_pending_key(self.store), None)

    def test_a_damaged_key_is_an_error_never_a_missing_key_that_could_be_regenerated(self):
        self.store.set(keystore.WORKING_KEY, "not-a-key")
        with self.assertRaises(keystore.KeyStoreError):
            keystore.load_working_key(self.store)
        with self.assertRaises(keystore.KeyStoreError):
            keystore.save_working_key(keystore.generate_key(), self.store)
        with self.assertRaises(CommandError):
            call_command("setup_backup_key", "--yes", stdout=io.StringIO())
        self.assertEqual(self.store.get(keystore.WORKING_KEY), "not-a-key")

    def test_an_unreadable_store_never_leads_to_a_new_key(self):
        with mock.patch.object(self.store, "get", side_effect=keystore.KeyStoreError("locked")):
            with self.assertRaises((CommandError, keystore.KeyStoreError)):
                call_command("setup_backup_key", "--yes", stdout=io.StringIO())
        self.assertEqual(self.store.items, {})

    def test_a_missing_key_makes_a_backup_fail_it_does_not_create_one(self):
        run = services.run_backup(destination=LocalDirectoryDestination("/tmp/zz-none"))
        self.assertEqual(run.status, S.FAILED)
        self.assertIn("no backup key", run.error_summary)
        self.assertEqual(self.store.items, {})

    def test_drive_credentials_are_never_replaced_without_the_replace_flag(self):
        self.store.set(keystore.DRIVE_TOKEN, '{"refresh_token": "kept"}')
        with self.assertRaisesRegex(CommandError, "already connected"):
            call_command("connect_drive", "--client-id", "a", "--client-secret", "b")
        self.assertEqual(self.store.get(keystore.DRIVE_TOKEN), '{"refresh_token": "kept"}')


class CommandTests(RotationBase):
    def test_the_full_rotation_through_the_command_prompts_hide_and_confirm(self):
        out = io.StringIO()
        recovery = keystore.recovery_key_text(self.old)
        prompts = []

        def fake_getpass(prompt=""):
            prompts.append(prompt)  # every secret prompt goes through the hidden-input function
            if "CURRENT" in prompt:
                return recovery
            return keystore.recovery_key_text(keystore.load_pending_key(self.store))

        with (
            mock.patch("getpass.getpass", side_effect=fake_getpass),
            mock.patch("builtins.input", side_effect=AssertionError("a secret was asked in clear")),
        ):
            call_command(
                "rotate_backup_key",
                f"--destination=local:{self.dest_dir}",
                f"--confirm={rotation.CONFIRM_ROTATE}",
                stdout=out,
            )
        text = out.getvalue()
        new = keystore.load_working_key(self.store)
        self.assertIn("NEW RECOVERY KEY", text)
        self.assertIn(keystore.recovery_key_text(new), text)  # shown once, to the owner
        self.assertNotIn(keystore.recovery_key_text(self.old), text)
        self.assertNotIn(keystore.encode_key(new), text)
        self.assertIn(f"new: {fp(new)}", text)
        self.assertIn("KEEP every paper recovery key", text)
        self.assertEqual(len(prompts), 2)  # the current key and the typed-back new key, both hidden
        self.assertEqual(text.count(keystore.recovery_key_text(new)), 1)  # shown exactly once

    def test_show_recovery_key_all_lists_every_key_with_its_fingerprint_and_is_audited(self):
        self.rotate()
        new = keystore.load_working_key(self.store)
        out = io.StringIO()
        call_command("show_recovery_key", "--yes", "--all", stdout=out)
        text = out.getvalue()
        self.assertIn(f"CURRENT  fingerprint {fp(new)}", text)
        self.assertIn(f"RETIRED  fingerprint {fp(self.old)}", text)
        self.assertIn(keystore.recovery_key_text(self.old), text)
        event = AuditEvent.objects.filter(action="backup.recovery_key_shown").latest("id")
        self.assertEqual(set(event.summary["fingerprints"]), {fp(new), fp(self.old)})
        self.assertNotIn(keystore.recovery_key_text(new), str(event.summary))

    def test_backup_status_reports_the_key_ring_and_a_missing_key_loudly(self):
        self.rotate()
        out = io.StringIO()
        call_command("backup_status", stdout=out)
        self.assertIn(f"Key {fp(self.old)}: 1 retained backup(s), key available", out.getvalue())
        self.store.delete(keystore.retired_name(fp(self.old)))
        out = io.StringIO()
        call_command("backup_status", stdout=out)
        self.assertIn("KEY NOT IN THE CREDENTIAL STORE", out.getvalue())

    def test_startup_is_blocked_when_the_key_of_the_newest_backup_is_not_available(self):
        self.rotate()
        self.assertIsNone(Preflight().check_key())
        self.store.delete(keystore.retired_name(fp(self.old)))
        self.assertIsNone(Preflight().check_key())  # the newest backup's key is the working key
        self.store.set(keystore.WORKING_KEY, keystore.encode_key(keystore.generate_key()))
        problem = Preflight().check_key()
        self.assertEqual(problem.code, "KEY_MISMATCH")
        self.store.set(keystore.WORKING_KEY, "damaged")
        self.assertEqual(Preflight().check_key().code, "KEY_UNAVAILABLE")
        self.store.delete(keystore.WORKING_KEY)
        self.assertEqual(Preflight().check_key().code, "KEY_MISSING")
        with mock.patch.object(self.store, "get", side_effect=keystore.KeyStoreError("locked")):
            self.assertEqual(Preflight().check_key().code, "KEY_UNAVAILABLE")


class ReplacementComputerTests(RehearsalBase):
    """The recovery procedure of docs/KEY_ROTATION_AND_RECOVERY.md, rehearsed on disposable
    databases: a new computer has an empty credential store and only the paper recovery keys."""

    def setUp(self):
        super().setUp()
        seed_library()
        self.store = keystore.get_keystore()
        self.old_key = self.key
        self.old_run, self.old_path = self.backup()
        result = rotation.rotate(
            confirm=rotation.CONFIRM_ROTATE,
            old_recovery_text=keystore.recovery_key_text(self.old_key),
            new_recovery_prompt=lambda text: text,
            store=self.store,
            destination=self.destination,
        )
        self.new_key = keystore.load_working_key(self.store)
        self.new_run = BackupRun.objects.filter(key_fingerprint=result["new"]).get()
        self.new_path = Path(self.dest_dir) / self.new_run.file_name
        self.old_paper = keystore.recovery_key_text(self.old_key)
        self.new_paper = keystore.recovery_key_text(self.new_key)
        keystore.reset_memory_keystore()  # the new computer: nothing in the credential store

    def restore_with(self, path, paper):
        target = self.new_target()
        result = self.restore_into(target, path, recovery_text=paper, confirm_key_import=True)
        self.assertTrue(result.ok, [s for s in result.steps if not s.ok])
        return target, result

    def test_newest_backup_first_then_an_old_one_each_with_its_own_paper_key(self):
        target, first = self.restore_with(self.new_path, self.new_paper)
        self.assertTrue(first.key_imported)
        self.assertEqual(keystore.load_working_key(), self.new_key)  # the newest key is current
        self.assertEqual(query(target, "SELECT count(*) FROM members_member")[0][0], 2)
        _, second = self.restore_with(self.old_path, self.old_paper)
        self.assertTrue(second.key_imported)
        self.assertEqual(keystore.load_working_key(), self.new_key)  # NOT replaced
        self.assertEqual(keystore.load_retired_key(fp(self.old_key)), self.old_key)
        # the stores now open both generations without any paper
        for run in (self.old_run, self.new_run):
            self.assertIsNotNone(keystore.find_key(run.key_fingerprint))
        self.assertIsNone(Preflight().check_key())

    def test_an_old_backup_first_is_fixed_by_set_current_and_nothing_is_lost(self):
        self.restore_with(self.old_path, self.old_paper)
        self.assertEqual(keystore.load_working_key(), self.old_key)  # first import fills the slot
        self.restore_with(self.new_path, self.new_paper)
        self.assertEqual(keystore.load_working_key(), self.old_key)  # never overwritten
        self.assertEqual(keystore.load_retired_key(fp(self.new_key)), self.new_key)
        rotation.set_current(
            fp(self.new_key), confirm=rotation.CONFIRM_SET_CURRENT, store=self.store
        )
        self.assertEqual(keystore.load_working_key(), self.new_key)
        self.assertEqual(keystore.load_retired_key(fp(self.old_key)), self.old_key)

    def test_a_wrong_paper_key_changes_nothing_and_never_fills_the_store(self):
        target = self.new_target()
        with self.assertRaisesRegex(restore.RestoreError, "different key"):
            self.restore_into(
                target, self.new_path, recovery_text=self.old_paper, confirm_key_import=True
            )
        self.assertIsNone(keystore.load_working_key())
        self.assertUntouched(target)

    def test_restoring_into_a_computer_with_another_working_key_keeps_it(self):
        other = keystore.generate_key()
        keystore.save_working_key(other)
        self.restore_with(self.new_path, self.new_paper)
        self.assertEqual(keystore.load_working_key(), other)  # untouched
        self.assertEqual(keystore.load_retired_key(fp(self.new_key)), self.new_key)
