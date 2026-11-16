"""Backup-key rotation: a new key for NEW backups while every historical backup stays openable.

Design (see docs/KEY_ROTATION_AND_RECOVERY.md):
* Keys live in the credential store in three kinds of slot: ``backup-working-key`` (new backups are
  made with it), ``backup-pending-key`` (a new key during a rotation, before it is committed) and
  ``backup-retired-<fingerprint>`` (an old key kept so the backups it made can still be restored).
* Every backup file names its key by fingerprint (public, 64 bits); restore looks the key up in the
  slots, or asks for that key's recovery key.
* The old key is NEVER deleted by a rotation. It is retired (verified copy), then proven to open the
  newest backup it made, and only an explicit, separate ``prune`` can remove it, and only when no
  retained backup needs it.
* Every step is persisted before the next one, so a crash at any point is recoverable with
  ``--resume`` (finish) or ``--abort`` (before the commit); ``set_current`` swaps back (rollback).
* Audit events carry fingerprints only, never key material. Nothing here prints a key except the
  recovery text shown once to the owner, who must type it back.
"""

import secrets
import tempfile
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

from django.db import connection
from django.utils import timezone

from audit.services import record

from . import crypto, keyscan, keystore, locks, services
from .models import BackupRun

CONFIRM_ROTATE = "ROTATE BACKUP KEY"
CONFIRM_ABORT = "ABORT KEY ROTATION"
CONFIRM_SET_CURRENT = "SET CURRENT BACKUP KEY"
CONFIRM_REMOVE = "REMOVE OLD BACKUP KEY"
CONFIRM_IMPORT = "IMPORT BACKUP KEY"
FRESH_BACKUP = timedelta(hours=26)
PROBE = "backup-rotation-probe"
S = BackupRun.Status


class RotationError(Exception):
    """A refusal or a failed step; the message is safe to show (no key material)."""


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""


@dataclass
class Report:
    checks: list = field(default_factory=list)

    def add(self, name, ok, detail=""):
        self.checks.append(Check(name, bool(ok), detail))

    @property
    def ok(self):
        return all(c.ok for c in self.checks)

    def failed(self):
        return [c for c in self.checks if not c.ok]


def fp(key):
    return crypto.fingerprint(key)


def _live_backups():
    return BackupRun.objects.filter(status=S.SUCCEEDED, remote_deleted_at__isnull=True)


def current_fingerprint():
    """The key of the newest verified daily backup: the key the library is 'currently' using."""
    run = _live_backups().filter(kind=BackupRun.Kind.DAILY).order_by("-started_at", "-id").first()
    return run.key_fingerprint if run else None


def key_usage():
    """{fingerprint: {'count', 'first', 'last'}} for the backups that still exist."""
    usage = {}
    for run in _live_backups().order_by("started_at"):
        entry = usage.setdefault(run.key_fingerprint, {"count": 0, "first": run.started_at})
        entry["count"] += 1
        entry["last"] = run.started_at
    return usage


def state(store=None):
    store = store or keystore.get_keystore()
    working, pending = keystore.load_working_key(store), keystore.load_pending_key(store)
    return {
        "working": fp(working) if working else None,
        "pending": fp(pending) if pending else None,
        "in_progress": bool(pending),
        "committed_not_finished": bool(pending and working and pending == working),
    }


