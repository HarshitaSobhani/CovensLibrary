"""Backup runs, health, catch-up and retention (Phase 7a).

Pipeline (PRD section 10): ``pg_dump -Fc`` + media + manifest -> tar -> AES-256-GCM -> SHA-256 ->
upload -> VERIFY what was stored -> record -> retention. Every outcome is a ``BackupRun`` row; a
failure never raises into the caller (the daily job must go on) but is stored, audited and shown
on the dashboard."""

import tempfile
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from audit.services import record
from core.models import LibrarySetting

from . import archive, crypto
from .destinations import DestinationError, get_destination
from .keystore import KeyStoreError, load_working_key
from .models import BackupRun

DAILY_KEEP = 30
MONTHLY_KEEP = 12
STALE_AFTER = timedelta(hours=26)  # B38: warn when the last success is older than this
RETRY_AFTER = timedelta(minutes=30)  # assumption: a failed attempt is not retried sooner
INTERRUPTED_AFTER = timedelta(hours=4)  # assumption: "running" this long means it died
CATCHUP_GRACE = timedelta(minutes=10)  # started later than this after the slot = catch-up

Status = BackupRun.Status


class BackupBusy(Exception):
    """Another backup is already running."""


# ------------------------------------------------------------------ helpers
def _error_text(exc):
    text = (
        f"{type(exc).__name__}: {exc}"
        if not isinstance(exc, (archive.BackupError, DestinationError, KeyStoreError))
        else str(exc)
    )
    return archive.scrub(text, settings.DATABASES["default"].get("PASSWORD"))[:480]


def _local(dt):
    return timezone.localtime(dt)


def _audit(using, action, summary, run=None):
    from audit.models import AuditEvent

    AuditEvent.objects.using(using).create(
        action=action,
        entity_type="backup",
        entity_id=str(run.pk) if run else "",
        summary=summary,
    )


def fail_interrupted(now, using="default"):
    """A run stuck in 'running' (the application died) is closed as failed."""
    stale = BackupRun.objects.using(using).filter(
        status=Status.RUNNING, started_at__lt=now - INTERRUPTED_AFTER
    )
    for run in stale:
        run.status = Status.FAILED
        run.finished_at = now
        run.error_summary = "interrupted (the application stopped during the backup)"
        run.save(using=using)


def _finish_failed(run, exc, now, using="default"):
    run.status = Status.FAILED
    run.finished_at = now
    run.error_summary = _error_text(exc)
    run.save(using=using)
    _audit(using, "backup.failed", {"trigger": run.trigger, "error": run.error_summary}, run)


def _verify_or_discard(destination, remote, size, sha):
    """Verify what this attempt uploaded. If it cannot be shown to be correct, remove that one
    file (best effort; the original failure is always the one reported, with a note if the
    removal also failed). Only the file this attempt created is ever removed."""
    problem = None
    try:
        if destination.verify(remote.remote_id, size, sha):
            return
        problem = archive.BackupError("the stored backup does not match what was uploaded")
    except DestinationError as exc:
        problem = exc
    note = ""
    try:
        destination.delete(remote.remote_id)
    except Exception:  # noqa: BLE001
        note = " (Removing the unverified upload also failed; it may remain at the destination.)"
    raise type(problem)(f"{problem}{note}") from None


