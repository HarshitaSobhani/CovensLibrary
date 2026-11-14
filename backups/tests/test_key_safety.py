"""Critical remediation (final security review): a key is never deleted while ANY backup needs it.

* ``abort`` looks at the real destination (every file's header), not only at the history.
* ``prune`` is fail-closed: complete listing, every header readable, history and destination agree.
* A missing key can be put back from paper, hidden and validated.
* Nothing in any output, log, audit row or error message carries key material.
"""

import io
import logging
import os
import tempfile
from pathlib import Path
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.utils import timezone

from audit.models import AuditEvent
from backups import crypto, keyscan, keystore, rotation, services
from backups.destinations import DestinationError, DriveDestination
from backups.models import BackupRun
from backups.tests.helpers import query
from backups.tests.test_restore import RehearsalBase, seed_library
from backups.tests.test_rotation import RotationBase, fp

S = BackupRun.Status
CONFIRM = rotation.CONFIRM_ABORT


def encrypted_file(directory, name, key, payload=b"payload"):
    """A real .cbk (authenticated header and chunks) holding arbitrary bytes."""
    plain = Path(tempfile.mkdtemp()) / "plain"
    plain.write_bytes(payload)
    target = Path(directory) / name
    crypto.encrypt_file(plain, target, key)
    return target


class Call:
    def __init__(self, result=None, error=None, headers=None):
        self.result, self.error, self.headers = (
            result,
            error,
            headers if headers is not None else {},
        )

    def execute(self):
        if self.error:
            raise self.error
        return self.result() if callable(self.result) else self.result


class FakeDriveFiles:
    """Just enough of Google Drive's files API: paging, trashed items, ranged downloads."""

    def __init__(self, files, page_size=2):
        self.files, self.page_size = files, page_size
        self.incomplete = False
        self.fail_on_page = None
        self.stuck_token = False
        self.ranges = []

    def list(self, q=None, fields=None, pageSize=None, pageToken=None, spaces=None):
        names = sorted(self.files)
        start = int(pageToken or 0)
        page_no = start // self.page_size
        if self.fail_on_page is not None and page_no == self.fail_on_page:
            return Call(error=OSError("connection reset"))
        chunk = names[start : start + self.page_size]
        reply = {
            "files": [
                {
                    "id": i,
                    "name": self.files[i]["name"],
                    "size": str(len(self.files[i]["data"])),
                    "mimeType": self.files[i].get("mime", "application/octet-stream"),
                    "trashed": self.files[i].get("trashed", False),
                }
                for i in chunk
            ]
        }
        if self.incomplete:
            reply["incompleteSearch"] = True
        if start + self.page_size < len(names):
            reply["nextPageToken"] = "2" if self.stuck_token else str(start + self.page_size)
        return Call(reply)

    def get_media(self, fileId):
        files = self

        class Request:
            def __init__(self):
                self.headers = {}

            def execute(self):
                data = files.files[fileId]["data"]
                spec = self.headers.get("Range")
                files.ranges.append(spec)
                if spec:
                    end = int(spec.split("-")[1])
                    return data[: end + 1]
                return data

        return Request()

    def delete(self, fileId):
        return Call(lambda: self.files.pop(fileId, None))


class FakeDrive:
    def __init__(self, files, **kw):
        self.f = FakeDriveFiles(files, **kw)

    def files(self):
        return self.f


def drive_with(**files):
    """A DriveDestination over fake files: {id: bytes or dict}."""
    entries = {
        i: (v if isinstance(v, dict) else {"name": f"{i}.cbk", "data": v}) for i, v in files.items()
    }
    service = FakeDrive(entries)
    return DriveDestination(service=service, folder_id="FOLDER"), service.f


def header_bytes(key):
    path = encrypted_file(tempfile.mkdtemp(), "h.cbk", key)
    return path.read_bytes()


