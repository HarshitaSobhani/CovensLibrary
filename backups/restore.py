"""Restore and replacement-computer recovery (Phase 7b), CLI-first.

Order of work (nothing destructive happens before every check has passed):
 1. obtain the encrypted backup;  2. obtain the key (Credential Manager, or the owner's recovery
 key after an explicit confirmation);  3. decrypt and verify EVERY file against the manifest and
 refuse unsafe archive members;  4. check compatibility (format, PostgreSQL major, migrations);
 5. a safety backup of the database being replaced;  6. restore the database (``pg_restore``) and
 media;  7. grants, migrations if the code is newer, security check;  8. verification;  9. record.

Restoring replaces the target database's contents; it runs as the OWNER role and never creates a
database (the replacement computer provisions an empty one first, docs/DATABASE_SECURITY.md)."""

import os
import re
import shutil
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from django.conf import settings
from django.db import connections
from django.db.migrations.loader import MigrationLoader
from django.utils import timezone

from . import archive, crypto, keystore
from .archive import BackupError, scrub
from .keystore import RecoveryKeyError

SUPPORTED_FORMAT = 1
ALLOWED_PREFIXES = ("media/", "assets/")
ALLOWED_NAMES = (archive.DUMP_NAME, archive.MANIFEST_NAME)
MAX_MEMBERS = 200000


class RestoreError(Exception):
    """A refusal or failure whose message is safe to show and store."""


@dataclass
class Step:
    name: str
    ok: bool
    detail: str = ""


@dataclass
class Result:
    steps: list = field(default_factory=list)
    manifest: dict = field(default_factory=dict)
    verification: dict = field(default_factory=dict)
    pre_restore: dict = field(default_factory=dict)
    key_imported: bool = False
    ok: bool = False

    def step(self, name, ok=True, detail=""):
        self.steps.append(Step(name, ok, detail))


# ---------------------------------------------------------------- the key
def obtain_key(header_fingerprint, *, store=None, recovery_text=None, confirm_import=False):
    """The working key for this backup. Order: the credential store (if its key matches), else
    the recovery key the owner typed. Importing the recovery key into the store needs the
    owner's explicit confirmation. Returns (key, imported)."""
    store = store or keystore.get_keystore()
    stored = keystore.load_working_key(store)
    if stored is not None and crypto.fingerprint(stored)[:16] == header_fingerprint:
        return stored, False
    if recovery_text is None:
        raise RestoreError(
            "This backup was made with a key that is not in the credential store "
            f"(fingerprint {header_fingerprint}). Provide the recovery key."
        )
    try:
        key = keystore.parse_recovery_key(recovery_text)
    except RecoveryKeyError as exc:
        raise RestoreError(str(exc)) from exc
    if crypto.fingerprint(key)[:16] != header_fingerprint:
        raise RestoreError(
            "The recovery key is valid but belongs to a different key than this backup "
            f"(this backup needs fingerprint {header_fingerprint})."
        )
    if not confirm_import:
        raise RestoreError(
            "Restoring the key into the credential store needs the owner's confirmation."
        )
    keystore.save_working_key(key, store)
    if keystore.load_working_key(store) != key:
        raise RestoreError(
            "The key was saved but could not be read back from the credential store."
        )
    return key, True


# ---------------------------------------------------------------- the archive
def _check_member_name(name):
    path = PurePosixPath(name)
    if (
        not name
        or name.startswith("/")
        or "\\" in name
        or "\x00" in name
        or any(part in ("", ".", "..") for part in path.parts)
        or re.match(r"^[A-Za-z]:", name)
    ):
        raise RestoreError(f"the backup contains an unsafe path: {name!r}")
    if name not in ALLOWED_NAMES and not name.startswith(ALLOWED_PREFIXES):
        raise RestoreError(f"the backup contains an unexpected file: {name!r}")