# ------------------------------------------------------------------ the run
def run_backup(
    *,
    trigger="manual",
    kind="daily",
    now=None,
    destination=None,
    store=None,
    conn=None,
    using="default",
    key=None,
):
    """Make one backup now. Returns the ``BackupRun`` (check ``status``); raises only BackupBusy.
    ``conn``/``using`` name the database to back up (default: the application database)."""
    now = now or timezone.now()
    fail_interrupted(now, using)
    try:
        with transaction.atomic(using=using):
            run = BackupRun.objects.using(using).create(kind=kind, trigger=trigger, started_at=now)
    except IntegrityError as exc:
        raise BackupBusy("another backup is already running") from exc
    try:
        destination = destination or get_destination()
        key = key if key is not None else load_working_key(store)
        if key is None:
            raise archive.BackupError("no backup key is set up (run setup_backup_key)")
        fp = crypto.fingerprint(key)
        base = settings.BACKUP_TMP_DIR or None
        with tempfile.TemporaryDirectory(prefix="coven-backup-", dir=base) as work:
            work = Path(work)
            tar_path = work / "archive.tar"
            manifest = archive.build_archive(tar_path, work, fp, now, conn)
            name = f"coven-backup-{_local(now):%Y%m%d-%H%M%S}-{fp[:8]}.cbk"
            enc_path = work / name
            sha, size, plain_sha = crypto.encrypt_file(tar_path, enc_path, key)
            if plain_sha != manifest["archive_sha256"]:
                raise archive.BackupError("the archive changed while it was being encrypted")
            if crypto.decrypt_file(enc_path, None, key) != plain_sha:  # the key really opens it
                raise archive.BackupError("the encrypted backup did not verify locally")
            remote = destination.upload(enc_path, name)
            _verify_or_discard(destination, remote, size, sha)
        done = timezone.now() if now is None else max(now, timezone.now())
        run.status = Status.SUCCEEDED
        run.finished_at = done
        run.verified_at = done
        run.file_name, run.size_bytes, run.sha256 = name, size, sha
        run.key_fingerprint = fp
        run.destination, run.remote_id = destination.label(), remote.remote_id
        run.manifest = {k: v for k, v in manifest.items() if k != "files"} | {
            "file_count": len(manifest["files"])
        }
        run.manifest.get("database", {}).pop("row_counts", None)  # kept in the archive only
        run.save(using=using)
        _audit(
            using,
            "backup.succeeded",
            {"kind": kind, "trigger": trigger, "size": size, "fingerprint": fp},
            run,
        )
    except Exception as exc:  # noqa: BLE001 - every failure is stored, never lost
        _finish_failed(run, exc, timezone.now(), using)
    run.refresh_from_db(using=using)
    return run


# ------------------------------------------------------------------ health and catch-up
def last_success(now=None):
    return (
        BackupRun.objects.filter(status=Status.SUCCEEDED, kind=BackupRun.Kind.DAILY)
        .order_by("-started_at")
        .first()
    )


def scheduled_slot(now, at):
    """The most recent moment at or before ``now`` when the daily backup was due (Asia/Kolkata)."""
    local = _local(now)
    slot = local.replace(hour=at.hour, minute=at.minute, second=0, microsecond=0)
    if slot > local:
        slot -= timedelta(days=1)
    return slot


