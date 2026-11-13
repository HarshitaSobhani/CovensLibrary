"""The plaintext archive that is encrypted: the database dump, media and application assets, and a
manifest describing exactly what is inside (hashes of every file)."""

import hashlib
import json
import os
import subprocess
import tarfile
from contextlib import contextmanager
from pathlib import Path

from django.conf import settings
from django.db import connection
from django.utils import timezone

from .crypto import sha256_file

DUMP_NAME = "database.dump"
MANIFEST_NAME = "manifest.json"
FORMAT_VERSION = 1


class BackupError(Exception):
    """A failure whose message is safe to show and store (no secrets)."""


def scrub(text, *secrets):
    text = str(text)
    for secret in secrets:
        if secret:
            text = text.replace(str(secret), "***")
    return text


def dump_database(target, conn=None, snapshot=None):
    """``pg_dump -Fc`` of the database into ``target``, as the connected role. With a snapshot id
    the dump sees exactly the data the row counts were taken from."""
    cfg = (conn or connection).settings_dict
    password = cfg.get("PASSWORD") or ""
    command = [
        settings.PG_DUMP_PATH,
        "-Fc",
        "--no-owner",
        "--no-acl",
        "-h",
        str(cfg.get("HOST") or "127.0.0.1"),
        "-p",
        str(cfg.get("PORT") or 5432),
        "-U",
        str(cfg["USER"]),
        "-d",
        str(cfg["NAME"]),
        "-f",
        str(target),
    ]
    if snapshot:
        command += ["--snapshot", snapshot]
    env = {**os.environ, "PGPASSWORD": password}
    try:
        done = subprocess.run(  # noqa: S603 - fixed argument list, no shell
            command, env=env, capture_output=True, text=True, timeout=1800, check=False
        )
    except FileNotFoundError as exc:
        raise BackupError("pg_dump was not found (set PG_DUMP_PATH)") from exc
    except subprocess.TimeoutExpired as exc:
        raise BackupError("pg_dump did not finish within 30 minutes") from exc
    if done.returncode != 0:
        detail = scrub(
            done.stderr.strip().splitlines()[-1] if done.stderr.strip() else "", password
        )
        raise BackupError(f"pg_dump failed: {detail[:300]}")
    if not Path(target).exists() or Path(target).stat().st_size == 0:
        raise BackupError("pg_dump produced no data")
    done = subprocess.run(  # noqa: S603
        [settings.PG_DUMP_PATH, "--version"], capture_output=True, text=True, check=False
    )
    return done.stdout.strip()


def _asset_roots():
    roots = []
    media = Path(settings.MEDIA_ROOT)
    assets = Path(settings.BASE_DIR) / "assets"
    for name, path in (("media", media), ("assets", assets)):
        if path.is_dir():
            roots.append((name, path))
    return roots


def migration_list(conn=None):
    with (conn or connection).cursor() as cur:
        cur.execute("SELECT app, name FROM django_migrations ORDER BY app, name")
        return [f"{app}.{name}" for app, name in cur.fetchall()]


def server_version(conn=None):
    with (conn or connection).cursor() as cur:
        cur.execute("SHOW server_version")
        return cur.fetchone()[0]


@contextmanager
def export_snapshot(conn=None):
    """Hold a REPEATABLE READ snapshot open: yields (snapshot id, {table: row count}) so the dump
    and the counts describe the same moment, whatever the application writes meanwhile."""
    import psycopg
    from psycopg import sql

    cfg = (conn or connection).settings_dict
    pg = psycopg.connect(
        host=cfg.get("HOST") or "127.0.0.1",
        port=cfg.get("PORT") or 5432,
        dbname=cfg["NAME"],
        user=cfg["USER"],
        password=cfg.get("PASSWORD") or "",
        autocommit=True,
    )
    try:
        pg.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
        snapshot = pg.execute("SELECT pg_export_snapshot()").fetchone()[0]
        tables = [
            r[0]
            for r in pg.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY 1"
            )
        ]
        counts = {}
        for table in tables:
            query = sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier("public", table))
            counts[table] = pg.execute(query).fetchone()[0]
        yield snapshot, counts
    finally:
        try:
            pg.execute("ROLLBACK")
        finally:
            pg.close()


def build_archive(target, workdir, key_fingerprint, now=None, conn=None):
    """Write the archive (an uncompressed tar: the dump is already compressed) and return the
    manifest. Paths in the tar use forward slashes on every operating system."""
    now = now or timezone.now()
    workdir = Path(workdir)
    dump_path = workdir / DUMP_NAME
    with export_snapshot(conn) as (snapshot, row_counts):
        pg_dump_version = dump_database(dump_path, conn, snapshot)
    files = [
        {"path": DUMP_NAME, "size": dump_path.stat().st_size, "sha256": sha256_file(dump_path)}
    ]
    members = [(DUMP_NAME, dump_path)]
    for root_name, root in _asset_roots():
        for path in sorted(p for p in root.rglob("*") if p.is_file()):
            rel = f"{root_name}/{path.relative_to(root).as_posix()}"
            files.append({"path": rel, "size": path.stat().st_size, "sha256": sha256_file(path)})
            members.append((rel, path))
    manifest = {
        "format_version": FORMAT_VERSION,
        "application": "Coven's Library",
        "app_version": settings.APP_VERSION,
        "created_at": timezone.localtime(now).isoformat(timespec="seconds"),
        "timezone": "Asia/Kolkata",
        "key_fingerprint": key_fingerprint,
        "database": {
            "name": (conn or connection).settings_dict["NAME"],
            "server_version": server_version(conn),
            "pg_dump_version": pg_dump_version,
            "migrations": migration_list(conn),
            "row_counts": row_counts,
        },
        "media_present": any(f["path"].startswith("media/") for f in files),
        "files": files,
    }
    manifest_bytes = json.dumps(manifest, indent=2, ensure_ascii=False).encode("utf-8")
    with tarfile.open(target, "w") as archive:
        for rel, path in members:
            archive.add(path, arcname=rel, recursive=False)
        info = tarfile.TarInfo(MANIFEST_NAME)
        info.size = len(manifest_bytes)
        info.mtime = int(now.timestamp())
        import io

        archive.addfile(info, io.BytesIO(manifest_bytes))
    manifest["archive_sha256"] = hashlib.sha256(Path(target).read_bytes()).hexdigest()
    return manifest
