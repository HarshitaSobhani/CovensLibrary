"""The real pipeline against PostgreSQL: pg_dump, archive, encryption, upload and verification."""

import json
import subprocess
import tarfile
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.db import connection, transaction
from django.test import TestCase, override_settings

from audit.models import AuditEvent
from backups import crypto, keystore, services
from backups.models import BackupRun

from .helpers import FakeDestination, TempDirMixin, fresh_key, ist

S = BackupRun.Status


def open_archive(destination_dir, run, key):
    """Decrypt a stored backup and return (tar bytes, manifest dict, {path: bytes})."""
    plain = Path(destination_dir) / "plain.tar"
    crypto.decrypt_file(Path(destination_dir) / run.file_name, plain, key)
    with tarfile.open(plain) as archive:
        members = {m.name: archive.extractfile(m).read() for m in archive.getmembers()}
    plain.unlink()
    return members, json.loads(members["manifest.json"])


class SuccessfulRunTests(TempDirMixin, TestCase):
    def setUp(self):
        self.make_dirs()
        self.key = fresh_key()
        self.addCleanup(keystore.reset_memory_keystore)

    def run_backup(self, **kw):
        return services.run_backup(destination=self.destination, now=ist(2027, 3, 1, 19, 45), **kw)

    def test_a_backup_is_stored_encrypted_and_verified(self):
        run = self.run_backup(trigger="scheduled")
        self.assertEqual(run.status, S.SUCCEEDED, run.error_summary)
        stored = Path(self.dest_dir) / run.file_name
        self.assertTrue(stored.is_file())
        self.assertEqual(run.size_bytes, stored.stat().st_size)
        self.assertEqual(run.sha256, crypto.sha256_file(stored))
        self.assertIsNotNone(run.verified_at)
        self.assertEqual(run.key_fingerprint, crypto.fingerprint(self.key))
        self.assertTrue(run.file_name.startswith("coven-backup-20270301-194500-"))
        self.assertEqual((run.destination, run.remote_id), ("local", run.file_name))
        self.assertNotIn(b"PostgreSQL", stored.read_bytes()[:200])  # not a plain dump

    def test_the_archive_holds_a_real_dump_media_and_a_matching_manifest(self):
        media = Path(self.tmp_dir) / "media-root"
        (media / "logos").mkdir(parents=True)
        (media / "logos" / "logo.png").write_bytes(b"PNGDATA")
        with override_settings(MEDIA_ROOT=media):
            run = self.run_backup()
        members, manifest = open_archive(self.dest_dir, run, self.key)
        self.assertIn("database.dump", members)
        self.assertEqual(members["media/logos/logo.png"], b"PNGDATA")
        self.assertTrue(any(name.startswith("assets/") for name in members))  # application assets
        self.assertEqual(manifest["key_fingerprint"], crypto.fingerprint(self.key))
        self.assertEqual(manifest["app_version"], settings.APP_VERSION)
        self.assertIn("core.0010_backup_time_default", manifest["database"]["migrations"])
        self.assertTrue(manifest["database"]["server_version"])
        self.assertTrue(manifest["media_present"])
        import hashlib

        for entry in manifest["files"]:
            self.assertEqual(
                hashlib.sha256(members[entry["path"]]).hexdigest(), entry["sha256"], entry["path"]
            )
            self.assertEqual(len(members[entry["path"]]), entry["size"])
        dump_file = Path(self.tmp_dir) / "check.dump"
        dump_file.write_bytes(members["database.dump"])
        listing = subprocess.run(
            ["pg_restore", "--list", str(dump_file)], capture_output=True, check=False
        )
        dump_file.unlink()
        self.assertEqual(listing.returncode, 0, listing.stderr[:200])
        self.assertIn(b"backups_backuprun", listing.stdout)  # the schema is really in the dump

    def test_an_absent_media_folder_is_recorded_as_empty_not_an_error(self):
        with override_settings(MEDIA_ROOT=Path(self.tmp_dir) / "does-not-exist"):
            run = self.run_backup()
        self.assertEqual(run.status, S.SUCCEEDED)
        members, manifest = open_archive(self.dest_dir, run, self.key)
        self.assertFalse(manifest["media_present"])
        self.assertFalse(any(n.startswith("media/") for n in members))

    def test_nothing_secret_is_stored_or_logged(self):
        run = self.run_backup()
        password = settings.DATABASES["default"].get("PASSWORD") or "x-no-password-set"
        blobs = [json.dumps(run.manifest), run.error_summary, run.file_name, run.sha256]
        blobs += [json.dumps(e.summary) for e in AuditEvent.objects.all()]
        stored = (Path(self.dest_dir) / run.file_name).read_bytes()
        for text in blobs:
            self.assertNotIn(password, text)
            self.assertNotIn(self.key.hex(), text)
            self.assertNotIn(keystore.recovery_key_text(self.key), text)
        self.assertNotIn(self.key, stored)
        self.assertEqual(AuditEvent.objects.filter(action="backup.succeeded").count(), 1)

    def test_the_temporary_files_are_always_removed(self):
        self.run_backup()
        self.assertEqual(list(Path(self.tmp_dir).iterdir()), [])