# ----------------------------------------------------------------------------------- scanning
class ScanTests(RotationBase):
    def test_every_file_is_listed_with_the_key_it_names(self):
        scan = keyscan.scan_destination(self.destination)
        self.assertEqual([e.fingerprint for e in scan.entries], [fp(self.old)])
        self.assertEqual(scan.problems, [])
        self.assertEqual(scan.fingerprints(), [fp(self.old)])

    def test_a_missing_destination_is_an_error_not_an_empty_listing(self):
        gone = Path(self.dest_dir) / "nope"
        from backups.destinations import LocalDirectoryDestination

        with self.assertRaisesRegex(keyscan.ScanError, "could not be fully listed"):
            keyscan.scan_destination(LocalDirectoryDestination(gone))

    def test_a_permission_failure_is_an_error(self):
        with mock.patch("os.scandir", side_effect=PermissionError("denied")):
            with self.assertRaisesRegex(keyscan.ScanError, "PermissionError"):
                keyscan.scan_destination(self.destination)

    def test_a_file_that_cannot_be_read_is_reported_not_skipped(self):
        real = open

        def deny(path, *args, **kwargs):
            if str(path).endswith(self.first.file_name):
                raise PermissionError("denied")
            return real(path, *args, **kwargs)

        with mock.patch("builtins.open", deny):
            scan = keyscan.scan_destination(self.destination)
        self.assertEqual(len(scan.problems), 1)
        self.assertIn("could not be read", scan.problems[0].problem)

    def test_malformed_unknown_and_truncated_items_are_problems(self):
        root = Path(self.dest_dir)
        (root / "garbage.cbk").write_bytes(b"this is not a backup at all, just text" * 3)
        (root / "short.cbk").write_bytes(b"COVENBK1")
        (root / "notes.txt").write_text("hello")
        (root / "empty.cbk").write_bytes(b"")
        (root / "subfolder").mkdir()
        scan = keyscan.scan_destination(self.destination)
        bad = {e.name: e.problem for e in scan.problems}
        self.assertEqual(
            set(bad), {"garbage.cbk", "short.cbk", "notes.txt", "empty.cbk", "subfolder"}
        )
        self.assertIn("not a backup file of a supported version", bad["garbage.cbk"])
        self.assertIn("too short", bad["short.cbk"])
        self.assertIn("folder", bad["subfolder"])
        self.assertEqual(len(scan.readable), 1)  # the real backup is still read

    def test_a_header_with_an_unsupported_version_or_chunk_size_is_refused(self):
        raw = bytearray(header_bytes(self.old))
        raw[8] = 99  # the version byte
        path = Path(self.dest_dir) / "future.cbk"
        path.write_bytes(bytes(raw))
        scan = keyscan.scan_destination(self.destination)
        self.assertTrue(any(e.name == "future.cbk" and e.problem for e in scan.entries))

    def test_a_destination_without_enumeration_is_refused(self):
        with self.assertRaisesRegex(keyscan.ScanError, "cannot be enumerated"):
            keyscan.scan_destination(object())

    def test_duplicates_are_reported(self):
        drive, _ = drive_with(
            a={"name": "same.cbk", "data": header_bytes(self.old)},
            b={"name": "same.cbk", "data": header_bytes(self.old)},
        )
        self.assertEqual(keyscan.scan_destination(drive).duplicate_names, ["same.cbk"])


class DriveScanTests(RotationBase):
    def test_every_page_is_followed_and_trashed_items_are_included(self):
        files = {f"f{i}": {"name": f"b{i}.cbk", "data": header_bytes(self.old)} for i in range(5)}
        files["f2"]["trashed"] = True  # a trashed backup is still restorable: it counts
        drive, fake = drive_with(**files)
        scan = keyscan.scan_destination(drive)
        self.assertEqual(len(scan.entries), 5)
        self.assertTrue(any(e.trashed for e in scan.entries))
        self.assertEqual(set(fake.ranges), {f"bytes=0-{crypto.HEADER.size - 1}"})  # header only

    def test_an_incomplete_listing_is_an_error(self):
        drive, fake = drive_with(a=header_bytes(self.old))
        fake.incomplete = True
        with self.assertRaisesRegex(keyscan.ScanError, "incomplete"):
            keyscan.scan_destination(drive)

    def test_a_failure_on_a_later_page_is_an_error_not_a_short_list(self):
        files = {f"f{i}": header_bytes(self.old) for i in range(5)}
        drive, fake = drive_with(**files)
        fake.fail_on_page = 1
        with self.assertRaises(keyscan.ScanError):
            keyscan.scan_destination(drive)

    def test_a_listing_that_does_not_advance_is_an_error(self):
        drive, fake = drive_with(**{f"f{i}": header_bytes(self.old) for i in range(5)})
        fake.stuck_token = True
        with self.assertRaisesRegex(keyscan.ScanError, "did not advance"):
            keyscan.scan_destination(drive)

    def test_an_unknown_folder_cannot_be_listed(self):
        drive, _ = drive_with(a=header_bytes(self.old))
        drive._folder_id = None
        with self.assertRaisesRegex(keyscan.ScanError, "not known"):
            keyscan.scan_destination(drive)