# ------------------------------------------------------------------ preflight (changes nothing)
def preflight(*, store=None, destination=None, now=None, old_recovery_text=None):
    store = store or keystore.get_keystore()
    now = now or timezone.now()
    report = Report()
    try:
        working = keystore.load_working_key(store)
    except keystore.KeyStoreError as exc:
        report.add("The current backup key can be read", False, str(exc))
        return report
    report.add("The current backup key can be read", working is not None, "no key is set up")
    if working is None:
        return report
    current = fp(working)
    pend = keystore.load_pending_key(store)
    report.add(
        "No rotation is already in progress",
        pend is None,
        "a rotation is in progress: resume it or abort it first",
    )
    report.add(
        "The credential store accepts and returns a test value",
        _store_round_trip(store),
        "the store could not be written and read back: nothing would be safe to rotate",
    )
    recent = (
        _live_backups()
        .filter(
            kind=BackupRun.Kind.DAILY, key_fingerprint=current, started_at__gte=now - FRESH_BACKUP
        )
        .order_by("-started_at")
        .first()
    )
    report.add(
        "A verified backup with the current key exists from the last 26 hours",
        recent is not None,
        "run a backup first so the old key has a fresh, restorable backup",
    )
    newest = _live_backups().filter(key_fingerprint=current).order_by("-started_at", "-id").first()
    stored_ok = False
    if newest is not None and destination is not None:
        try:
            stored_ok = bool(destination.verify(newest.remote_id, newest.size_bytes, newest.sha256))
        except Exception:  # noqa: BLE001 - any storage problem is a failed check, not a crash
            stored_ok = False
    report.add(
        "The newest backup made with the current key is present at the destination and intact",
        stored_ok,
        "the old key's newest backup could not be verified at the destination",
    )
    if old_recovery_text is None:
        report.add("The owner holds the CURRENT recovery key (typed back)", True, "will be asked")
    else:
        try:
            typed = keystore.parse_recovery_key(old_recovery_text)
            ok = typed == working
        except keystore.RecoveryKeyError:
            ok = False
        report.add(
            "The owner holds the CURRENT recovery key (typed back)",
            ok,
            "the recovery key typed does not match the current backup key",
        )
    return report


def _store_round_trip(store):
    value = secrets.token_hex(8)
    try:
        store.set(PROBE, value)
        good = store.get(PROBE) == value
        store.delete(PROBE)
        return good and not store.get(PROBE)
    except Exception:  # noqa: BLE001
        return False


# ------------------------------------------------------------------ the rotation
def rotate(
    *, confirm, old_recovery_text, new_recovery_prompt, store=None, destination=None, now=None
):
    """Start and complete a rotation. ``new_recovery_prompt(text)`` shows the NEW recovery key to
    the owner and returns what she typed back (it must match). Returns a summary dict."""
    if confirm != CONFIRM_ROTATE:
        raise RotationError(f'Cancelled: type "{CONFIRM_ROTATE}" to confirm.')
    store = store or keystore.get_keystore()
    destination = destination or services.get_destination()
    now = now or timezone.now()
    if not locks.try_lock(connection):
        raise RotationError("Another maintenance operation is using the database. Try again.")
    try:
        report = preflight(
            store=store, destination=destination, now=now, old_recovery_text=old_recovery_text
        )
        if not report.ok:
            raise RotationError(
                "Preflight failed, nothing was changed: "
                + "; ".join(f"{c.name} ({c.detail})" for c in report.failed())
            )
        old = keystore.load_working_key(store)
        new = keystore.generate_key()
        keystore.save_slot(keystore.PENDING_KEY, new, store)  # persisted before anything else
        record("backup.key_rotation_started", summary={"old": fp(old), "new": fp(new)})
        return _continue(store, destination, new_recovery_prompt, now)
    finally:
        locks.unlock(connection)


def resume(*, new_recovery_prompt, store=None, destination=None, now=None):
    """Finish an interrupted rotation from wherever it stopped."""
    store = store or keystore.get_keystore()
    destination = destination or services.get_destination()
    if not locks.try_lock(connection):
        raise RotationError("Another maintenance operation is using the database. Try again.")
    try:
        if keystore.load_pending_key(store) is None:
            raise RotationError("No rotation is in progress.")
        return _continue(store, destination, new_recovery_prompt, now or timezone.now())
    finally:
        locks.unlock(connection)