def open_backup(encrypted, key, workdir):
    """Decrypt, safely extract and verify. Returns (manifest, {name: Path}). Raises RestoreError
    on a damaged archive, an unsafe member or any hash that differs from the manifest."""
    workdir = Path(workdir)
    plain = workdir / "archive.tar"
    try:
        crypto.decrypt_file(encrypted, plain, key)
    except crypto.DecryptionError as exc:
        raise RestoreError(str(exc)) from exc
    files = {}
    root = workdir / "x"
    root.mkdir()
    try:
        with tarfile.open(plain) as tar:
            members = tar.getmembers()
            if len(members) > MAX_MEMBERS:
                raise RestoreError("the backup contains too many files")
            seen = set()
            for member in members:
                _check_member_name(member.name)
                if not member.isreg():
                    raise RestoreError(f"the backup contains a non-file entry: {member.name!r}")
                if member.name in seen:
                    raise RestoreError(f"the backup lists {member.name!r} twice")
                seen.add(member.name)
                target = root / member.name
                target.parent.mkdir(parents=True, exist_ok=True)
                with tar.extractfile(member) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                files[member.name] = target
    except tarfile.TarError as exc:
        raise RestoreError("the decrypted backup is not a valid archive") from exc
    finally:
        plain.unlink(missing_ok=True)
    if archive.MANIFEST_NAME not in files or archive.DUMP_NAME not in files:
        raise RestoreError("the backup has no manifest or no database dump")
    import json

    try:
        manifest = json.loads(files[archive.MANIFEST_NAME].read_text("utf-8"))
    except ValueError as exc:
        raise RestoreError("the backup manifest is damaged") from exc
    listed = {entry["path"]: entry for entry in manifest.get("files", [])}
    if set(listed) != set(files) - {archive.MANIFEST_NAME}:
        raise RestoreError("the files in the backup do not match its manifest")
    for name, entry in listed.items():
        if (
            files[name].stat().st_size != entry["size"]
            or crypto.sha256_file(files[name]) != entry["sha256"]
        ):
            raise RestoreError(f"{name} does not match the manifest (hash or size differs)")
    return manifest, files


# ---------------------------------------------------------------- compatibility
def server_major(conn):
    with conn.cursor() as cur:
        cur.execute("SHOW server_version_num")
        return int(cur.fetchone()[0]) // 10000


def code_migrations():
    loader = MigrationLoader(None, ignore_no_migrations=True)
    return {f"{app}.{name}" for app, name in loader.disk_migrations}


def check_compatibility(manifest, conn):
    """Returns {'migrate_needed': bool, 'notes': [...]}; raises RestoreError when restoring would
    be unsafe."""
    if manifest.get("application") != "Coven's Library":
        raise RestoreError("this is not a Coven's Library backup")
    if manifest.get("format_version") != SUPPORTED_FORMAT:
        raise RestoreError(f"unsupported backup format {manifest.get('format_version')!r}")
    db = manifest.get("database", {})
    try:
        backup_major = int(str(db.get("server_version", "")).split(".")[0])
    except ValueError as exc:
        raise RestoreError("the backup does not record its PostgreSQL version") from exc
    current = server_major(conn)
    notes = []
    if backup_major > current:
        raise RestoreError(
            f"the backup was made with PostgreSQL {backup_major}; this server is {current}. "
            "Install the same or a newer PostgreSQL major version first."
        )
    if backup_major < current:
        notes.append(f"PostgreSQL major version differs (backup {backup_major}, server {current})")
    known = code_migrations()
    unknown = sorted(set(db.get("migrations", [])) - known)
    if unknown:
        raise RestoreError(
            "the backup comes from a NEWER application version (unknown migrations such as "
            f"{unknown[0]}). Install the matching or a newer application first."
        )
    newer = sorted(known - set(db.get("migrations", [])))
    if newer:
        notes.append(f"{len(newer)} newer migration(s) will be applied after the restore")
    return {"migrate_needed": bool(newer), "notes": notes}