# ----------------------------------------------------------------------------------- abort
class AbortTests(RotationBase):
    def crash(self, name):
        with mock.patch.object(rotation, name, side_effect=RuntimeError("power cut")):
            with self.assertRaises(RuntimeError):
                self.rotate()

    def crash_before_commit(self):
        """Everything done (verification backup included) except the commit itself."""
        with mock.patch.object(keystore, "save_working_key", side_effect=RuntimeError("cut")):
            with self.assertRaises(RuntimeError):
                self.rotate()

    def abort(self, destination=None):
        return rotation.abort(
            confirm=CONFIRM, destination=destination or self.destination, store=self.store
        )

    def assert_every_backup_key_is_available(self):
        """The invariant: every backup that exists names a key the store holds."""
        scan = keyscan.scan_destination(self.destination)
        for entry in scan.readable:
            self.assertIsNotNone(keystore.find_key(entry.fingerprint, self.store), entry.name)
        for run in BackupRun.objects.filter(status=S.SUCCEEDED, remote_deleted_at__isnull=True):
            self.assertIsNotNone(keystore.find_key(run.key_fingerprint, self.store), run.file_name)

    def test_abort_before_any_backup_with_the_new_key_is_allowed(self):
        self.crash("_verification_backup")
        pending = keystore.load_pending_key(self.store)
        self.assertEqual(
            BackupRun.objects.filter(key_fingerprint=fp(pending), status=S.SUCCEEDED).count(), 0
        )
        result = self.abort()
        self.assertEqual(result["working"], fp(self.old))
        self.assertIsNone(keystore.load_pending_key(self.store))
        self.assert_every_backup_key_is_available()

    def test_abort_after_the_verification_backup_is_refused_and_the_key_is_kept(self):
        self.crash_before_commit()
        pending = keystore.load_pending_key(self.store)
        self.assertTrue(
            BackupRun.objects.filter(key_fingerprint=fp(pending), status=S.SUCCEEDED).exists()
        )
        with self.assertRaisesRegex(rotation.RotationError, "rotate_backup_key --resume"):
            self.abort()
        self.assertEqual(keystore.load_pending_key(self.store), pending)  # NOT deleted
        self.assertEqual(AuditEvent.objects.filter(action="backup.key_rotation_aborted").count(), 0)
        self.assert_every_backup_key_is_available()
        # and the rotation can be finished
        rotation.resume(
            new_recovery_prompt=lambda text: text, store=self.store, destination=self.destination
        )
        self.assertEqual(keystore.load_working_key(self.store), pending)
        self.assert_every_backup_key_is_available()

    def test_a_backup_uploaded_but_never_recorded_still_blocks_abort(self):
        """The process died after the upload and before the history row was written: the database
        knows nothing, the destination file proves the key is in use."""
        self.crash("_verification_backup")
        pending = keystore.load_pending_key(self.store)
        encrypted_file(self.dest_dir, "coven-backup-unrecorded.cbk", pending)
        self.assertEqual(
            BackupRun.objects.filter(key_fingerprint=fp(pending)).count(), 0
        )  # the history has no idea
        with self.assertRaisesRegex(rotation.RotationError, "made with the new key"):
            self.abort()
        self.assertEqual(keystore.load_pending_key(self.store), pending)

    def test_a_backup_still_running_blocks_abort(self):
        self.crash("_verification_backup")
        BackupRun.objects.create(started_at=timezone.now())  # status running
        with self.assertRaisesRegex(rotation.RotationError, "running"):
            self.abort()
        self.assertIsNotNone(keystore.load_pending_key(self.store))

    def test_an_unavailable_destination_refuses_abort_and_deletes_nothing(self):
        self.crash("_verification_backup")
        from backups.destinations import LocalDirectoryDestination

        gone = LocalDirectoryDestination(Path(self.dest_dir) / "unplugged")
        with self.assertRaisesRegex(rotation.RotationError, "Abort refused, nothing was deleted"):
            self.abort(gone)
        broken = mock.Mock()
        broken.enumerate_files.side_effect = DestinationError("offline")
        broken.read_head = mock.Mock()
        with self.assertRaises(rotation.RotationError):
            self.abort(broken)
        self.assertIsNotNone(keystore.load_pending_key(self.store))

    def test_a_malformed_header_refuses_abort_because_it_might_need_the_key(self):
        self.crash("_verification_backup")
        (Path(self.dest_dir) / "mystery.cbk").write_bytes(b"\x00" * 64)
        with self.assertRaisesRegex(rotation.RotationError, "could not be safely inspected"):
            self.abort()
        self.assertIsNotNone(keystore.load_pending_key(self.store))

    def test_an_incomplete_drive_listing_refuses_abort(self):
        self.crash("_verification_backup")
        drive, fake = drive_with(a=header_bytes(self.old))
        fake.incomplete = True
        with self.assertRaises(rotation.RotationError):
            self.abort(drive)
        self.assertIsNotNone(keystore.load_pending_key(self.store))

    def test_a_drive_destination_with_a_backup_made_with_the_new_key_refuses_abort(self):
        self.crash("_verification_backup")
        pending = keystore.load_pending_key(self.store)
        drive, _ = drive_with(a=header_bytes(self.old), b=header_bytes(pending))
        with self.assertRaisesRegex(rotation.RotationError, "made with the new key"):
            self.abort(drive)

    def test_a_missing_pending_key_says_so_and_changes_nothing(self):
        with self.assertRaisesRegex(rotation.RotationError, "No rotation is in progress"):
            self.abort()
        self.assertEqual(keystore.load_working_key(self.store), self.old)

    def test_the_wrong_confirmation_and_an_already_committed_key_are_refused(self):
        self.crash("_verification_backup")
        with self.assertRaises(rotation.RotationError):
            rotation.abort(confirm="abort", destination=self.destination, store=self.store)
        keystore.save_working_key(
            keystore.load_pending_key(self.store), self.store, replace=True
        )  # committed
        with self.assertRaisesRegex(rotation.RotationError, "already the working key"):
            self.abort()

    def test_the_command_refuses_with_a_plain_message_and_no_traceback(self):
        self.crash_before_commit()
        out = io.StringIO()
        with self.assertRaisesRegex(CommandError, "--resume"):
            call_command(
                "rotate_backup_key",
                "--abort",
                f"--destination=local:{self.dest_dir}",
                f"--confirm={CONFIRM}",
                stdout=out,
            )
        self.assertIsNotNone(keystore.load_pending_key(self.store))

    def test_backup_busy_is_a_controlled_error_in_the_rotation(self):
        with mock.patch.object(services, "run_backup", side_effect=services.BackupBusy("busy")):
            with self.assertRaisesRegex(rotation.RotationError, "A backup is running"):
                self.rotate()
        self.assertEqual(keystore.load_working_key(self.store), self.old)  # nothing lost
        self.assertIsNotNone(keystore.load_pending_key(self.store))  # resumable
        out = io.StringIO()
        with (
            mock.patch.object(services, "run_backup", side_effect=services.BackupBusy("busy")),
            mock.patch(
                "getpass.getpass",
                side_effect=lambda p="": keystore.recovery_key_text(
                    keystore.load_pending_key(self.store)
                ),
            ),
        ):
            with self.assertRaisesRegex(CommandError, "running right now|A backup is running"):
                call_command(
                    "rotate_backup_key",
                    "--resume",
                    f"--destination=local:{self.dest_dir}",
                    stdout=out,
                )