def _continue(store, destination, new_recovery_prompt, now):
    working = keystore.load_working_key(store)
    new = keystore.load_pending_key(store)
    if new is None:
        raise RotationError("No rotation is in progress.")
    if working == new:  # the commit was written but not finished
        return _finish(store, new, working, resumed=True)
    if working is None:
        raise RotationError("The current backup key is missing: stop and contact support.")
    old = working
    # A. the owner proves she has written the NEW recovery key down
    typed = new_recovery_prompt(keystore.recovery_key_text(new))
    try:
        ok = keystore.parse_recovery_key(typed or "") == new
    except keystore.RecoveryKeyError:
        ok = False
    if not ok:
        raise RotationError(
            "The new recovery key was not typed back correctly. Nothing was committed; run the "
            "rotation again with --resume."
        )
    # B. keep the old key: a verified retired copy, BEFORE anything else can change
    keystore.save_retired(old, store)
    # C. prove the retired copy opens the newest backup the old key made
    _prove_old_key(store, destination, fp(old))
    # D. a real backup with the new key, verified at the destination and re-opened with the new key
    _verification_backup(store, destination, new, now)
    # E. commit
    keystore.save_working_key(new, store, replace=True)
    return _finish(store, new, old)


def _prove_old_key(store, destination, old_fp):
    run = _live_backups().filter(key_fingerprint=old_fp).order_by("-started_at", "-id").first()
    if run is None:
        raise RotationError("No backup made with the old key exists to prove the old key with.")
    retired = keystore.load_retired_key(old_fp, store)
    if retired is None or fp(retired) != old_fp:
        raise RotationError("The retired copy of the old key is not readable; nothing committed.")
    _open_remote(destination, run, retired, "the old key")


def _verification_backup(store, destination, new, now):
    new_fp = fp(new)
    run = _live_backups().filter(key_fingerprint=new_fp).order_by("-started_at", "-id").first()
    if run is None:
        try:
            run = services.run_backup(trigger="manual", destination=destination, key=new, now=now)
        except services.BackupBusy as exc:
            raise RotationError(
                "A backup is running right now, so the rotation is paused (nothing was lost). "
                "Wait for it to finish, then run rotate_backup_key --resume."
            ) from exc
        if run.status != S.SUCCEEDED or run.key_fingerprint != new_fp:
            raise RotationError(
                "The verification backup with the new key failed, nothing was committed: "
                f"{run.error_summary}"
            )
    _open_remote(destination, run, keystore.load_pending_key(store), "the new key")


def _open_remote(destination, run, key, label):
    with tempfile.TemporaryDirectory(prefix="coven-rotation-") as work:
        target = Path(work) / "check.cbk"
        try:
            destination.download(run.remote_id, target)
            plain = crypto.decrypt_file(target, None, key)
        except Exception as exc:  # noqa: BLE001
            raise RotationError(
                f"{label[0].upper() + label[1:]} could not open {run.file_name} "
                f"({type(exc).__name__}); nothing was committed."
            ) from exc
    expected = (run.manifest or {}).get("archive_sha256")
    if expected and plain != expected:
        raise RotationError(f"{label} opened {run.file_name} but its content does not match")


def _finish(store, new, working, resumed=False):
    new_fp = fp(new)
    if not _recorded("backup.key_rotation_committed", "new", new_fp):
        record("backup.key_rotation_committed", summary={"new": new_fp})
    keystore.delete_slot(keystore.PENDING_KEY, store)  # identical to the working key now
    return {"new": new_fp, "retired": sorted(_retired_fps(store)), "resumed": resumed}


def _recorded(action, key, value):
    from audit.models import AuditEvent

    return AuditEvent.objects.filter(action=action, **{f"summary__{key}": value}).exists()


def _retired_fps(store):
    return keystore.retired_fingerprints(store)


