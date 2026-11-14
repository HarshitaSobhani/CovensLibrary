"""The Google Drive adapter against a fake Drive client. Behaviour against REAL Google is
UNVERIFIED (it needs the owner's OAuth credentials, decision D-29e)."""

import json
import shutil
import tempfile
from pathlib import Path
from unittest import mock

from django.test import TestCase, override_settings

from backups import keystore
from backups.destinations import (
    FOLDER_NAME,
    DestinationError,
    DriveAuthError,
    DriveDestination,
    DriveNetworkError,
    DriveQuotaError,
    LocalDirectoryDestination,
    build_drive_service,
    classify_drive_error,
    get_destination,
)
from core.models import LibrarySetting


class Call:
    def __init__(self, result=None, error=None):
        self.result, self.error = result, error

    def execute(self):
        if self.error:
            raise self.error
        return self.result


class FakeFiles:
    def __init__(self):
        self.created = []
        self.meta = {}
        self.deleted = []
        self.error = None

    def create(self, body, media_body=None, fields=None):
        self.created.append(body)
        if body.get("mimeType") == "application/vnd.google-apps.folder":
            return Call({"id": "FOLDER-1"})
        if self.error:
            return Call(error=self.error)
        return Call({"id": "FILE-1", "size": "123", "sha256Checksum": "abc"})

    def get(self, fileId, fields=None):
        if self.error:
            return Call(error=self.error)
        return Call(self.meta[fileId])

    def delete(self, fileId):
        if isinstance(self.error, Exception):
            return Call(error=self.error)
        self.deleted.append(fileId)
        return Call({})

    def get_media(self, fileId):
        return Call(error=self.error)

    def list(self, q=None, fields=None, pageSize=None):
        self.queries = getattr(self, "queries", []) + [q]
        if self.list_error:
            return Call(error=self.list_error)
        return Call({"files": [{"id": i} for i in self.listing(q)]})

    list_error = None

    def listing(self, q):
        return list(self.stored)

    stored = ()


class FakeService:
    def __init__(self):
        self.f = FakeFiles()

    def files(self):
        return self.f


class DriveAdapterTests(TestCase):
    def setUp(self):
        self.service = FakeService()
        self.drive = DriveDestination(service=self.service)
        self.file = Path(tempfile.mkdtemp()) / "b.cbk"
        self.file.write_bytes(b"x" * 123)
        self.addCleanup(self.file.unlink)

    def test_the_app_creates_its_own_folder_once_and_remembers_it(self):
        self.assertEqual(self.drive.folder(), "FOLDER-1")
        self.assertEqual(LibrarySetting.load().drive_folder_id, "FOLDER-1")
        self.assertEqual(self.service.f.created[0]["name"], FOLDER_NAME)
        again = DriveDestination(service=FakeService())
        self.assertEqual(again.folder(), "FOLDER-1")  # from the setting: no second folder
        self.assertEqual(again.service().f.created, [])

    def test_upload_puts_the_file_in_that_folder(self):
        with mock.patch("googleapiclient.http.MediaFileUpload"):
            remote = self.drive.upload(self.file, "b.cbk")
        self.assertEqual((remote.remote_id, remote.size, remote.sha256), ("FILE-1", 123, "abc"))
        body = [c for c in self.service.f.created if c.get("name") == "b.cbk"][0]
        self.assertEqual(body["parents"], ["FOLDER-1"])

    def test_verification_needs_the_same_size_and_hash_computed_by_drive(self):
        meta = self.service.f.meta
        meta["F"] = {"size": "123", "sha256Checksum": "ABC", "trashed": False}
        self.assertTrue(self.drive.verify("F", 123, "abc"))  # case-insensitive hex
        self.assertFalse(self.drive.verify("F", 124, "abc"))
        self.assertFalse(self.drive.verify("F", 123, "abd"))
        meta["F"] = {"size": "123", "trashed": True, "sha256Checksum": "abc"}
        self.assertFalse(self.drive.verify("F", 123, "abc"))
        meta["F"] = {"size": "123", "md5Checksum": "m"}  # no SHA-256 from Drive: not verified
        self.assertFalse(self.drive.verify("F", 123, "abc"))

    def test_errors_are_wrapped_without_leaking_detail(self):
        self.service.f.error = RuntimeError("token=SECRET-TOKEN-123 quota exceeded")
        with mock.patch("googleapiclient.http.MediaFileUpload"):
            with self.assertRaises(DestinationError) as ctx:
                self.drive.upload(self.file, "b.cbk")
        self.assertNotIn("SECRET-TOKEN", str(ctx.exception))
        with self.assertRaises(DestinationError) as ctx:
            self.drive.verify("F", 1, "x")
        self.assertNotIn("SECRET-TOKEN", str(ctx.exception))
        with self.assertRaises(DestinationError):
            self.drive.delete("F")

    def test_deleting_something_already_gone_is_fine(self):
        class NotFound(Exception):
            resp = mock.Mock(status=404)

        self.service.f.error = NotFound()
        self.drive.delete("F")  # no exception
        self.service.f.error = None
        self.drive.delete("G")
        self.assertEqual(self.service.f.deleted, ["G"])