# ----------------------------------------------------------------------------------- import
class ImportTests(RotationBase):
    def lose_the_pending_key(self):
        """A rotation interrupted after the verification backup, then the pending slot is lost."""
        with mock.patch.object(keystore, "save_working_key", side_effect=RuntimeError("cut")):
            with self.assertRaises(RuntimeError):
                self.rotate()
        pending = keystore.load_pending_key(self.store)
        self.store.delete(keystore.PENDING_KEY)
        self.assertIsNone(keystore.load_pending_key(self.store))
        return pending

    def import_key(self, paper, **kw):
        kw.setdefault("confirm", rotation.CONFIRM_IMPORT)
        kw.setdefault("as_slot", "pending")
        kw.setdefault("destination", self.destination)
        return rotation.import_key(paper, store=self.store, **kw)

    def test_a_missing_pending_key_is_restored_from_paper_and_the_rotation_resumed(self):
        pending = self.lose_the_pending_key()
        with self.assertRaisesRegex(rotation.RotationError, "No rotation is in progress"):
            rotation.resume(
                new_recovery_prompt=lambda t: t, store=self.store, destination=self.destination
            )
        result = self.import_key(keystore.recovery_key_text(pending))
        self.assertEqual(result, {"imported": fp(pending), "slot": "pending"})
        self.assertEqual(keystore.load_pending_key(self.store), pending)
        rotation.resume(
            new_recovery_prompt=lambda t: t, store=self.store, destination=self.destination
        )
        self.assertEqual(keystore.load_working_key(self.store), pending)
        self.assertEqual(keystore.load_retired_key(fp(self.old), self.store), self.old)
        event = AuditEvent.objects.get(action="backup.key_imported")
        self.assertEqual(event.summary, {"fingerprint": fp(pending), "slot": "pending"})

    def test_the_paper_key_is_validated_and_must_open_a_real_backup(self):
        pending = self.lose_the_pending_key()
        paper = keystore.recovery_key_text(pending)
        typo = paper[:-1] + ("A" if paper[-1] != "A" else "B")
        for bad, message in ((typo, "typing mistake"), ("", "characters|length|valid")):
            with self.assertRaisesRegex(rotation.RotationError, message):
                self.import_key(bad)
        other = keystore.recovery_key_text(keystore.generate_key())  # valid, but opens nothing
        with self.assertRaisesRegex(rotation.RotationError, "No backup at the destination"):
            self.import_key(other, as_slot="retired")
        with self.assertRaisesRegex(rotation.RotationError, "Cancelled"):
            self.import_key(paper, confirm="yes")
        self.assertIsNone(keystore.load_pending_key(self.store))
        self.assertEqual(AuditEvent.objects.filter(action="backup.key_imported").count(), 0)

    def test_nothing_existing_is_ever_overwritten(self):
        self.rotate()
        new = keystore.load_working_key(self.store)
        for slot in ("pending", "retired"):
            with self.assertRaisesRegex(rotation.RotationError, "already in the credential store"):
                self.import_key(keystore.recovery_key_text(new), as_slot=slot)
            with self.assertRaisesRegex(rotation.RotationError, "already in the credential store"):
                self.import_key(keystore.recovery_key_text(self.old), as_slot=slot)
        self.assertEqual(keystore.load_working_key(self.store), new)
        self.assertEqual(keystore.load_retired_key(fp(self.old), self.store), self.old)

    def test_pending_is_refused_without_an_unfinished_rotation(self):
        self.rotate()  # committed
        new = keystore.load_working_key(self.store)
        self.store.delete(keystore.WORKING_KEY)
        with self.assertRaisesRegex(rotation.RotationError, "No unfinished rotation"):
            self.import_key(keystore.recovery_key_text(new))
        self.import_key(keystore.recovery_key_text(new), as_slot="retired")
        self.assertEqual(keystore.load_retired_key(fp(new), self.store), new)

    def test_pending_is_refused_while_another_pending_key_exists(self):
        keystore.save_slot(keystore.PENDING_KEY, keystore.generate_key(), self.store)
        stranger = keystore.recovery_key_text(keystore.generate_key())
        with self.assertRaisesRegex(rotation.RotationError, "pending key already exists"):
            self.import_key(stranger)

    def test_an_unavailable_destination_refuses_the_import(self):
        pending = self.lose_the_pending_key()
        broken = mock.Mock()
        broken.enumerate_files.side_effect = DestinationError("offline")
        with self.assertRaisesRegex(rotation.RotationError, "Nothing was imported"):
            self.import_key(keystore.recovery_key_text(pending), destination=broken)
        self.assertIsNone(keystore.load_pending_key(self.store))
        with self.assertRaisesRegex(rotation.RotationError, "Name a backup file"):
            self.import_key(keystore.recovery_key_text(pending), destination=None)

    def test_a_local_backup_file_can_be_the_proof_when_there_is_no_destination(self):
        # a replacement computer with a backup on a USB stick and the old key only on paper
        keystore.reset_memory_keystore()
        usb = Path(self.dest_dir) / self.first.file_name
        result = rotation.import_key(
            keystore.recovery_key_text(self.old),
            confirm=rotation.CONFIRM_IMPORT,
            as_slot="retired",
            backup_file=usb,
            store=self.store,
        )
        self.assertEqual(result["imported"], fp(self.old))
        for wrong in (Path(self.dest_dir) / "missing.cbk",):
            with self.assertRaisesRegex(rotation.RotationError, "not a readable backup"):
                rotation.import_key(
                    keystore.recovery_key_text(keystore.generate_key()),
                    confirm=rotation.CONFIRM_IMPORT,
                    as_slot="retired",
                    backup_file=wrong,
                    store=self.store,
                )
        with self.assertRaisesRegex(rotation.RotationError, "different key"):
            rotation.import_key(
                keystore.recovery_key_text(keystore.generate_key()),
                confirm=rotation.CONFIRM_IMPORT,
                as_slot="retired",
                backup_file=usb,
                store=self.store,
            )

    def test_the_command_takes_the_key_hidden_and_never_echoes_it(self):
        pending = self.lose_the_pending_key()
        paper = keystore.recovery_key_text(pending)
        out = io.StringIO()
        with (
            mock.patch("getpass.getpass", return_value=paper) as hidden,
            mock.patch("builtins.input", side_effect=AssertionError("asked in clear")),
        ):
            call_command(
                "import_backup_key",
                "--as=pending",
                f"--destination=local:{self.dest_dir}",
                f"--confirm={rotation.CONFIRM_IMPORT}",
                stdout=out,
            )
        hidden.assert_called_once()
        text = out.getvalue()
        self.assertIn(fp(pending), text)
        self.assertIn("rotate_backup_key --resume", text)
        self.assertNotIn(paper, text)
        self.assertNotIn(keystore.encode_key(pending), text)
        self.assertEqual(keystore.load_pending_key(self.store), pending)

    def test_the_command_refuses_a_bad_key_with_a_plain_message(self):
        with mock.patch("getpass.getpass", return_value="NOT-A-KEY"):
            with self.assertRaises(CommandError) as ctx:
                call_command(
                    "import_backup_key",
                    f"--destination=local:{self.dest_dir}",
                    f"--confirm={rotation.CONFIRM_IMPORT}",
                    stdout=io.StringIO(),
                )
        self.assertNotIn("NOT-A-KEY", str(ctx.exception))