def abort(*, confirm, destination, store=None):
    """Cancel a rotation that has NOT been committed AND has made no backup with the new key. The
    old key stays the working key and any retired copy is kept (harmless).

    Refused, and the owner told to use --resume, when ANY backup already uses the pending key: that
    is judged from the destination itself (every file's header), not only from the database, and
    when the destination cannot be fully and safely inspected. Nothing is deleted on a refusal."""
    if confirm != CONFIRM_ABORT:
        raise RotationError(f'Cancelled: type "{CONFIRM_ABORT}" to confirm.')
    store = store or keystore.get_keystore()
    pending, working = keystore.load_pending_key(store), keystore.load_working_key(store)
    if pending is None:
        raise RotationError("No rotation is in progress.")
    if working == pending:
        raise RotationError("The new key is already the working key: use --resume to finish.")
    new_fp = fp(pending)
    if BackupRun.objects.filter(status=S.RUNNING).exists():
        raise RotationError(
            "A backup is running (or was interrupted) right now; abort was refused because it "
            "might be using the new key. Wait for it to finish, then try again, or use --resume."
        )
    if _live_backups().filter(key_fingerprint=new_fp).exists():
        raise RotationError(
            "Abort refused: a backup was already made with the new key. Finish the rotation with "
            "rotate_backup_key --resume. The new key was NOT deleted."
        )
    try:
        scan = keyscan.scan_destination(destination)
    except keyscan.ScanError as exc:
        raise RotationError(
            f"Abort refused, nothing was deleted: {exc}. Fix the destination or use --resume."
        ) from exc
    if scan.problems:
        raise RotationError(
            f"Abort refused, nothing was deleted: {len(scan.problems)} item(s) at the destination "
            f"could not be safely inspected ({_names(scan.problems)}). Use --resume, or inspect "
            "the destination first."
        )
    using = scan.using(new_fp)
    if using:
        raise RotationError(
            f"Abort refused: {len(using)} backup file(s) at the destination were made with the new "
            f"key ({_names(using)}). Finish the rotation with rotate_backup_key --resume. The new "
            "key was NOT deleted."
        )
    keystore.delete_slot(keystore.PENDING_KEY, store)
    record("backup.key_rotation_aborted", summary={"new": new_fp, "old": fp(working)})
    return {"aborted": new_fp, "working": fp(working)}


def _names(entries, limit=3):
    names = [e.name for e in entries]
    shown = ", ".join(names[:limit])
    return shown + (f" and {len(names) - limit} more" if len(names) > limit else "")


# ------------------------------------------------------------------ rollback / reconcile
def set_current(target_fp, *, confirm, store=None):
    """Make a retained key the working key (rollback after a rotation, or after restoring on a new
    computer so the working key matches the library's newest backup). Swaps: the key that was
    working is kept as a retired key. Nothing is deleted."""
    if confirm != CONFIRM_SET_CURRENT:
        raise RotationError(f'Cancelled: type "{CONFIRM_SET_CURRENT}" to confirm.')
    store = store or keystore.get_keystore()
    if keystore.load_pending_key(store) is not None:
        raise RotationError("A rotation is in progress: resume or abort it first.")
    working = keystore.load_working_key(store)
    if working is not None and fp(working) == target_fp:
        # An earlier attempt already made this key current and stopped before finishing: only the
        # clean-up of its retired copy is left. Idempotent, so the retry never refuses.
        if keystore.load_retired_key(target_fp, store) is not None:
            keystore.delete_retired(target_fp, store)
        record(
            "backup.key_current_set", summary={"from": target_fp, "to": target_fp, "resumed": True}
        )
        return {"working": target_fp, "retired": None}
    target = keystore.load_retired_key(target_fp, store)
    if target is None or fp(target) != target_fp:
        raise RotationError("That key is not among the retired keys in the credential store.")
    # Each step leaves every key in at least one slot: the old working key is kept as retired
    # BEFORE the working slot is replaced, and the target's retired copy is removed only after it
    # is the working key.
    if working is not None:
        keystore.save_retired(working, store)
    keystore.save_working_key(target, store, replace=True)
    keystore.delete_retired(target_fp, store)
    record(
        "backup.key_current_set",
        summary={"from": fp(working) if working else None, "to": target_fp},
    )
    return {"working": target_fp, "retired": fp(working) if working else None}


# ------------------------------------------------------------------ pruning old keys
@dataclass
class Assessment:
    fingerprint: str
    blockers: list = field(default_factory=list)
    notes: list = field(default_factory=list)

    @property
    def safe(self):
        return not self.blockers


