import shutil
import tempfile
from datetime import datetime
from zoneinfo import ZoneInfo

from django.test import override_settings

from backups import keystore
from backups.destinations import LocalDirectoryDestination, RemoteFile

IST = ZoneInfo("Asia/Kolkata")


def ist(y, m, d, hh=0, mm=0, ss=0):
    return datetime(y, m, d, hh, mm, ss, tzinfo=IST)


def fresh_key():
    keystore.reset_memory_keystore()
    key = keystore.generate_key()
    keystore.save_working_key(key)
    return key


class TempDirMixin:
    """A throw-away destination folder and temp folder for each test."""

    def make_dirs(self):
        self.dest_dir = tempfile.mkdtemp(prefix="coven-dest-")
        self.tmp_dir = tempfile.mkdtemp(prefix="coven-tmp-")
        self.addCleanup(shutil.rmtree, self.dest_dir, True)
        self.addCleanup(shutil.rmtree, self.tmp_dir, True)
        override = override_settings(BACKUP_TMP_DIR=self.tmp_dir)
        override.enable()
        self.addCleanup(override.disable)
        self.destination = LocalDirectoryDestination(self.dest_dir)


class FakeDestination(LocalDirectoryDestination):
    """Local destination whose behaviour tests can bend."""

    def __init__(self, root, fail_upload=False, lie_on_verify=False, fail_delete=()):
        super().__init__(root)
        self.fail_upload, self.lie_on_verify, self.fail_delete = (
            fail_upload,
            lie_on_verify,
            set(fail_delete),
        )
        self.deleted = []

    def upload(self, path, name):
        if self.fail_upload:
            from backups.destinations import DestinationError

            raise DestinationError("simulated upload failure")
        return super().upload(path, name)

    def verify(self, remote_id, size, sha256):
        return False if self.lie_on_verify else super().verify(remote_id, size, sha256)

    def delete(self, remote_id):
        if remote_id in self.fail_delete:
            from backups.destinations import DestinationError

            raise DestinationError("simulated delete failure")
        self.deleted.append(remote_id)
        super().delete(remote_id)


__all__ = ["RemoteFile", "ist", "fresh_key", "TempDirMixin", "FakeDestination"]


# ---------------------------------------------------------------- disposable databases
import os  # noqa: E402
import uuid  # noqa: E402

import psycopg  # noqa: E402
from django.db import connections  # noqa: E402


def _admin():
    cfg = connections["default"].settings_dict
    return psycopg.connect(
        host=cfg.get("HOST") or "127.0.0.1",
        port=cfg.get("PORT") or 5432,
        dbname="postgres",
        user=cfg["USER"],
        password=cfg.get("PASSWORD") or "",
        autocommit=True,
    )


def create_disposable_database():
    """An EMPTY database owned by the connected (owner) role; the test role has CREATEDB on a
    development machine. Never touches the real library database."""
    name = f"coven_rehearsal_{uuid.uuid4().hex[:10]}"
    with _admin() as admin:
        admin.execute(f'CREATE DATABASE "{name}"')
    return name


def drop_disposable_database(name):
    """Drop a rehearsal database. A backend that is still shutting down can make the first attempt
    fail, so it is retried a few times (test clean-up only)."""
    import time

    last = None
    for _ in range(6):
        try:
            with _admin() as admin:
                admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
            return
        except psycopg.Error as exc:
            last = exc
            time.sleep(0.5)
    raise last


def query(name, sql_text, params=()):
    cfg = connections["default"].settings_dict
    with psycopg.connect(
        host=cfg.get("HOST") or "127.0.0.1", port=cfg.get("PORT") or 5432, dbname=name,
        user=cfg["USER"], password=cfg.get("PASSWORD") or "", autocommit=True,
    ) as pg:  # fmt: skip
        cursor = pg.execute(sql_text, params)
        return cursor.fetchall() if cursor.description else []


_STRUCTURE = {
    "tables": "SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY 1",
    "constraints": (
        "SELECT conrelid::regclass::text, conname, contype FROM pg_constraint "
        "WHERE connamespace='public'::regnamespace ORDER BY 1,2"
    ),
    "indexes": "SELECT indexname FROM pg_indexes WHERE schemaname='public' ORDER BY 1",
    "triggers": (
        "SELECT tgrelid::regclass::text, tgname FROM pg_trigger WHERE NOT tgisinternal ORDER BY 1,2"
    ),
    "functions": (
        "SELECT proname, coalesce(proconfig::text,'') FROM pg_proc "
        "WHERE pronamespace='public'::regnamespace ORDER BY 1"
    ),
    "sequences": (
        "SELECT sequencename, last_value FROM pg_sequences WHERE schemaname='public' ORDER BY 1"
    ),
}


def schema_fingerprint(name):
    """Everything structural that must survive a restore, as comparable lists."""
    return {part: query(name, sql_text) for part, sql_text in _STRUCTURE.items()}


__all__ = [
    *__all__,
    "create_disposable_database",
    "drop_disposable_database",
    "query",
    "schema_fingerprint",
    "os",
]