# ----------------------------------------------------------------------------------- prune
class PruneTests(RotationBase):
    """A retired key is removed only when the destination ITSELF shows that nothing needs it."""

    def setUp(self):
        super().setUp()
        self.rotate()
        self.old_fp = fp(self.old)

    def prune(self, destination=None, target=None, **kw):
        return rotation.prune(
            target or self.old_fp,
            confirm=rotation.CONFIRM_REMOVE,
            destination=destination or self.destination,
            store=self.store,
            **kw,
        )

    def retire_the_old_backups_for_real(self):
        """What retention does: remove the file and mark the history row, in step."""
        for run in BackupRun.objects.filter(key_fingerprint=self.old_fp):
            self.destination.delete(run.remote_id)
        BackupRun.objects.filter(key_fingerprint=self.old_fp).update(
            remote_deleted_at=timezone.now()
        )

    def assert_kept(self):
        self.assertIsNotNone(keystore.load_retired_key(self.old_fp, self.store))
        self.assertEqual(AuditEvent.objects.filter(action="backup.key_retired_removed").count(), 0)

    def refuses(self, message, **kw):
        with self.assertRaisesRegex(rotation.RotationError, message):
            self.prune(**kw)
        self.assert_kept()

    # -- the exact reported scenario ------------------------------------------------------
    def test_an_old_key_backup_with_no_history_row_blocks_pruning(self):
        self.retire_the_old_backups_for_real()  # the history says: nothing needs the old key
        self.assertFalse(
            BackupRun.objects.filter(key_fingerprint=self.old_fp, remote_deleted_at=None).exists()
        )
        encrypted_file(self.dest_dir, "coven-backup-older-timeline.cbk", self.old)  # no row
        assessment = rotation.assess_prune(
            self.old_fp, destination=self.destination, store=self.store
        )
        self.assertFalse(assessment.safe)
        self.assertTrue(any("need that key" in b for b in assessment.blockers))
        self.assertTrue(any("not in this database's history" in b for b in assessment.blockers))
        self.refuses("the key was kept")
        self.assertEqual(keystore.find_key(self.old_fp, self.store), self.old)  # still available

    # -- the scenario through a REAL restore of an older database -------------------------
    # (see OlderDatabaseRestoreTests below)

    def test_a_missing_destination_refuses(self):
        from backups.destinations import LocalDirectoryDestination

        self.refuses(
            "could not be fully listed",
            destination=LocalDirectoryDestination(Path(self.dest_dir) / "unplugged"),
        )

    def test_a_permission_failure_refuses(self):
        with mock.patch("os.scandir", side_effect=PermissionError("denied")):
            self.refuses("could not be fully listed")

    def test_an_incomplete_drive_listing_refuses(self):
        drive, fake = drive_with(a=header_bytes(self.old))
        fake.incomplete = True
        self.refuses("incomplete", destination=drive)

    def test_a_silently_short_listing_is_caught_by_the_history(self):
        """A listing that quietly misses files (no flag at all) cannot hide a needed key: the
        history lists backups that the listing does not contain."""
        live = BackupRun.objects.filter(status=S.SUCCEEDED, remote_deleted_at__isnull=True).first()
        drive, _ = drive_with(
            only={"name": "other.cbk", "data": header_bytes(keystore.generate_key())}
        )
        self.assertTrue(live is not None)
        self.refuses("not at the destination", destination=drive)

    def test_a_malformed_header_refuses(self):
        self.retire_the_old_backups_for_real()
        (Path(self.dest_dir) / "mystery.cbk").write_bytes(b"\xff" * 80)
        self.refuses("could not be safely inspected")

    def test_a_file_that_is_not_a_backup_refuses_too(self):
        self.retire_the_old_backups_for_real()
        (Path(self.dest_dir) / "notes.txt").write_text("remember the milk")
        self.refuses("could not be safely inspected")

    def test_an_unknown_key_fingerprint_is_not_treated_as_irrelevant(self):
        self.retire_the_old_backups_for_real()
        stranger = keystore.generate_key()
        encrypted_file(self.dest_dir, "from-somewhere-else.cbk", stranger)
        assessment = rotation.assess_prune(
            self.old_fp, destination=self.destination, store=self.store
        )
        self.assertFalse(assessment.safe)
        self.assertTrue(any(fp(stranger) in b for b in assessment.blockers))
        self.refuses("not in this database's history")

    def test_duplicate_backup_files_refuse(self):
        new_run = BackupRun.objects.filter(
            key_fingerprint=fp(keystore.load_working_key(self.store))
        ).get()
        drive, fake = drive_with(
            a={
                "name": new_run.file_name,
                "data": header_bytes(keystore.load_working_key(self.store)),
            },
            b={
                "name": new_run.file_name,
                "data": header_bytes(keystore.load_working_key(self.store)),
            },
        )
        self.refuses("duplicate backup file names", destination=drive)

    def test_an_empty_destination_with_history_refuses(self):
        for entry in list(Path(self.dest_dir).iterdir()):
            entry.unlink()
        self.refuses("destination is empty|not at the destination")

    def test_an_empty_destination_with_no_history_at_all_is_safe(self):
        """Nothing exists anywhere that could need the key."""
        for entry in list(Path(self.dest_dir).iterdir()):
            entry.unlink()
        with mock.patch.object(rotation, "_live_backups", return_value=BackupRun.objects.none()):
            assessment = rotation.assess_prune(
                self.old_fp, destination=self.destination, store=self.store
            )
        self.assertTrue(assessment.safe, assessment.blockers)

    def test_a_backup_in_the_history_that_is_not_at_the_destination_refuses(self):
        self.destination.delete(
            BackupRun.objects.filter(key_fingerprint=fp(keystore.load_working_key(self.store)))
            .get()
            .remote_id
        )
        self.refuses("not at the destination")

    # -- the safe outcomes -----------------------------------------------------------------
    def test_normal_pruning_after_every_dependent_backup_is_genuinely_gone(self):
        self.retire_the_old_backups_for_real()
        assessment = rotation.assess_prune(
            self.old_fp, destination=self.destination, store=self.store
        )
        self.assertTrue(assessment.safe, assessment.blockers)
        self.assertEqual(self.prune(), {"removed": self.old_fp})
        self.assertIsNone(keystore.load_retired_key(self.old_fp, self.store))
        self.assertIsNone(keystore.find_key(self.old_fp, self.store))
        self.assertEqual(AuditEvent.objects.filter(action="backup.key_retired_removed").count(), 1)
        # the new key is untouched
        self.assertIsNotNone(keystore.load_working_key(self.store))

    def test_a_complete_drive_destination_with_no_dependent_backup_allows_pruning(self):
        self.retire_the_old_backups_for_real()
        new = keystore.load_working_key(self.store)
        run = BackupRun.objects.get(key_fingerprint=fp(new))
        # the same backup, as a Drive destination sees it (the history row names the Drive id)
        drive, _ = drive_with(**{run.remote_id: {"name": run.file_name, "data": header_bytes(new)}})
        self.assertEqual(self.prune(destination=drive), {"removed": self.old_fp})

    def test_the_wrong_confirmation_or_a_rotation_in_progress_refuses(self):
        self.retire_the_old_backups_for_real()
        with self.assertRaisesRegex(rotation.RotationError, "Cancelled"):
            rotation.prune(
                self.old_fp, confirm="remove", destination=self.destination, store=self.store
            )
        keystore.save_slot(keystore.PENDING_KEY, keystore.generate_key(), self.store)
        self.refuses("rotation is in progress")

    def test_the_command_lists_the_reasons_and_changes_nothing(self):
        encrypted_file(self.dest_dir, "extra.cbk", self.old)
        out = io.StringIO()
        call_command("prune_backup_keys", f"--destination=local:{self.dest_dir}", stdout=out)
        text = out.getvalue()
        self.assertIn(f"{self.old_fp}: KEPT", text)
        self.assertIn("not in this database's history", text)
        with self.assertRaisesRegex(CommandError, "the key was kept"):
            call_command(
                "prune_backup_keys",
                f"--fingerprint={self.old_fp}",
                f"--destination=local:{self.dest_dir}",
                f"--confirm={rotation.CONFIRM_REMOVE}",
                stdout=io.StringIO(),
            )
        self.assert_kept()

    def test_the_command_reports_a_missing_destination_plainly(self):
        out = io.StringIO()
        call_command("prune_backup_keys", f"--destination=local:{self.dest_dir}/gone", stdout=out)
        self.assertIn("could not be fully listed", out.getvalue())
        self.assert_kept()