def assess_prune(target_fp, *, destination, store=None):
    """Can this retired key be removed? It can ONLY if the destination can be listed completely,
    every item there can be read as a backup, no backup (at the destination or in the history)
    needs the key, and the destination and the database history agree. Anything doubtful is a
    blocker; an unknown file is never assumed to be irrelevant. Changes nothing."""
    store = store or keystore.get_keystore()
    result = Assessment(target_fp)
    working = keystore.load_working_key(store)
    if working is not None and fp(working) == target_fp:
        result.blockers.append("it is the current working key")
    if keystore.load_pending_key(store) is not None:
        result.blockers.append("a rotation is in progress")
    if keystore.load_retired_key(target_fp, store) is None:
        result.blockers.append("it is not a retired key in the credential store")
    try:
        scan = keyscan.scan_destination(destination)
    except keyscan.ScanError as exc:
        result.blockers.append(str(exc))
        return result
    if scan.problems:
        result.blockers.append(
            f"{len(scan.problems)} item(s) at the destination could not be safely inspected "
            f"({_names(scan.problems)}); an uninspected file might need the key"
        )
    if scan.duplicate_names:
        result.blockers.append(
            "duplicate backup file names at the destination ("
            + ", ".join(scan.duplicate_names[:3])
            + "): the history cannot be matched to them"
        )
    needing = scan.using(target_fp)
    if needing:
        result.blockers.append(
            f"{len(needing)} backup file(s) at the destination need that key ({_names(needing)})"
        )
    live = list(_live_backups())
    in_history = [r for r in live if r.key_fingerprint == target_fp]
    if in_history:
        result.blockers.append(f"{len(in_history)} retained backup(s) in the history need that key")
    listed = scan.remote_ids()
    missing = [r for r in live if r.remote_id not in listed]
    if missing:
        result.blockers.append(
            f"{len(missing)} backup(s) the history lists are not at the destination "
            f"({', '.join(r.file_name for r in missing[:3])}): the listing may be incomplete or "
            "the wrong destination is configured"
        )
    known = {r.remote_id for r in live}
    unknown = [e for e in scan.entries if e.remote_id not in known]
    if unknown:
        keys = sorted({e.fingerprint for e in unknown if e.fingerprint})
        result.blockers.append(
            f"{len(unknown)} file(s) at the destination are not in this database's history "
            f"({_names(unknown)}; keys {', '.join(keys) or 'unreadable'}): a database restored "
            "from an older backup? History and destination must agree before a key is removed"
        )
    if not scan.entries and live:
        result.blockers.append("the destination is empty but the history lists backups")
    result.notes.append(f"{len(scan.entries)} item(s) inspected at the destination")
    return result


def prune_report(*, destination, store=None):
    """One assessment per retired key (for the listing), or the reason none could be made."""
    store = store or keystore.get_keystore()
    fingerprints = sorted(set(keystore.retired_fingerprints(store)) | set(key_usage()))
    working = keystore.load_working_key(store)
    return [
        assess_prune(known, destination=destination, store=store)
        for known in fingerprints
        if working is None or known != fp(working)
    ]


def prune(target_fp, *, confirm, destination, store=None):
    if confirm != CONFIRM_REMOVE:
        raise RotationError(f'Cancelled: type "{CONFIRM_REMOVE}" to confirm.')
    store = store or keystore.get_keystore()
    assessment = assess_prune(target_fp, destination=destination, store=store)
    if not assessment.safe:
        raise RotationError(
            "Refused, the key was kept: " + "; ".join(assessment.blockers[:4]) + "."
        )
    keystore.delete_retired(target_fp, store)
    record("backup.key_retired_removed", summary={"fingerprint": target_fp})
    return {"removed": target_fp}


# ------------------------------------------------------------------ importing a key from paper
def _unfinished_rotation_for(new_fp):
    from audit.models import AuditEvent

    started = (
        AuditEvent.objects.filter(action="backup.key_rotation_started", summary__new=new_fp)
        .order_by("-id")
        .first()
    )
    if started is None:
        return False
    return not AuditEvent.objects.filter(
        action__in=["backup.key_rotation_committed", "backup.key_rotation_aborted"],
        summary__new=new_fp,
        id__gt=started.id,
    ).exists()