# ---------------------------------------------------------------- the database
def table_count(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM pg_tables WHERE schemaname = 'public'")
        return cur.fetchone()[0]


def _env(conn):
    return {**os.environ, "PGPASSWORD": conn.settings_dict.get("PASSWORD") or ""}


def _pg_args(conn, name):
    cfg = conn.settings_dict
    return [
        "-h",
        str(cfg.get("HOST") or "127.0.0.1"),
        "-p",
        str(cfg.get("PORT") or 5432),
        "-U",
        str(cfg["USER"]),
        "-d",
        name,
    ]


def pg_restore_path():
    configured = Path(settings.PG_DUMP_PATH)
    if configured.name.startswith("pg_dump") and configured.parent != Path("."):
        candidate = configured.with_name(configured.name.replace("pg_dump", "pg_restore"))
        if candidate.exists():
            return str(candidate)
    return "pg_restore"


def run_pg_restore(dump, conn):
    password = conn.settings_dict.get("PASSWORD") or ""
    listing = subprocess.run(  # noqa: S603 - fixed arguments
        [pg_restore_path(), "--list", str(dump)], capture_output=True, text=True, check=False
    )
    if listing.returncode != 0:
        raise RestoreError("the database dump is not readable by pg_restore")
    command = [
        pg_restore_path(), "--no-owner", "--no-acl", "--exit-on-error", "--single-transaction",
        *_pg_args(conn, conn.settings_dict["NAME"]), str(dump),
    ]  # fmt: skip
    try:
        done = subprocess.run(  # noqa: S603
            command, env=_env(conn), capture_output=True, text=True, timeout=3600, check=False
        )
    except FileNotFoundError as exc:
        raise RestoreError("pg_restore was not found (set PG_DUMP_PATH next to it)") from exc
    if done.returncode != 0:
        detail = scrub(
            done.stderr.strip().splitlines()[-1] if done.stderr.strip() else "", password
        )
        raise RestoreError(f"pg_restore failed: {detail[:300]}")


def wipe_database(conn):
    """Remove everything the owner role owns in this database (the old contents)."""
    with conn.cursor() as cur:
        cur.execute("DROP OWNED BY CURRENT_USER CASCADE")


# ---------------------------------------------------------------- media and assets
def restore_media(files, replace_existing=True):
    """Put ``media/`` back exactly as backed up; keep application ``assets/`` that already exist
    (they ship with the code) and only report differences. Returns a summary dict."""
    media_root = Path(settings.MEDIA_ROOT)
    media = {n: p for n, p in files.items() if n.startswith("media/")}
    summary = {"media_files": len(media), "assets_missing_restored": 0, "assets_different": []}
    if media or media_root.exists():
        if media_root.exists() and replace_existing:
            for child in media_root.iterdir():
                shutil.rmtree(child) if child.is_dir() else child.unlink()
        for name, source in media.items():
            target = media_root / name[len("media/") :]
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
    assets_root = Path(settings.BASE_DIR) / "assets"
    for name, source in files.items():
        if not name.startswith("assets/"):
            continue
        target = assets_root / name[len("assets/") :]
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            summary["assets_missing_restored"] += 1
        elif crypto.sha256_file(target) != crypto.sha256_file(source):
            summary["assets_different"].append(name)
    return summary


# ---------------------------------------------------------------- verification
def row_counts(conn):
    from psycopg import sql

    counts = {}
    with conn.cursor() as cur:
        cur.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY 1")
        for (table,) in cur.fetchall():
            cur.execute(
                sql.SQL("SELECT count(*) FROM {}")
                .format(sql.Identifier("public", table))
                .as_string()
            )
            counts[table] = cur.fetchone()[0]
    return counts


def verify_counts(manifest, conn):
    """Every table the backup recorded must hold exactly the rows it held."""
    expected = manifest["database"].get("row_counts", {})
    actual = row_counts(conn)
    wrong = {t: (n, actual.get(t)) for t, n in expected.items() if actual.get(t) != n}
    return {"ok": not wrong, "tables": len(expected), "mismatches": wrong}


def verify_functional(manifest, conn, runtime_role):
    """Checks on the restored database that need no application state."""
    from core import dbsecurity

    out = {}
    with conn.cursor() as cur:
        cur.execute("SELECT app || '.' || name FROM django_migrations")
        have = {r[0] for r in cur.fetchall()}
        missing = sorted(set(manifest["database"]["migrations"]) - have)
        out["migrations_present"] = {"ok": not missing, "missing": missing[:5]}
        cur.execute(
            "SELECT count(*) FROM finance_financialtransaction t WHERE t.cash_flow IN ('in','out') "
            "AND t.amount_paise <> COALESCE((SELECT sum(p.amount_paise) "
            "FROM finance_paymententry p "
            "WHERE p.transaction_id = t.id), 0)"
        )
        bad = cur.fetchone()[0]
        out["payment_sums_match"] = {"ok": bad == 0, "mismatches": bad}
        cur.execute(
            "SELECT count(*) FROM (SELECT cycle_id FROM finance_tokenledgerentry GROUP BY cycle_id "
            "HAVING sum(delta_paise) < 0) x"
        )
        negative = cur.fetchone()[0]
        out["token_balances_not_negative"] = {"ok": negative == 0, "cycles": negative}
    findings = dbsecurity.inspect(conn, runtime_role, "owner")
    out["database_security"] = {"ok": not findings, "findings": [str(f) for f in findings[:5]]}
    return out


# ---------------------------------------------------------------- the whole restore
def restore(**kwargs):
    """Restore a backup (see ``_restore``). Holds the maintenance lock for the whole restore so
    that backup retention cannot run against the database while it is being replaced."""
    from . import locks

    conn = kwargs["conn"]
    if not locks.try_lock(conn):
        raise RestoreError(
            "backup retention (or another restore) is using this database right now; "
            "nothing was changed - try again in a minute"
        )
    try:
        return _restore(**kwargs)
    finally:
        locks.unlock(conn)


def _restore(
    *,
    encrypted,
    conn,
    alias,
    store=None,
    recovery_text=None,
    confirm_key_import=False,
    replace=False,
    safety_destination=None,
    skip_safety_backup=False,
    now=None,
):
    """Restore ``encrypted`` into the database behind ``conn``/``alias``. Returns a Result;
    raises RestoreError for every refusal (before anything is changed unless stated)."""
    from django.core.management import call_command

    result = Result()
    started = now or timezone.now()
    encrypted = Path(encrypted)
    if settings.DB_ROLE != "owner":
        raise RestoreError(
            "restore needs the database OWNER role (use --settings=config.settings.migrate)"
        )
    try:
        _, _, fingerprint, _ = crypto.read_header(encrypted)
    except crypto.DecryptionError as exc:
        raise RestoreError(str(exc)) from exc
    result.step("backup file recognised", True, f"key fingerprint {fingerprint}")
    key, imported = obtain_key(
        fingerprint, store=store, recovery_text=recovery_text, confirm_import=confirm_key_import
    )
    result.key_imported = imported
    result.step(
        "key available",
        True,
        "restored into the credential store" if imported else "from the credential store",
    )
    with tempfile.TemporaryDirectory(
        prefix="coven-restore-", dir=settings.BACKUP_TMP_DIR or None
    ) as work:
        manifest, files = open_backup(encrypted, key, work)
        result.manifest = manifest
        result.step(
            "backup decrypted and every file verified against the manifest",
            True,
            f"{len(files) - 1} files",
        )
        compat = check_compatibility(manifest, conn)
        result.step("compatible", True, "; ".join(compat["notes"]) or "same versions")
        populated = table_count(conn) > 0
        if populated and not replace:
            raise RestoreError(
                "the target database is not empty; confirm replacing it with --replace"
            )
        if populated:
            if skip_safety_backup:
                result.step("safety backup", True, "SKIPPED at the owner's explicit request")
            else:
                from .models import BackupRun
                from .services import run_backup

                if safety_destination is None:
                    raise RestoreError(
                        "a safety backup of the existing database needs a destination"
                    )
                run = run_backup(
                    trigger="pre_restore", kind="pre_restore",
                    destination=safety_destination, store=store,
                    conn=conn, using=alias,
                )  # fmt: skip
                if run.status != BackupRun.Status.SUCCEEDED:
                    raise RestoreError(
                        f"the safety backup failed, nothing was changed: {run.error_summary}"
                    )
                result.pre_restore = {
                    "file_name": run.file_name, "sha256": run.sha256, "size_bytes": run.size_bytes,
                    "destination": run.destination, "remote_id": run.remote_id,
                    "key_fingerprint": run.key_fingerprint,
                    "started_at": run.started_at.isoformat(),
                }  # fmt: skip
                result.step("safety backup of the existing database", True, run.file_name)
            wipe_database(conn)
            result.step("old contents removed", True)
        else:
            result.step("safety backup", True, "not needed: the target database is empty")
        run_pg_restore(files[archive.DUMP_NAME], conn)
        result.step("database restored", True)
        counts = verify_counts(manifest, conn)
        result.verification["row_counts"] = counts
        result.step("row counts match the backup", counts["ok"], f"{counts['tables']} tables")
        media = restore_media(files)
        result.verification["media"] = media
        result.step("media restored", True, f"{media['media_files']} files")
    from core import dbsecurity

    with conn.cursor():
        pass
    dbsecurity.apply_grants(conn, settings.DB_RUNTIME_ROLE)
    result.step("runtime grants applied", True)
    if compat["migrate_needed"]:
        call_command("migrate", database=alias, verbosity=0, interactive=False)
        result.step("newer migrations applied", True)
    functional = verify_functional(manifest, conn, settings.DB_RUNTIME_ROLE)
    result.verification.update(functional)
    for name, outcome in functional.items():
        result.step(name.replace("_", " "), outcome["ok"])
    result.ok = all(s.ok for s in result.steps)
    _record(result, conn, alias, encrypted, started)
    return result


def settle_running_rows(alias, encrypted, manifest, now):
    """The dump was taken while its own run was 'running', so the restored history holds that row
    in the running state (it would block every new backup). It IS the backup file being restored:
    complete it from that file. Any other running row is closed as interrupted."""
    from .models import BackupRun

    running = list(BackupRun.objects.using(alias).filter(status=BackupRun.Status.RUNNING))
    if not running:
        return 0
    created = manifest.get("created_at", "")
    own = None
    if created:
        from datetime import datetime

        taken = datetime.fromisoformat(created)
        own = min(running, key=lambda r: abs((r.started_at - taken).total_seconds()))
    for run in running:
        if run is own:
            run.status = BackupRun.Status.SUCCEEDED
            run.finished_at = now
            run.verified_at = now
            run.file_name, run.remote_id = encrypted.name, encrypted.name
            run.size_bytes = encrypted.stat().st_size
            run.sha256 = crypto.sha256_file(encrypted)
            run.key_fingerprint = manifest.get("key_fingerprint", "")
            run.destination = "restored"
            import copy

            run.manifest = copy.deepcopy({k: v for k, v in manifest.items() if k != "files"})
            run.manifest.get("database", {}).pop("row_counts", None)
        else:
            run.status = BackupRun.Status.FAILED
            run.finished_at = now
            run.error_summary = "interrupted (in progress when the restored backup was taken)"
        run.save(using=alias)
    return len(running)


def _record(result, conn, alias, encrypted, started):
    """Write the RestoreRun (and the safety backup's BackupRun) into the RESTORED database."""
    from .models import BackupRun, RestoreRun

    settle_running_rows(alias, encrypted, result.manifest, started)

    manifest = result.manifest
    RestoreRun.objects.using(alias).create(
        backup_file_name=encrypted.name,
        backup_sha256=crypto.sha256_file(encrypted),
        key_fingerprint=manifest.get("key_fingerprint", ""),
        target_database=conn.settings_dict["NAME"],
        status=RestoreRun.Status.SUCCEEDED if result.ok else RestoreRun.Status.VERIFICATION_FAILED,
        started_at=started,
        pre_restore=result.pre_restore,
        verification=result.verification,
        key_imported=result.key_imported,
    )
    pre = result.pre_restore
    if pre:  # the history of the replaced database is gone: keep the safety backup protected
        BackupRun.objects.using(alias).create(
            kind=BackupRun.Kind.PRE_RESTORE, trigger=BackupRun.Trigger.PRE_RESTORE,
            status=BackupRun.Status.SUCCEEDED, started_at=started, finished_at=started,
            verified_at=started, file_name=pre["file_name"], size_bytes=pre["size_bytes"],
            sha256=pre["sha256"], key_fingerprint=pre["key_fingerprint"],
            destination=pre["destination"], remote_id=pre["remote_id"],
        )  # fmt: skip
    from audit.models import AuditEvent

    AuditEvent.objects.using(alias).create(
        action="backup.restored",
        entity_type="backup",
        entity_id=encrypted.name[:80],
        summary={
            "ok": result.ok,
            "tables": len(manifest.get("database", {}).get("row_counts", {})),
            "key_imported": result.key_imported,
        },
    )


RESTORE_ALIAS = "restore_target"


def database_alias(name):
    """A Django connection alias for another database on the same server (same credentials).
    One fixed alias is re-pointed at the requested database, so it can be declared up front."""
    default = connections.databases["default"]
    if name == default["NAME"]:
        return "default"
    if RESTORE_ALIAS in connections.databases:
        try:
            connections[RESTORE_ALIAS].close()
        except Exception:  # noqa: BLE001
            pass
        connections.databases[RESTORE_ALIAS]["NAME"] = name
    else:
        connections.databases[RESTORE_ALIAS] = {**default, "NAME": name}
    return RESTORE_ALIAS


__all__ = ["restore", "RestoreError", "Result", "BackupError", "database_alias"]