class OlderDatabaseRestoreTests(RehearsalBase):
    """The reported defect, through a REAL restore: the application database is replaced by an
    older backup, whose history lacks later backups that still sit at the destination."""

    def setUp(self):
        super().setUp()
        seed_library()
        self.store = keystore.get_keystore()
        self.old_key = self.key
        self.a_run, self.a_path = self.backup()  # an old-key backup
        rotation.rotate(
            confirm=rotation.CONFIRM_ROTATE,
            old_recovery_text=keystore.recovery_key_text(self.old_key),
            new_recovery_prompt=lambda text: text,
            store=self.store,
            destination=self.destination,
        )
        self.new_key = keystore.load_working_key(self.store)
        self.b_run = BackupRun.objects.get(key_fingerprint=fp(self.new_key))
        # retention has removed the old backup, file and history row in step
        self.destination.delete(self.a_run.remote_id)
        BackupRun.objects.filter(pk=self.a_run.pk).update(remote_deleted_at=timezone.now())
        # a LATER backup that used the old key again (key rolled back for a while) reaches the
        # destination; the history row is not in the older backup that is restored below
        self.later = encrypted_file(self.dest_dir, "coven-backup-later-old-key.cbk", self.old_key)

    def test_pruning_after_restoring_an_older_database_is_refused_and_the_key_survives(self):
        target = self.new_target()
        self.restore_into(target, Path(self.dest_dir) / self.b_run.file_name, recovery_text=None)
        # the application now runs on the restored (older) database
        original = connection.settings_dict["NAME"]
        connection.close()
        connection.settings_dict["NAME"] = target

        def back():
            connection.close()
            connection.settings_dict["NAME"] = original

        self.addCleanup(back)
        history = list(
            BackupRun.objects.filter(remote_deleted_at__isnull=True).values_list(
                "file_name", flat=True
            )
        )
        self.assertNotIn(self.later.name, history)  # the restored history does not know the file
        assessment = rotation.assess_prune(
            fp(self.old_key), destination=self.destination, store=self.store
        )
        self.assertFalse(assessment.safe)
        self.assertTrue(any("need that key" in b for b in assessment.blockers))
        with self.assertRaisesRegex(rotation.RotationError, "the key was kept"):
            rotation.prune(
                fp(self.old_key),
                confirm=rotation.CONFIRM_REMOVE,
                destination=self.destination,
                store=self.store,
            )
        self.assertEqual(keystore.find_key(fp(self.old_key), self.store), self.old_key)
        # the file that needs the key still opens with it
        crypto.decrypt_file(self.later, None, keystore.find_key(fp(self.old_key), self.store))
        self.assertEqual(query(target, "SELECT count(*) FROM members_member")[0][0], 2)