def import_key(recovery_text, *, confirm, as_slot, destination=None, backup_file=None, store=None):
    """Put a key back into the credential store from its paper recovery key.

    Safe by construction: the recovery key is validated (checksum), the key must open a REAL backup
    (one at the destination or a local file you name), nothing existing is overwritten, and the
    fingerprint (never the key) is all that is recorded or returned. ``as_slot`` is ``retired``
    (the normal case: it opens old backups; make it current with --set-current if it is the key of
    the newest backup) or ``pending`` (only for a rotation that was left unfinished, so that
    --resume can complete it)."""
    import tempfile
    from pathlib import Path

    if confirm != CONFIRM_IMPORT:
        raise RotationError(f'Cancelled: type "{CONFIRM_IMPORT}" to confirm.')
    if as_slot not in ("retired", "pending"):
        raise RotationError("The key can be imported as 'retired' or 'pending'.")
    store = store or keystore.get_keystore()
    try:
        key = keystore.parse_recovery_key(recovery_text or "")
    except keystore.RecoveryKeyError as exc:
        raise RotationError(str(exc)) from exc
    wanted = fp(key)
    if keystore.find_key(wanted, store) is not None:
        raise RotationError(f"The key {wanted} is already in the credential store.")
    if as_slot == "pending":
        if keystore.load_pending_key(store) is not None:
            raise RotationError("A pending key already exists; resume or abort that rotation.")
        if not _unfinished_rotation_for(wanted):
            raise RotationError(
                "No unfinished rotation uses that key (it was committed, aborted or never "
                "started): import it as 'retired' instead."
            )
    # the key must really open a backup that was made with it
    with tempfile.TemporaryDirectory(prefix="coven-import-") as work:
        target = Path(work) / "proof.cbk"
        if backup_file:
            source = Path(backup_file)
            try:
                header_fp = crypto.read_header(source)[2]
            except (OSError, crypto.DecryptionError) as exc:
                raise RotationError(f"That file is not a readable backup: {exc}") from exc
            if header_fp != wanted:
                raise RotationError("That backup file was made with a different key.")
            target = source
        else:
            if destination is None:
                raise RotationError("Name a backup file (--backup-file) or a destination.")
            try:
                scan = keyscan.scan_destination(destination)
            except keyscan.ScanError as exc:
                raise RotationError(f"Nothing was imported: {exc}.") from exc
            candidates = scan.using(wanted)
            if not candidates:
                raise RotationError(
                    "No backup at the destination was made with that key, so it cannot be "
                    "checked: nothing was imported."
                )
            try:
                destination.download(candidates[0].remote_id, target)
            except Exception as exc:  # noqa: BLE001
                raise RotationError(
                    f"The backup could not be downloaded ({type(exc).__name__}): nothing imported."
                ) from exc
        try:
            crypto.decrypt_file(target, None, key)
        except crypto.DecryptionError as exc:
            raise RotationError("That key does not open the backup: nothing was imported.") from exc
    if as_slot == "pending":
        keystore.save_slot(keystore.PENDING_KEY, key, store)
    else:
        keystore.save_retired(key, store)
    record("backup.key_imported", summary={"fingerprint": wanted, "slot": as_slot})
    return {"imported": wanted, "slot": as_slot}


# ------------------------------------------------------------------ recovery material
def recovery_bundle(store=None):
    """Every key the owner must be able to recover, with the recovery text and what it protects."""
    store = store or keystore.get_keystore()
    usage = key_usage()
    items = []
    seen = set()

    def add(role, key):
        if key is None or fp(key) in seen:
            return
        seen.add(fp(key))
        used = usage.get(fp(key), {})
        items.append(
            {
                "role": role,
                "fingerprint": fp(key),
                "recovery": keystore.recovery_key_text(key),
                "backups": used.get("count", 0),
                "first": used.get("first"),
                "last": used.get("last"),
            }
        )

    add("current", keystore.load_working_key(store))
    add("pending (rotation not finished)", keystore.load_pending_key(store))
    for known in sorted(set(usage) | set(keystore.retired_fingerprints(store))):
        add("retired", keystore.load_retired_key(known, store))
    return items