def health(now=None):
    """What the dashboard shows: the last success, its age, and what is wrong."""
    now = now or timezone.now()
    last = BackupRun.objects.filter(status=Status.SUCCEEDED).order_by("-started_at").first()
    problems = []
    if last is None:
        problems.append("No backup has ever succeeded.")
    elif now - last.started_at > STALE_AFTER:
        hours = int((now - last.started_at).total_seconds() // 3600)
        problems.append(f"The last successful backup is {hours} hours old (over 26 hours).")
    since = last.started_at if last else None
    failures = BackupRun.objects.filter(status=Status.FAILED)
    if since:
        failures = failures.filter(started_at__gt=since)
    return {
        "last_success": last,
        "age": (now - last.started_at) if last else None,
        "stale": last is None or now - last.started_at > STALE_AFTER,
        "problems": problems,
        "failures": list(failures.order_by("-started_at")[:5]),
        "failure_count": failures.count(),
    }


def backup_due(now=None):
    """(due, trigger). Due when no backup attempt has succeeded since the latest daily slot and a
    failed attempt is not too recent; a late start is a catch-up (B38)."""
    now = now or timezone.now()
    at = LibrarySetting.load().backup_time
    slot = scheduled_slot(now, at)
    if BackupRun.objects.filter(
        status=Status.RUNNING, started_at__gte=now - INTERRUPTED_AFTER
    ).exists():
        return False, ""
    if BackupRun.objects.filter(
        status=Status.SUCCEEDED, kind=BackupRun.Kind.DAILY, started_at__gte=slot
    ).exists():
        return False, ""
    recent_failure = BackupRun.objects.filter(
        status=Status.FAILED, started_at__gte=slot, finished_at__gt=now - RETRY_AFTER
    ).exists()  # the 30 minutes run from the failed attempt, not from when it began
    if recent_failure:
        return False, ""
    trigger = (
        BackupRun.Trigger.SCHEDULED if now - slot <= CATCHUP_GRACE else BackupRun.Trigger.CATCHUP
    )
    return True, trigger


# ------------------------------------------------------------------ retention
def released_safety_ids(live, using="default"):
    """Primary keys of safety backups (taken before a restore) that retention may treat like any
    other backup. BOTH must hold, else the safety backup stays protected: (1) the restore that
    replaced the database passed its verification (a ``RestoreRun`` that SUCCEEDED and names this
    exact safety file), and (2) a LATER daily backup of the restored system succeeded and was
    verified. Anything unclear (no matching restore, failed verification, no later success, a
    failed later backup) keeps it protected."""
    from .models import RestoreRun

    passed = [
        r
        for r in RestoreRun.objects.using(using).filter(status=RestoreRun.Status.SUCCEEDED)
        if r.pre_restore
    ]
    released = set()
    for safety in live.filter(kind=BackupRun.Kind.PRE_RESTORE):
        for restore in passed:
            pre = restore.pre_restore
            if pre.get("file_name") != safety.file_name or pre.get("remote_id") != safety.remote_id:
                continue
            if live.filter(
                kind=BackupRun.Kind.DAILY,
                verified_at__isnull=False,
                started_at__gt=restore.finished_at,
            ).exists():
                released.add(safety.pk)
                break
    return released


def retention_plan(now=None, using="default"):
    """(keep, remove) lists of runs. Daily: the newest 30 successful backups. Monthly: the last
    successful backup of each of the 12 most recent COMPLETED calendar months (D-29c). The newest
    successful daily backup is always kept. A safety backup before a restore is kept until it is
    released (see ``released_safety_ids``); once released it takes part in normal retention."""
    now = now or timezone.now()
    live = BackupRun.objects.using(using).filter(
        status=Status.SUCCEEDED, remote_deleted_at__isnull=True
    )
    newest_daily = live.filter(kind=BackupRun.Kind.DAILY).order_by("-started_at", "-id").first()
    released = released_safety_ids(live, using)
    pool = live.filter(Q(kind=BackupRun.Kind.DAILY) | Q(pk__in=released))
    daily = list(pool.order_by("-started_at", "-id"))
    keep = {r.pk for r in daily[:DAILY_KEEP]}
    by_month = {}
    this_month = (_local(now).year, _local(now).month)
    for run in daily:  # newest first: the first seen per month is that month's last backup
        key = (_local(run.started_at).year, _local(run.started_at).month)
        if key < this_month:
            by_month.setdefault(key, run)
    for key in sorted(by_month, reverse=True)[:MONTHLY_KEEP]:
        keep.add(by_month[key].pk)
    if newest_daily:
        keep.add(newest_daily.pk)
    protected = live.filter(kind=BackupRun.Kind.PRE_RESTORE).exclude(pk__in=released)
    keep |= set(protected.values_list("pk", flat=True))
    keep_runs = [r for r in live.order_by("-started_at", "-id") if r.pk in keep]
    remove_runs = [r for r in live.order_by("-started_at", "-id") if r.pk not in keep]
    return keep_runs, remove_runs


def apply_retention(now=None, destination=None, dry_run=False, using="default"):
    """Remove what the plan says from the destination.
    Returns {'kept': n, 'removed': n, 'errors': n}; ``skipped`` is set instead when a restore is
    using the database (retention then does nothing and runs again at the next launch)."""
    from django.db import connections

    from . import locks

    now = now or timezone.now()
    conn = connections[using]
    if not locks.try_lock(conn, shared=True):
        return {
            "kept": 0,
            "removed": 0,
            "errors": 0,
            "skipped": "a restore is in progress on this database",
        }
    try:
        keep, remove = retention_plan(now, using)
        result = {"kept": len(keep), "removed": 0, "errors": 0}
        if dry_run or not remove:
            result["would_remove"] = len(remove)
            return result
        destination = destination or get_destination()
        for run in remove:
            try:
                destination.delete(run.remote_id)
            except Exception as exc:  # noqa: BLE001 - one failure must not stop the others
                result["errors"] += 1
                _audit(using, "backup.retention_failed", {"error": _error_text(exc)}, run)
                continue
            run.remote_deleted_at = now
            run.save(using=using)
            result["removed"] += 1
        if result["removed"]:
            _audit(
                using,
                "backup.retention",
                {"removed": result["removed"], "kept": result["kept"]},
            )
        return result
    finally:
        locks.unlock(conn, shared=True)


def run_due_backup(now=None, destination=None, store=None):
    """The daily-job step: back up when due (catch-up included), then apply retention after a
    success. Returns the run or None."""
    now = now or timezone.now()
    due, trigger = backup_due(now)
    if not due:
        return None
    try:
        run = run_backup(trigger=trigger, now=now, destination=destination, store=store)
    except BackupBusy:
        return None
    if run.status == Status.SUCCEEDED:
        try:
            apply_retention(now, destination)
        except Exception as exc:  # noqa: BLE001 - retention trouble must not hide a good backup
            record("backup.retention_failed", summary={"error": _error_text(exc)})
    return run
