"""Where encrypted backups are stored. ``LocalDirectoryDestination`` is used by tests and
rehearsals; ``DriveDestination`` talks to Google Drive with the ``drive.file`` scope in a folder the
application created itself (decision D-29a). Both can verify what was stored, not just that an
upload returned."""

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from django.conf import settings

from .crypto import sha256_file
from .keystore import DRIVE_CLIENT, DRIVE_TOKEN, KeyStoreError, get_keystore

FOLDER_NAME = "Coven's Library Backups"
SCOPES = ["https://www.googleapis.com/auth/drive.file"]


class DestinationError(Exception):
    """A storage problem whose message is safe to show and store."""


class DriveAuthError(DestinationError):
    """Google refused or no longer accepts the stored authorisation."""


class DriveNetworkError(DestinationError):
    """Google Drive could not be reached, or answered with a temporary failure."""


class DriveQuotaError(DestinationError):
    """The Drive account has no room left."""


AUTH_MESSAGE = (
    "Google Drive no longer accepts the saved authorisation (it was revoked, expired or "
    "refused). Run connect_drive to authorise again."
)
NETWORK_MESSAGE = (
    "Could not reach Google Drive (network problem or a temporary Google error). Check the "
    "internet connection; the backup is retried automatically."
)
QUOTA_MESSAGE = "Google Drive storage is full. Free space in the Drive account, then retry."
_QUOTA_REASONS = {"storageQuotaExceeded", "quotaExceeded"}
_RATE_REASONS = {"rateLimitExceeded", "userRateLimitExceeded", "backendError"}


def _http_status(exc):
    status = getattr(getattr(exc, "resp", None), "status", None)
    try:
        return int(status) if status is not None else None
    except (TypeError, ValueError):
        return None


def _http_reasons(exc):
    """The machine-readable error reasons of a Google HTTP error (never the free text)."""
    import json

    try:
        body = json.loads(getattr(exc, "content", b"") or b"{}")
        return {e.get("reason", "") for e in body.get("error", {}).get("errors", [])}
    except Exception:  # noqa: BLE001
        return set()


def classify_drive_error(exc, action):
    """Turn any exception from the Google client into a DestinationError whose message is
    actionable and safe: it never carries tokens, credentials or the exception's own text."""
    names = {cls.__name__ for cls in type(exc).__mro__}
    status = _http_status(exc)
    reasons = _http_reasons(exc)
    if "RefreshError" in names or status == 401:
        return DriveAuthError(AUTH_MESSAGE)
    if reasons & _QUOTA_REASONS:
        return DriveQuotaError(QUOTA_MESSAGE)
    if status == 403 and not reasons & _RATE_REASONS:
        return DriveAuthError(AUTH_MESSAGE)
    if (
        isinstance(exc, OSError)  # ConnectionError, TimeoutError, socket errors
        or names & {"TransportError", "ServerNotFoundError", "HttpLib2Error"}
        or status in (408, 429)
        or (status is not None and status >= 500)
        or reasons & _RATE_REASONS
    ):
        return DriveNetworkError(NETWORK_MESSAGE)
    if status is not None:
        return DestinationError(f"Google Drive {action} failed (HTTP {status})")
    return DestinationError(f"Google Drive {action} failed ({type(exc).__name__})")


@dataclass
class RemoteFile:
    remote_id: str
    size: int
    sha256: str = ""
    name: str = ""


@dataclass
class RemoteEntry:
    """One item found while enumerating a destination (a backup or something unexpected)."""

    name: str
    remote_id: str
    is_folder: bool = False
    trashed: bool = False
    size: int | None = None