class HttpError(Exception):
    def __init__(self, status, reason=""):
        self.resp = mock.Mock(status=status)
        self.content = json.dumps({"error": {"errors": [{"reason": reason}]}}).encode()
        super().__init__("secret-token-zzz detail")


class RefreshError(Exception):
    pass


class ErrorClassificationTests(TestCase):
    def classify(self, exc, action="upload"):
        return classify_drive_error(exc, action)

    def test_authorisation_failures_are_told_apart_and_actionable(self):
        for exc in (RefreshError("invalid_grant secret-token-zzz"), HttpError(401), HttpError(403)):
            error = self.classify(exc)
            self.assertIsInstance(error, DriveAuthError)
            self.assertIn("connect_drive", str(error))
            self.assertNotIn("secret-token-zzz", str(error))

    def test_network_and_temporary_failures_are_told_apart(self):
        for exc in (
            ConnectionResetError("secret-token-zzz"),
            TimeoutError(),
            OSError("dns"),
            HttpError(503),
            HttpError(429),
            HttpError(403, "rateLimitExceeded"),
        ):
            error = self.classify(exc)
            self.assertIsInstance(error, DriveNetworkError, repr(exc))
            self.assertIn("retried automatically", str(error))
            self.assertNotIn("secret-token-zzz", str(error))

    def test_a_full_drive_is_reported_as_such(self):
        error = self.classify(HttpError(403, "storageQuotaExceeded"))
        self.assertIsInstance(error, DriveQuotaError)
        self.assertIn("storage is full", str(error))

    def test_anything_else_shows_only_a_status_or_the_type(self):
        self.assertEqual(
            str(self.classify(HttpError(404), "download")),
            "Google Drive download failed (HTTP 404)",
        )
        error = self.classify(ValueError("secret-token-zzz"), "delete")
        self.assertEqual(str(error), "Google Drive delete failed (ValueError)")
        self.assertNotIsInstance(error, (DriveAuthError, DriveNetworkError, DriveQuotaError))

    def test_every_drive_operation_uses_the_classification(self):
        drive = DriveDestination(service=FakeService(), folder_id="F")
        drive.service().f.error = RefreshError("x")
        for call in (
            lambda: drive.verify("a", 1, "x"),
            lambda: drive.delete("a"),
        ):
            with self.assertRaises(DriveAuthError):
                call()
        target = Path(tempfile.mkdtemp()) / "x"
        self.addCleanup(shutil.rmtree, target.parent, ignore_errors=True)
        with mock.patch("googleapiclient.http.MediaIoBaseDownload") as dl:
            dl.return_value.next_chunk.side_effect = RefreshError("x")
            with self.assertRaises(DriveAuthError):
                drive.download("a", target)