# ----------------------------------------------------------------------------------- disclosure
class DisclosureTests(RotationBase):
    """No command output, log line, audit row, history row or error message contains key material
    (the single sanctioned exception: the NEW recovery key is shown once on the screen)."""

    def key_texts(self, *keys):
        return [t for k in keys for t in (keystore.encode_key(k), keystore.recovery_key_text(k))]

    def everything(self):
        parts = [str(e.summary) + e.action for e in AuditEvent.objects.all()]
        parts += [str(vars(r)) for r in BackupRun.objects.all()]
        return "\n".join(parts)

    def capture_logs(self):
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setLevel(logging.DEBUG)
        root = logging.getLogger()
        root.addHandler(handler)
        old_level = root.level
        root.setLevel(logging.DEBUG)
        self.addCleanup(root.removeHandler, handler)
        self.addCleanup(root.setLevel, old_level)
        return stream

    def test_a_complete_rotation_and_the_follow_up_commands_disclose_nothing(self):
        logs = self.capture_logs()
        out, err = io.StringIO(), io.StringIO()
        recovery = keystore.recovery_key_text(self.old)

        def fake_getpass(prompt=""):
            if "CURRENT" in prompt:
                return recovery
            return keystore.recovery_key_text(keystore.load_pending_key(self.store))

        with mock.patch("getpass.getpass", side_effect=fake_getpass):
            call_command(
                "rotate_backup_key",
                f"--destination=local:{self.dest_dir}",
                f"--confirm={rotation.CONFIRM_ROTATE}",
                stdout=out,
                stderr=err,
            )
        new = keystore.load_working_key(self.store)
        screen = out.getvalue() + err.getvalue()
        for text in self.key_texts(self.old):
            self.assertNotIn(text, screen + logs.getvalue() + self.everything())
        self.assertEqual(screen.count(keystore.recovery_key_text(new)), 1)  # the one sanctioned
        self.assertNotIn(keystore.encode_key(new), screen)
        for text in self.key_texts(new):
            self.assertNotIn(text, logs.getvalue() + self.everything())
        # the follow-up commands
        more = io.StringIO()
        call_command("backup_status", stdout=more)
        call_command("prune_backup_keys", f"--destination=local:{self.dest_dir}", stdout=more)
        for text in self.key_texts(self.old, new):
            self.assertNotIn(text, more.getvalue() + logs.getvalue() + self.everything())

    def test_error_messages_never_contain_the_keys_that_were_typed_or_stored(self):
        logs = self.capture_logs()
        wrong = keystore.generate_key()
        wrong_paper = keystore.recovery_key_text(wrong)
        messages = []

        def attempt(function):
            try:
                function()
            except Exception as exc:  # noqa: BLE001
                messages.append(str(exc))

        attempt(lambda: self.rotate(old_recovery_text=wrong_paper))
        attempt(lambda: self.rotate(typed="WRONG"))
        attempt(
            lambda: rotation.import_key(
                wrong_paper,
                confirm=rotation.CONFIRM_IMPORT,
                as_slot="retired",
                destination=self.destination,
                store=self.store,
            )
        )
        attempt(
            lambda: rotation.prune(
                fp(self.old),
                confirm="nope",
                destination=self.destination,
                store=self.store,
            )
        )
        pending = keystore.load_pending_key(self.store)
        texts = self.key_texts(self.old, wrong) + (self.key_texts(pending) if pending else [])
        blob = "\n".join(messages) + logs.getvalue() + self.everything()
        self.assertTrue(messages)
        for text in texts:
            self.assertNotIn(text, blob)

    def test_the_keystore_never_appears_in_a_failed_backup_message(self):
        run = services.run_backup(destination=mock.Mock(upload=mock.Mock(side_effect=OSError("x"))))
        self.assertEqual(run.status, S.FAILED)
        for text in self.key_texts(self.old):
            self.assertNotIn(text, run.error_summary)

    def test_recovery_key_files_are_discouraged_and_guarded(self):
        from django.conf import settings

        from backups.management.commands.restore_backup import Command

        command = Command()
        inside = Path(settings.BASE_DIR) / "recovery-test.txt"
        with self.assertRaisesRegex(CommandError, "inside the application folder"):
            command.read_key_file(str(inside))
        loose = Path(tempfile.mkdtemp()) / "key.txt"
        loose.write_text("x")
        loose.chmod(0o644)
        if os.name == "posix":
            with self.assertRaisesRegex(CommandError, "readable by other users"):
                command.read_key_file(str(loose))
        loose.chmod(0o600)
        command.stderr = io.StringIO()
        self.assertEqual(command.read_key_file(str(loose)), "x")
        self.assertIn("Delete this file now", command.stderr.getvalue())

    def test_the_rotation_command_has_no_recovery_key_file_option(self):
        from backups.management.commands.rotate_backup_key import Command

        text = Command().create_parser("manage.py", "rotate_backup_key").format_help()
        self.assertNotIn("recovery-key-file", text)
        self.assertNotIn("old-recovery", text)