class LocalDirectoryDestination:
    kind = "local"

    def __init__(self, root):
        self.root = Path(root)

    def label(self):
        return "local"

    def upload(self, path, name):
        self.root.mkdir(parents=True, exist_ok=True)
        target = self.root / name
        if target.exists():
            raise DestinationError(f"{name} already exists at the destination")
        try:
            shutil.copyfile(path, target)
        except BaseException:
            target.unlink(missing_ok=True)  # only this attempt's own, just-created file
            raise
        return RemoteFile(name, target.stat().st_size, name=name)

    def verify(self, remote_id, size, sha256):
        """True only when the stored file has exactly this size and SHA-256."""
        target = self.root / remote_id
        return target.is_file() and target.stat().st_size == size and sha256_file(target) == sha256

    def download(self, remote_id, path):
        source = self.root / remote_id
        if not source.is_file():
            raise DestinationError("the backup file is not at the destination")
        shutil.copyfile(source, path)

    def delete(self, remote_id):
        try:
            (self.root / remote_id).unlink()
        except FileNotFoundError:
            pass

    def list(self):
        return sorted(p.name for p in self.root.glob("*.cbk")) if self.root.is_dir() else []

    def enumerate_files(self):
        """EVERY entry of the backup folder, or DestinationError. Never 'empty because it could not
        look': a missing folder, a permission problem or a read error is an error."""
        try:
            if not self.root.is_dir():
                raise DestinationError("the backup folder does not exist or is not a folder")
            entries = []
            with os.scandir(self.root) as scan:
                for item in scan:
                    is_folder = item.is_dir(follow_symlinks=False)
                    entries.append(
                        RemoteEntry(
                            item.name,
                            item.name,
                            is_folder=is_folder,
                            size=None if is_folder else item.stat(follow_symlinks=False).st_size,
                        )
                    )
        except DestinationError:
            raise
        except OSError as exc:
            raise DestinationError(
                f"the backup folder could not be read ({type(exc).__name__})"
            ) from exc
        return sorted(entries, key=lambda e: e.name)

    def read_head(self, entry, count):
        """The first ``count`` bytes of a file, or DestinationError."""
        try:
            with open(self.root / entry.remote_id, "rb") as handle:
                return handle.read(count)
        except OSError as exc:
            raise DestinationError(
                f"{entry.name} could not be read ({type(exc).__name__})"
            ) from exc