class UploadCleanupTests(TestCase):
    def setUp(self):
        self.service = FakeService()
        self.drive = DriveDestination(service=self.service, folder_id="FOLDER")
        self.file = Path(tempfile.mkdtemp()) / "b.cbk"
        self.file.write_bytes(b"x" * 10)
        self.addCleanup(self.file.unlink)
        patcher = mock.patch("googleapiclient.http.MediaFileUpload")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_only_the_partial_file_of_the_failed_attempt_is_removed(self):
        self.service.f.error = ConnectionResetError("secret-token-zzz")
        calls = {"n": 0}

        def staged(q):
            calls["n"] += 1
            return ["OLD"] if calls["n"] == 1 else ["OLD", "PARTIAL"]

        self.service.f.listing = staged
        # delete() must not fail the cleanup: only the upload errors
        self.service.f.delete = lambda fileId: self.service.f.deleted.append(fileId) or Call({})
        with self.assertRaises(DriveNetworkError) as ctx:
            self.drive.upload(self.file, "b.cbk")
        self.assertEqual(self.service.f.deleted, ["PARTIAL"])  # never "OLD"
        self.assertNotIn("also failed", str(ctx.exception))
        self.assertNotIn("secret-token-zzz", str(ctx.exception))

    def test_the_query_is_limited_to_this_name_and_folder_and_escaped(self):
        self.service.f.error = OSError()
        self.service.f.delete = lambda fileId: Call({})
        with self.assertRaises(DriveNetworkError):
            self.drive.upload(self.file, "it's.cbk")
        query = self.service.f.queries[0]
        self.assertIn("name = 'it\\'s.cbk'", query)
        self.assertIn("'FOLDER' in parents", query)
        self.assertIn("trashed = false", query)

    def test_a_cleanup_failure_is_recorded_without_hiding_the_original_error(self):
        self.service.f.error = HttpError(401)
        calls = {"n": 0}

        def staged(q):
            calls["n"] += 1
            return [] if calls["n"] == 1 else ["PARTIAL"]

        self.service.f.listing = staged
        with self.assertRaises(DriveAuthError) as ctx:  # delete fails too (same fake error)
            self.drive.upload(self.file, "b.cbk")
        self.assertIn("connect_drive", str(ctx.exception))  # the upload error is still first
        self.assertIn("Cleaning up the partial upload also failed", str(ctx.exception))
        self.assertEqual(self.service.f.deleted, [])

    def test_if_existing_files_cannot_be_listed_nothing_is_deleted(self):
        self.service.f.error = OSError()
        self.service.f.list_error = OSError()
        with self.assertRaises(DriveNetworkError) as ctx:
            self.drive.upload(self.file, "b.cbk")
        self.assertIn("could not be checked", str(ctx.exception))
        self.assertEqual(self.service.f.deleted, [])

    def test_a_successful_upload_deletes_nothing(self):
        self.service.f.stored = ["OLD"]
        remote = self.drive.upload(self.file, "b.cbk")
        self.assertEqual(remote.remote_id, "FILE-1")
        self.assertEqual(self.service.f.deleted, [])

    def test_the_local_destination_removes_its_own_partial_copy_only(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        local = LocalDirectoryDestination(root)
        (root / "keep.cbk").write_bytes(b"verified")

        def broken_copy(src, dst):
            Path(dst).write_bytes(b"part")
            raise OSError("disk full")

        with mock.patch("backups.destinations.shutil.copyfile", broken_copy):
            with self.assertRaises(OSError):
                local.upload(self.file, "new.cbk")
        self.assertEqual(sorted(p.name for p in root.iterdir()), ["keep.cbk"])
        with self.assertRaises(DestinationError):  # an existing name is refused, not replaced
            local.upload(self.file, "keep.cbk")
        self.assertEqual((root / "keep.cbk").read_bytes(), b"verified")


class ConnectionTests(TestCase):
    def setUp(self):
        keystore.reset_memory_keystore()
        self.addCleanup(keystore.reset_memory_keystore)

    def test_not_connected_is_a_clear_error(self):
        with self.assertRaisesRegex(DestinationError, "not connected"):
            build_drive_service()

    def test_the_service_is_built_from_the_credential_store_only(self):
        store = keystore.get_keystore()
        store.set(keystore.DRIVE_CLIENT, json.dumps({"client_id": "cid", "client_secret": "csec"}))
        store.set(keystore.DRIVE_TOKEN, json.dumps({"refresh_token": "rtok"}))
        with mock.patch("googleapiclient.discovery.build") as build:
            build_drive_service()
        credentials = build.call_args.kwargs["credentials"]
        self.assertEqual((credentials.client_id, credentials.refresh_token), ("cid", "rtok"))
        self.assertEqual(list(credentials.scopes), ["https://www.googleapis.com/auth/drive.file"])

    def test_the_destination_setting(self):
        self.assertIsInstance(get_destination("drive"), DriveDestination)
        self.assertIsInstance(get_destination("local:/tmp/x"), LocalDirectoryDestination)
        for bad in ("local:", "ftp://x", "s3"):
            with self.assertRaises(DestinationError):
                get_destination(bad)
        with override_settings(BACKUP_DESTINATION="local:/tmp/y"):
            self.assertEqual(get_destination().root, Path("/tmp/y"))

    def test_no_google_credentials_are_configured_anywhere_in_settings(self):
        from django.conf import settings

        names = [n for n in dir(settings) if n.isupper()]
        self.assertFalse(
            [
                n
                for n in names
                if "GOOGLE" in n or "OAUTH" in n or "DRIVE" in n.replace("DRIVE_", "DRIVE_")
            ]
        )


class ConnectDriveSecretHandlingTests(TestCase):
    """Found by the security audit: the client secret used to be a required command-line argument
    (visible in the process list and the shell history). It is now asked for, hidden."""

    def setUp(self):
        keystore.reset_memory_keystore()
        self.addCleanup(keystore.reset_memory_keystore)

    def run_command(self, *args, typed_id="typed-id", typed_secret="typed-secret"):
        from django.core.management import call_command

        creds = mock.Mock(refresh_token="refresh-1")
        flow = mock.Mock()
        flow.run_local_server.return_value = creds
        with (
            mock.patch(
                "google_auth_oauthlib.flow.InstalledAppFlow.from_client_config", return_value=flow
            ) as cfg,
            mock.patch("builtins.input", return_value=typed_id) as ask_id,
            mock.patch("getpass.getpass", return_value=typed_secret) as ask_secret,
        ):
            call_command("connect_drive", *args)
        return cfg, ask_id, ask_secret

    def test_with_no_arguments_both_values_are_asked_for_and_the_secret_is_hidden(self):
        cfg, ask_id, ask_secret = self.run_command()
        ask_id.assert_called_once()
        ask_secret.assert_called_once()
        installed = cfg.call_args.args[0]["installed"]
        self.assertEqual(
            (installed["client_id"], installed["client_secret"]), ("typed-id", "typed-secret")
        )
        stored = json.loads(keystore.get_keystore().get(keystore.DRIVE_CLIENT))
        self.assertEqual(stored["client_secret"], "typed-secret")

    def test_arguments_still_work_and_skip_the_prompts(self):
        _, ask_id, ask_secret = self.run_command("--client-id", "cid", "--client-secret", "csec")
        ask_id.assert_not_called()
        ask_secret.assert_not_called()

    def test_the_help_text_discourages_the_command_line_secret(self):
        from backups.management.commands.connect_drive import Command

        text = Command().create_parser("manage.py", "connect_drive").format_help()
        self.assertIn("DISCOURAGED", text)

    def test_empty_answers_are_refused_and_nothing_is_stored(self):
        from django.core.management.base import CommandError

        with self.assertRaises(CommandError):
            self.run_command(typed_id="", typed_secret="")
        self.assertIsNone(keystore.get_keystore().get(keystore.DRIVE_CLIENT))