class FailureTests(TempDirMixin, TestCase):
    def setUp(self):
        self.make_dirs()
        self.key = fresh_key()
        self.addCleanup(keystore.reset_memory_keystore)

    def attempt(self, destination=None, **kw):
        return services.run_backup(
            destination=destination or self.destination, now=ist(2027, 3, 1, 19, 45), **kw
        )

    def assertFailed(self, run, *words):
        self.assertEqual(run.status, S.FAILED)
        for word in words:
            self.assertIn(word, run.error_summary)
        self.assertIsNotNone(run.finished_at)
        self.assertEqual(list(Path(self.tmp_dir).iterdir()), [], "temp files left behind")
        self.assertTrue(
            AuditEvent.objects.filter(action="backup.failed", entity_id=str(run.pk)).exists()
        )
        self.assertEqual(list(Path(self.dest_dir).glob("*.cbk")), [])

    def test_no_key_is_a_clear_failure_not_a_crash(self):
        keystore.reset_memory_keystore()
        self.assertFailed(self.attempt(), "no backup key")

    def test_a_missing_pg_dump_is_a_clear_failure(self):
        with override_settings(PG_DUMP_PATH="/nonexistent/pg_dump"):
            self.assertFailed(self.attempt(), "pg_dump was not found")

    def test_a_failing_pg_dump_does_not_leak_the_password(self):
        password = settings.DATABASES["default"].get("PASSWORD") or ""
        bad = subprocess.CompletedProcess(
            [], 1, "", f"FATAL: password authentication failed {password}"
        )
        with mock.patch("backups.archive.subprocess.run", return_value=bad):
            run = self.attempt()
        self.assertFailed(run, "pg_dump failed")
        if password:
            self.assertNotIn(password, run.error_summary)

    def test_a_pg_dump_that_produces_nothing_fails(self):
        done = subprocess.CompletedProcess([], 0, "", "")
        with mock.patch("backups.archive.subprocess.run", return_value=done):
            self.assertFailed(self.attempt(), "no data")

    def test_an_upload_failure_is_recorded(self):
        self.assertFailed(
            self.attempt(FakeDestination(self.dest_dir, fail_upload=True)), "upload failure"
        )

    def test_an_upload_that_cannot_be_verified_fails_even_though_it_returned(self):
        destination = FakeDestination(self.dest_dir, lie_on_verify=True)
        run = self.attempt(destination)
        self.assertEqual(run.status, S.FAILED)
        self.assertIn("does not match", run.error_summary)
        self.assertIsNone(run.verified_at)

    def test_a_failure_after_a_success_leaves_the_old_backup_alone(self):
        good = self.attempt()
        self.assertEqual(good.status, S.SUCCEEDED)
        keystore.reset_memory_keystore()
        bad = services.run_backup(destination=self.destination, now=ist(2027, 3, 2, 19, 45))
        self.assertEqual(bad.status, S.FAILED)
        self.assertTrue((Path(self.dest_dir) / good.file_name).is_file())
        self.assertEqual(BackupRun.objects.get(pk=good.pk).status, S.SUCCEEDED)

    def test_an_unverified_upload_is_removed_and_the_run_fails(self):
        keep = self.attempt()  # an earlier verified backup must survive
        self.assertEqual(keep.status, S.SUCCEEDED)
        before = set(self.destination.list())
        bad = services.run_backup(
            destination=FakeDestination(self.dest_dir, lie_on_verify=True),
            now=ist(2027, 3, 2, 19, 45),
        )
        self.assertEqual(bad.status, S.FAILED)
        self.assertIn("does not match", bad.error_summary)
        self.assertEqual(set(self.destination.list()), before)  # only this attempt's file removed
        self.assertNotIn("also failed", bad.error_summary)

    def test_a_failed_removal_is_noted_and_the_original_error_is_kept(self):
        class Stubborn(FakeDestination):
            def delete(self, remote_id):
                from backups.destinations import DestinationError

                raise DestinationError("cannot delete")

        bad = services.run_backup(
            destination=Stubborn(self.dest_dir, lie_on_verify=True), now=ist(2027, 3, 2, 19, 45)
        )
        self.assertEqual(bad.status, S.FAILED)
        self.assertIn("does not match", bad.error_summary)
        self.assertIn("Removing the unverified upload also failed", bad.error_summary)

    def test_a_failed_run_never_claims_success_fields(self):
        run = self.attempt(FakeDestination(self.dest_dir, fail_upload=True))
        self.assertEqual((run.sha256, run.remote_id, run.verified_at), ("", "", None))