class DriveDestination:
    """Google Drive, ``drive.file`` scope, application-created folder. NOT verified against real
    Google yet: it needs OAuth client credentials from the owner (decision D-29e)."""

    kind = "drive"

    def __init__(self, service=None, folder_id=None):
        self._service = service
        self._folder_id = folder_id

    def label(self):
        return "drive"

    # -- connection -------------------------------------------------------------------
    def service(self):
        if self._service is None:
            self._service = build_drive_service()
        return self._service

    def folder(self):
        from core.models import LibrarySetting

        if self._folder_id:
            return self._folder_id
        setting = LibrarySetting.load()
        if setting.drive_folder_id:
            self._folder_id = setting.drive_folder_id
            return self._folder_id
        created = (
            self.service()
            .files()
            .create(
                body={"name": FOLDER_NAME, "mimeType": "application/vnd.google-apps.folder"},
                fields="id",
            )
            .execute()
        )
        setting.drive_folder_id = created["id"]
        setting.save(update_fields=["drive_folder_id"])
        self._folder_id = created["id"]
        return self._folder_id

    # -- operations -------------------------------------------------------------------
    def upload(self, path, name):
        from googleapiclient.http import MediaFileUpload

        media = MediaFileUpload(str(path), mimetype="application/octet-stream", resumable=True)
        before = self._ids_named(name)  # None when it could not be checked
        try:
            created = (
                self.service()
                .files()
                .create(
                    body={"name": name, "parents": [self.folder()]},
                    media_body=media,
                    fields="id,size,sha256Checksum,md5Checksum",
                )
                .execute()
            )
        except DestinationError:
            raise
        except Exception as exc:  # noqa: BLE001
            error = classify_drive_error(exc, "upload")
            note = self._cleanup_failed_upload(name, before)
            if note:
                error = type(error)(f"{error} {note}")
            raise error from exc
        return RemoteFile(
            created["id"], int(created.get("size", 0)), created.get("sha256Checksum", ""), name
        )

    def _ids_named(self, name):
        """Ids of the files in the backup folder with exactly this name, or None if unknown."""
        escaped = name.replace("\\", "\\\\").replace("'", "\\'")
        try:
            found = (
                self.service()
                .files()
                .list(
                    q=f"name = '{escaped}' and '{self.folder()}' in parents and trashed = false",
                    fields="files(id)",
                    pageSize=100,
                )
                .execute()
            )
        except Exception:  # noqa: BLE001
            return None
        return {f["id"] for f in found.get("files", [])}

    def _cleanup_failed_upload(self, name, before):
        """Best effort: remove only a file with this attempt's unique name that did not exist
        before the attempt (the outcome of a failed upload is uncertain). Never touches other
        files. Returns a short note when cleanup failed or could not be checked, else ''."""
        if before is None:
            return "(A partial upload could not be checked for and may remain in the folder.)"
        try:
            after = self._ids_named(name)
            if after is None:
                raise DestinationError("listing failed")
            for remote_id in sorted(after - before):
                self.service().files().delete(fileId=remote_id).execute()
        except Exception:  # noqa: BLE001
            return "(Cleaning up the partial upload also failed; it may remain in the folder.)"
        return ""

    def known_folder(self):
        """The backup folder's id WITHOUT creating anything, or None when it is not known."""
        from core.models import LibrarySetting

        return self._folder_id or LibrarySetting.load().drive_folder_id or None

    def enumerate_files(self):
        """EVERY item of the backup folder (trashed ones too: a trashed backup is still
        restorable), following every page. An incomplete listing is an error, never a result."""
        folder = self.known_folder()
        if not folder:
            raise DestinationError("the Drive backup folder is not known; it cannot be listed")
        entries, token, seen, pages = [], None, set(), 0
        while True:
            pages += 1
            if pages > 500:
                raise DestinationError("the Drive listing has too many pages to be trusted")
            try:
                reply = (
                    self.service()
                    .files()
                    .list(
                        q=f"'{folder}' in parents",
                        fields="nextPageToken, incompleteSearch, "
                        "files(id, name, size, mimeType, trashed)",
                        pageSize=1000,
                        pageToken=token,
                        spaces="drive",
                    )
                    .execute()
                )
            except DestinationError:
                raise
            except Exception as exc:  # noqa: BLE001
                raise classify_drive_error(exc, "listing") from exc
            if reply.get("incompleteSearch"):
                raise DestinationError("Google Drive reported an incomplete listing")
            for item in reply.get("files", []):
                entries.append(
                    RemoteEntry(
                        item.get("name", ""),
                        item["id"],
                        is_folder=item.get("mimeType") == "application/vnd.google-apps.folder",
                        trashed=bool(item.get("trashed")),
                        size=int(item["size"]) if item.get("size") is not None else None,
                    )
                )
            token = reply.get("nextPageToken")
            if not token:
                return sorted(entries, key=lambda e: (e.name, e.remote_id))
            if token in seen:
                raise DestinationError("the Drive listing did not advance; it cannot be trusted")
            seen.add(token)

    def read_head(self, entry, count):
        """The first ``count`` bytes of a file (a ranged download), or DestinationError."""
        try:
            request = self.service().files().get_media(fileId=entry.remote_id)
            request.headers["Range"] = f"bytes=0-{count - 1}"
            data = request.execute()
        except DestinationError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise classify_drive_error(exc, "download") from exc
        if not isinstance(data, (bytes, bytearray)):
            raise DestinationError(f"{entry.name} could not be read")
        return bytes(data[:count])

    def verify(self, remote_id, size, sha256):
        """Compare the size and the SHA-256 (or MD5 when Drive offers no SHA-256) that Drive
        itself computed for the stored file with the local values."""
        try:
            meta = (
                self.service()
                .files()
                .get(fileId=remote_id, fields="size,sha256Checksum,md5Checksum,trashed")
                .execute()
            )
        except Exception as exc:  # noqa: BLE001
            raise classify_drive_error(exc, "verification") from exc
        if meta.get("trashed") or int(meta.get("size", -1)) != size:
            return False
        if meta.get("sha256Checksum"):
            return meta["sha256Checksum"].lower() == sha256.lower()
        return False  # without a Drive-computed hash the upload is not considered verified

    def download(self, remote_id, path):
        from googleapiclient.http import MediaIoBaseDownload

        try:
            request = self.service().files().get_media(fileId=remote_id)
            with open(path, "wb") as handle:
                downloader = MediaIoBaseDownload(handle, request, chunksize=8 * 1024 * 1024)
                done = False
                while not done:
                    _, done = downloader.next_chunk()
        except Exception as exc:  # noqa: BLE001
            raise classify_drive_error(exc, "download") from exc

    def delete(self, remote_id):
        try:
            self.service().files().delete(fileId=remote_id).execute()
        except Exception as exc:  # noqa: BLE001
            if getattr(getattr(exc, "resp", None), "status", None) == 404:
                return
            raise classify_drive_error(exc, "delete") from exc


def build_drive_service():
    """An authorised Drive client from the credentials in the credential store."""
    import json

    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    store = get_keystore()
    try:
        token = store.get(DRIVE_TOKEN)
        client = store.get(DRIVE_CLIENT)
    except KeyStoreError as exc:
        raise DestinationError(str(exc)) from exc
    if not token or not client:
        raise DestinationError("Google Drive is not connected (run connect_drive)")
    info, conf = json.loads(token), json.loads(client)
    credentials = Credentials(
        token=None,
        refresh_token=info["refresh_token"],
        token_uri="https://oauth2.googleapis.com/token",
        client_id=conf["client_id"],
        client_secret=conf["client_secret"],
        scopes=SCOPES,
    )
    return build("drive", "v3", credentials=credentials, cache_discovery=False)


def get_destination(spec=None):
    """``spec`` is ``"drive"`` or ``"local:<folder>"``; default from settings."""
    spec = spec or settings.BACKUP_DESTINATION
    if spec == "drive":
        return DriveDestination()
    if spec.startswith("local:") and len(spec) > 6:
        return LocalDirectoryDestination(spec[6:])
    raise DestinationError("BACKUP_DESTINATION must be 'drive' or 'local:<folder>'")