class SingleRunTests(TempDirMixin, TestCase):
    def setUp(self):
        self.make_dirs()
        fresh_key()
        self.addCleanup(keystore.reset_memory_keystore)

    def test_a_second_run_is_refused_while_one_is_running(self):
        BackupRun.objects.create(started_at=ist(2027, 3, 1, 19, 40))
        with self.assertRaises(services.BackupBusy):
            services.run_backup(destination=self.destination, now=ist(2027, 3, 1, 19, 45))
        self.assertEqual(BackupRun.objects.count(), 1)

    def test_a_run_stuck_for_hours_is_closed_as_interrupted_and_a_new_one_starts(self):
        stuck = BackupRun.objects.create(started_at=ist(2027, 3, 1, 10, 0))
        run = services.run_backup(destination=self.destination, now=ist(2027, 3, 1, 19, 45))
        self.assertEqual(run.status, S.SUCCEEDED)
        stuck.refresh_from_db()
        self.assertEqual(stuck.status, S.FAILED)
        self.assertIn("interrupted", stuck.error_summary)

    def test_the_database_allows_only_one_running_row(self):
        from django.db import IntegrityError

        BackupRun.objects.create()
        with self.assertRaises(IntegrityError), transaction.atomic():
            BackupRun.objects.create()


class StoredBackupTamperTests(TempDirMixin, TestCase):
    def setUp(self):
        self.make_dirs()
        self.key = fresh_key()
        self.addCleanup(keystore.reset_memory_keystore)
        self.run = services.run_backup(destination=self.destination, now=ist(2027, 3, 1, 19, 45))

    def test_a_modified_stored_file_fails_verification_and_decryption(self):
        stored = Path(self.dest_dir) / self.run.file_name
        self.assertTrue(
            self.destination.verify(self.run.remote_id, self.run.size_bytes, self.run.sha256)
        )
        data = bytearray(stored.read_bytes())
        data[len(data) // 2] ^= 0xFF
        stored.write_bytes(bytes(data))
        self.assertFalse(
            self.destination.verify(self.run.remote_id, self.run.size_bytes, self.run.sha256)
        )
        with self.assertRaises(crypto.DecryptionError):
            crypto.decrypt_file(stored, None, self.key)

    def test_a_truncated_or_missing_stored_file_fails_verification(self):
        stored = Path(self.dest_dir) / self.run.file_name
        stored.write_bytes(stored.read_bytes()[:-100])
        self.assertFalse(
            self.destination.verify(self.run.remote_id, self.run.size_bytes, self.run.sha256)
        )
        stored.unlink()
        self.assertFalse(
            self.destination.verify(self.run.remote_id, self.run.size_bytes, self.run.sha256)
        )

    def test_the_wrong_key_cannot_open_a_stored_backup(self):
        with self.assertRaises(crypto.DecryptionError):
            crypto.decrypt_file(
                Path(self.dest_dir) / self.run.file_name, None, keystore.generate_key()
            )


class RecordProtectionTests(TestCase):
    def finished(self):
        return BackupRun.objects.create(
            status=S.SUCCEEDED, finished_at=ist(2027, 3, 1, 20), verified_at=ist(2027, 3, 1, 20),
            sha256="a" * 64, remote_id="f.cbk", size_bytes=10, file_name="f.cbk",
        )  # fmt: skip

    def refused(self, sql, params=()):
        from django.db import DatabaseError

        with self.assertRaises(DatabaseError), transaction.atomic(), connection.cursor() as cur:
            cur.execute(sql, params)

    def test_history_cannot_be_deleted_or_rewritten(self):
        run = self.finished()
        self.refused("DELETE FROM backups_backuprun WHERE id=%s", [run.pk])
        self.refused("UPDATE backups_backuprun SET sha256='b' WHERE id=%s", [run.pk])
        self.refused("UPDATE backups_backuprun SET status='failed' WHERE id=%s", [run.pk])
        self.refused("UPDATE backups_backuprun SET error_summary='x' WHERE id=%s", [run.pk])
        self.refused("UPDATE backups_backuprun SET manifest = '{\"a\": 1}' WHERE id=%s", [run.pk])

    def test_only_verification_and_retention_fields_may_change(self):
        run = self.finished()
        BackupRun.objects.filter(pk=run.pk).update(remote_deleted_at=ist(2027, 4, 1))
        self.refused("UPDATE backups_backuprun SET remote_deleted_at=NULL WHERE id=%s", [run.pk])

    def test_a_success_must_be_verified_and_complete(self):
        from django.db import IntegrityError

        for bad in (
            dict(sha256=""),
            dict(remote_id=""),
            dict(size_bytes=0),
            dict(verified_at=None),
            dict(finished_at=None),
        ):
            when = ist(2027, 3, 1)
            fields = dict(status=S.SUCCEEDED, finished_at=when, verified_at=when,
                          sha256="a" * 64, remote_id="f", size_bytes=1)  # fmt: skip
            fields.update(bad)
            with self.assertRaises(IntegrityError), transaction.atomic():
                BackupRun.objects.create(**fields)

    def test_the_guard_function_pins_its_search_path(self):
        with connection.cursor() as cur:
            cur.execute("SELECT proconfig FROM pg_proc WHERE proname='backups_backuprun_guard'")
            self.assertIn("search_path=pg_catalog", cur.fetchone()[0])
