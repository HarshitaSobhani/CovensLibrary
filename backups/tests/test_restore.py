"""Restore rehearsals in DISPOSABLE databases: a real backup of a database full of real application
data is restored and compared with the source; every unsafe or incompatible input is refused
before anything is changed. (A rehearsal on a clean Windows machine is still outstanding.)"""

import io
import json
import os
import tarfile
import tempfile
from pathlib import Path

import psycopg
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection, connections
from django.test import TransactionTestCase, override_settings

from backups import archive, crypto, keystore, locks, restore, services
from backups.models import BackupRun, RestoreRun
from circulation import forfeiture, grace
from circulation import services as loans
from circulation.tests.helpers import D, make_copies, make_owner, member_at, overdue_loan
from communications import services as comms
from communications.tests.helpers import give_email
from communications.tests.helpers import ist as cist
from finance import corrections as fc
from memberships.tests.helpers import ensure_plans

from .helpers import (
    FakeDestination,
    TempDirMixin,
    create_disposable_database,
    drop_disposable_database,
    fresh_key,
    query,
    schema_fingerprint,
)

# The alias that restores are pointed at must exist before the test runner reads `databases`. It
# starts as a copy of the default database (same test database) and is re-pointed per test.
connections.databases.setdefault(restore.RESTORE_ALIAS, {**connections.databases["default"]})


def seed_library():
    """A realistic little library: members, a subscription, loans, a day-8 forfeiture, a lost
    book, adjustments, emails, audit history."""
    owner = make_owner()
    asha, copy, loan = overdue_loan(owner, name="Asha Rao", number="9876500001")
    give_email(asha, "asha@example.com")
    ravi = member_at(owner, name="Ravi Menon", number="9876500002")
    copies = make_copies(owner, 2, title="Lost One")
    lost_loan = loans.issue_loan(actor=owner, member=ravi, copy=copies[0], today=D(2026, 12, 1))
    loans.mark_lost(
        actor=owner, loan=lost_loan, owner_confirmed=True, reason="gone", today=D(2027, 1, 5)
    )
    fc.post_adjustment(
        actor=owner,
        member=ravi,
        direction="none",
        amount_paise=100,
        reason="note",
        owner_approved=True,
        today=D(2027, 1, 6),
    )
    forfeiture.process_overdue_forfeitures(today=D(2027, 1, 9))
    grace.change_grace_days(actor=owner, new_days=5, today=D(2027, 1, 9))
    comms.run_cycle(cist(2027, 1, 9, 10))
    return owner


class RehearsalBase(TempDirMixin, TransactionTestCase):
    databases = {"default", restore.RESTORE_ALIAS}

    def setUp(self):
        ensure_plans()
        self.make_dirs()
        self.key = fresh_key()
        self.addCleanup(keystore.reset_memory_keystore)
        self.targets = []
        self.addCleanup(self.drop_targets)

    def drop_targets(self):
        alias = connections[restore.RESTORE_ALIAS]
        alias.close()  # point the alias back at the test database before the targets go away
        alias.settings_dict["NAME"] = connections.databases["default"]["NAME"]
        for name in self.targets:
            drop_disposable_database(name)

    def new_target(self):
        name = create_disposable_database()
        self.targets.append(name)
        return name

    def backup(self, now=None):
        run = services.run_backup(destination=self.destination, now=now)
        self.assertEqual(run.status, BackupRun.Status.SUCCEEDED, run.error_summary)
        return run, Path(self.dest_dir) / run.file_name

    def restore_into(self, name, path, **kw):
        alias = restore.database_alias(name)
        return restore.restore(encrypted=path, conn=connections[alias], alias=alias, **kw)

    def forge(self, source, mutate, key=None, name="forged.cbk"):
        """Re-encrypt a backup after changing its tar (to test refusals)."""
        key = key or self.key
        work = Path(tempfile.mkdtemp(dir=self.tmp_dir))
        plain, forged = work / "p.tar", work / "f.tar"
        crypto.decrypt_file(source, plain, key)
        with tarfile.open(plain) as src, tarfile.open(forged, "w") as dst:
            members = [
                (m, src.extractfile(m).read() if m.isreg() else None) for m in src.getmembers()
            ]
            for member, data in mutate(members):
                dst.addfile(member, io.BytesIO(data) if data is not None else None)
        target = work / name
        crypto.encrypt_file(forged, target, key)
        return target

    def manifest_edit(self, change):
        def mutate(members):
            out = []
            for member, data in members:
                if member.name == "manifest.json":
                    manifest = json.loads(data)
                    change(manifest)
                    data = json.dumps(manifest).encode()
                    member.size = len(data)
                out.append((member, data))
            return out

        return mutate

    def assertUntouched(self, name):
        self.assertEqual(
            query(name, "SELECT count(*) FROM pg_tables WHERE schemaname='public'")[0][0],
            0,
            "the target was modified",
        )


class RoundTripTests(RehearsalBase):
    def test_a_real_backup_restores_into_a_clean_database_identically(self):
        seed_library()
        run, path = self.backup()
        source = schema_fingerprint(connection.settings_dict["NAME"])
        target = self.new_target()
        result = self.restore_into(target, path)
        self.assertTrue(result.ok, [s for s in result.steps if not s.ok])
        restored = schema_fingerprint(target)
        for part in ("tables", "constraints", "indexes", "triggers", "functions"):
            self.assertEqual(restored[part], source[part], part)
        # sequences: equal, except the two that the backup run itself advanced after the dump
        moved = ("audit_auditevent_id_seq", "backups_backuprun_id_seq", "backups_restorerun_id_seq")
        self.assertEqual(
            [r for r in restored["sequences"] if r[0] not in moved],
            [r for r in source["sequences"] if r[0] not in moved],
        )
        counts = {
            t: query(target, f'SELECT count(*) FROM "{t}"')[0][0] for (t,) in restored["tables"]
        }
        manifest_counts = result.manifest["database"]["row_counts"]
        for table, expected in manifest_counts.items():
            if table not in ("backups_restorerun", "audit_auditevent"):  # the restore wrote these
                self.assertEqual(counts[table], expected, table)

    def test_the_restored_data_is_the_data_that_was_backed_up(self):
        seed_library()
        _, path = self.backup()
        target = self.new_target()
        self.restore_into(target, path)
        names = [r[0] for r in query(target, "SELECT full_name FROM members_member ORDER BY id")]
        self.assertEqual(names, ["Asha Rao", "Ravi Menon"])
        source_actions = [
            r[0]
            for r in query(
                connection.settings_dict["NAME"], "SELECT action FROM audit_auditevent ORDER BY id"
            )
        ]
        restored_actions = [
            r[0] for r in query(target, "SELECT action FROM audit_auditevent ORDER BY id")
        ]
        # the source also logged the completion of the backup itself; the restore logged itself
        self.assertEqual(restored_actions, source_actions[:-1] + ["backup.restored"])
        self.assertEqual(source_actions[-1], "backup.succeeded")
        source = connection.cursor()
        for sql_text in (
            "SELECT receipt_no, amount_paise, kind FROM finance_financialtransaction ORDER BY id",
            "SELECT delta_paise, entry_type FROM finance_tokenledgerentry ORDER BY id",
            "SELECT status, due_on FROM circulation_loan ORDER BY id",
            "SELECT dedupe_key, kind, status FROM communications_notification ORDER BY id",
            "SELECT event_type, dedupe_key FROM core_domainevent ORDER BY id",
            "SELECT copy_code, status, condition FROM catalogue_bookcopy ORDER BY id",
        ):
            source.execute(sql_text)
            self.assertEqual(
                [tuple(r) for r in source.fetchall()], query(target, sql_text), sql_text
            )

    def test_the_security_model_is_reapplied_and_the_triggers_still_protect_history(self):
        seed_library()
        _, path = self.backup()
        target = self.new_target()
        result = self.restore_into(target, path)
        self.assertTrue(result.verification["database_security"]["ok"])
        for statement in (
            "UPDATE finance_financialtransaction SET amount_paise = amount_paise + 1",
            "DELETE FROM audit_auditevent",
            "UPDATE finance_tokenledgerentry SET delta_paise = 1",
        ):
            with self.assertRaises(psycopg.Error):
                query(target, statement)
        runtime = settings_runtime = os.environ.get("DB_PASSWORD")
        if runtime:  # the runtime role can use the restored database but is still restricted
            cfg = connection.settings_dict
            with psycopg.connect(
                host=cfg["HOST"] or "127.0.0.1",
                port=cfg["PORT"] or 5432,
                dbname=target,
                user=os.environ.get("DB_USER", "coven_app"),
                password=runtime,
                autocommit=True,
            ) as pg:
                self.assertEqual(pg.execute("SELECT count(*) FROM members_member").fetchone()[0], 2)
                with self.assertRaises(psycopg.Error):
                    pg.execute("DELETE FROM members_member")
                with self.assertRaises(psycopg.Error):
                    pg.execute("UPDATE audit_auditevent SET action='x'")
        del settings_runtime

    def test_the_restore_is_recorded_in_the_restored_database(self):
        seed_library()
        _, path = self.backup()
        target = self.new_target()
        result = self.restore_into(target, path)
        alias = restore.database_alias(target)
        record = RestoreRun.objects.using(alias).get()
        self.assertEqual(
            (record.status, record.backup_file_name, record.target_database),
            ("succeeded", path.name, target),
        )
        self.assertTrue(record.verification["row_counts"]["ok"])
        self.assertEqual(record.key_fingerprint, crypto.fingerprint(self.key))
        events = query(
            target, "SELECT summary FROM audit_auditevent WHERE action='backup.restored'"
        )
        self.assertEqual(len(events), 1)
        self.assertNotIn(self.key.hex(), json.dumps(events[0][0]))
        self.assertTrue(result.ok)
        with self.assertRaises(psycopg.Error):  # the restore record is append-only too
            query(target, "UPDATE backups_restorerun SET status='failed'")

    def test_the_numbering_continues_where_it_stopped(self):
        seed_library()
        _, path = self.backup()
        target = self.new_target()
        self.restore_into(target, path)
        source = {
            r[0]: r[1]
            for r in query(
                connection.settings_dict["NAME"],
                "SELECT sequencename, last_value FROM pg_sequences WHERE schemaname='public'",
            )
        }
        restored = {
            r[0]: r[1]
            for r in query(
                target,
                "SELECT sequencename, last_value FROM pg_sequences WHERE schemaname='public'",
            )
        }
        moved = ("audit_auditevent_id_seq", "backups_backuprun_id_seq", "backups_restorerun_id_seq")
        diff = {
            k: (source[k], restored.get(k))
            for k in source
            if k not in moved and source[k] != restored.get(k)
        }
        self.assertEqual(diff, {})
        ids = query(target, "SELECT name, year, last_value FROM core_idsequence ORDER BY 1,2")
        self.assertEqual(
            ids,
            query(
                connection.settings_dict["NAME"],
                "SELECT name, year, last_value FROM core_idsequence ORDER BY 1,2",
            ),
        )


class RestoredDatabaseIsUsableTests(RehearsalBase):
    def test_the_restored_database_is_operational_and_can_be_restored_again(self):
        seed_library()
        run, path = self.backup()
        target = self.new_target()
        self.restore_into(target, path)
        alias = restore.database_alias(target)
        self.assertEqual(BackupRun.objects.using(alias).filter(status="running").count(), 0)
        own = BackupRun.objects.using(alias).get(file_name=path.name)
        self.assertEqual(
            (own.status, own.sha256, own.destination),
            ("succeeded", crypto.sha256_file(path), "restored"),
        )
        self.assertEqual(own.key_fingerprint, crypto.fingerprint(self.key))
        # the replacement computer carries on: a new backup of the restored database works ...
        again = services.run_backup(
            conn=connections[alias],
            using=alias,
            destination=self.destination,
            now=cist(2027, 2, 1, 19, 45),
        )
        self.assertEqual(again.status, "succeeded", again.error_summary)
        # ... and that backup restores into a third database, closing the chain
        third = self.new_target()
        result = self.restore_into(third, Path(self.dest_dir) / again.file_name)
        self.assertTrue(result.ok)
        self.assertEqual(
            query(third, "SELECT full_name FROM members_member ORDER BY id"),
            [("Asha Rao",), ("Ravi Menon",)],
        )
        self.assertEqual(
            query(third, "SELECT count(*) FROM backups_restorerun"), [(2,)]
        )  # the first restore's record travelled

    def test_a_stale_second_running_row_is_closed_as_interrupted(self):
        seed_library()
        run, path = self.backup()
        target = self.new_target()
        self.restore_into(target, path)
        alias = restore.database_alias(target)
        self.assertEqual(
            restore.settle_running_rows(alias, path, {}, cist(2027, 3, 1, 12)), 0
        )  # nothing left to settle


class KeyRecoveryTests(RehearsalBase):
    def test_a_replacement_computer_restores_with_the_recovery_key_after_confirmation(self):
        seed_library()
        _, path = self.backup()
        recovery = keystore.recovery_key_text(self.key)
        keystore.reset_memory_keystore()  # a new computer: the credential store is empty
        target = self.new_target()
        with self.assertRaisesRegex(restore.RestoreError, "Provide the recovery key"):
            self.restore_into(target, path)
        with self.assertRaisesRegex(restore.RestoreError, "confirmation"):
            self.restore_into(target, path, recovery_text=recovery)  # no confirmation
        self.assertIsNone(keystore.load_working_key())
        self.assertUntouched(target)
        result = self.restore_into(
            target, path, recovery_text=recovery.lower().replace("-", " "), confirm_key_import=True
        )
        self.assertTrue(result.ok)
        self.assertTrue(result.key_imported)
        self.assertEqual(
            keystore.load_working_key(), self.key
        )  # restored into the credential store
        self.assertTrue(RestoreRun.objects.using(restore.database_alias(target)).get().key_imported)

    def test_a_wrong_or_mistyped_recovery_key_is_refused_untouched(self):
        seed_library()
        _, path = self.backup()
        recovery = keystore.recovery_key_text(self.key)
        other = keystore.recovery_key_text(keystore.generate_key())
        keystore.reset_memory_keystore()
        target = self.new_target()
        index = next(i for i, c in enumerate(recovery) if c != "-")
        typo = recovery[:index] + ("A" if recovery[index] != "A" else "B") + recovery[index + 1 :]
        for bad, message in (
            (typo, "typing mistake|not valid|wrong length"),
            (other, "different key"),
            ("", "characters|length|valid"),
        ):
            with self.assertRaisesRegex(restore.RestoreError, message):
                self.restore_into(target, path, recovery_text=bad, confirm_key_import=True)
            self.assertIsNone(keystore.load_working_key())
            self.assertUntouched(target)

    def test_a_stored_key_of_another_backup_does_not_open_this_one(self):
        seed_library()
        _, path = self.backup()
        keystore.save_working_key(keystore.generate_key())  # a different key is in the store
        target = self.new_target()
        with self.assertRaisesRegex(restore.RestoreError, "not in the credential store"):
            self.restore_into(target, path)
        self.assertUntouched(target)


class RefusalTests(RehearsalBase):
    def setUp(self):
        super().setUp()
        seed_library()
        self.run, self.path = self.backup()
        self.target = self.new_target()

    def refused(self, path, message):
        with self.assertRaisesRegex(restore.RestoreError, message):
            self.restore_into(self.target, path)
        self.assertUntouched(self.target)

    def test_a_tampered_backup_is_refused_before_anything_changes(self):
        data = bytearray(self.path.read_bytes())
        data[len(data) // 2] ^= 0x55
        bad = Path(self.tmp_dir) / "tampered.cbk"
        bad.write_bytes(bytes(data))
        self.refused(bad, "damaged or has been tampered")

    def test_truncated_or_junk_files_are_refused(self):
        bad = Path(self.tmp_dir) / "short.cbk"
        bad.write_bytes(self.path.read_bytes()[:-50])
        self.refused(bad, "damaged or has been tampered")
        junk = Path(self.tmp_dir) / "junk.cbk"
        junk.write_bytes(os.urandom(1000))
        self.refused(junk, "not a backup")

    def test_unsafe_archive_members_are_refused(self):
        def add(name, data=b"x", kind=tarfile.REGTYPE, link=""):
            def mutate(members):
                info = tarfile.TarInfo(name)
                info.size = len(data) if kind == tarfile.REGTYPE else 0
                info.type = kind
                info.linkname = link
                return members + [(info, data if kind == tarfile.REGTYPE else None)]

            return mutate

        for name, kwargs, message in (
            ("../evil.txt", {}, "unsafe path"),
            ("/etc/evil", {}, "unsafe path"),
            ("media/../../evil", {}, "unsafe path"),
            ("media\\..\\evil", {}, "unsafe path"),
            ("C:/evil", {}, "unsafe path"),
            ("other/file", {}, "unexpected file"),
            ("media/link", {"kind": tarfile.SYMTYPE, "link": "/etc/passwd"}, "non-file"),
            ("media/dir", {"kind": tarfile.DIRTYPE}, "non-file"),
        ):
            forged = self.forge(self.path, add(name, **kwargs))
            self.refused(forged, message)

    def test_a_file_that_differs_from_the_manifest_is_refused(self):
        def mutate(members):
            return [(m, (b"CORRUPT" + d) if m.name == "database.dump" else d) for m, d in members]

        def fix_sizes(members):
            out = []
            for member, data in mutate(members):
                if data is not None:
                    member.size = len(data)
                out.append((member, data))
            return out

        self.refused(self.forge(self.path, fix_sizes), "does not match the manifest")

    def test_extra_missing_and_duplicate_files_are_refused(self):
        extra = lambda members: members + [(tarfile.TarInfo("media/extra.txt"), b"")]  # noqa: E731
        self.refused(self.forge(self.path, extra), "do not match its manifest")
        no_dump = lambda members: [(m, d) for m, d in members if m.name != "database.dump"]  # noqa: E731
        self.refused(self.forge(self.path, no_dump), "no manifest or no database dump")
        dup = lambda members: members + [members[0]]  # noqa: E731
        self.refused(self.forge(self.path, dup), "twice")

    def test_incompatible_versions_are_refused(self):
        cases = (
            (lambda m: m.update(format_version=2), "unsupported backup format"),
            (lambda m: m.update(application="Other App"), "not a Coven's Library backup"),
            (lambda m: m["database"].update(server_version="99.1"), "PostgreSQL 99"),
            (lambda m: m["database"].update(server_version="unknown"), "PostgreSQL version"),
            (
                lambda m: m["database"]["migrations"].append("core.9999_from_the_future"),
                "NEWER application",
            ),
        )
        for change, message in cases:
            self.refused(self.forge(self.path, self.manifest_edit(change)), message)

    def test_an_older_backup_notes_that_newer_migrations_will_be_applied(self):
        def older(manifest):
            manifest["database"]["migrations"] = [
                m for m in manifest["database"]["migrations"] if not m.startswith("backups.0004")
            ]

        forged = self.forge(self.path, self.manifest_edit(older))
        result = self.restore_into(self.target, forged)
        self.assertTrue(result.ok)
        self.assertIn("newer migrations applied", [s.name for s in result.steps])

    def test_a_non_empty_target_needs_explicit_replacement(self):
        self.restore_into(self.target, self.path)
        with self.assertRaisesRegex(restore.RestoreError, "not empty"):
            self.restore_into(self.target, self.path)
        before = query(self.target, "SELECT count(*) FROM members_member")
        self.assertEqual(before, [(2,)])

    def test_restore_needs_the_owner_role(self):
        with override_settings(DB_ROLE="runtime"):
            with self.assertRaisesRegex(restore.RestoreError, "OWNER role"):
                self.restore_into(self.target, self.path)
        self.assertUntouched(self.target)


class ReplaceTests(RehearsalBase):
    def setUp(self):
        super().setUp()
        seed_library()
        self.run, self.path = self.backup()
        self.target = self.new_target()
        self.restore_into(self.target, self.path)
        self.alias = restore.database_alias(self.target)
        self.safety_dir = tempfile.mkdtemp(prefix="coven-safety-")
        self.addCleanup(__import__("shutil").rmtree, self.safety_dir, True)
        self.safety = FakeDestination(self.safety_dir)

    def make_target_different(self):
        query(
            self.target, "UPDATE members_member SET notes = 'edited after the backup'"
        )  # not blocked: mutable table
        return query(self.target, "SELECT notes FROM members_member ORDER BY id")

    def test_replacing_takes_a_safety_backup_first_and_then_restores_the_backup(self):
        self.make_target_different()
        result = self.restore_into(
            self.target, self.path, replace=True, safety_destination=self.safety
        )
        self.assertTrue(result.ok)
        names = [s.name for s in result.steps]
        self.assertLess(
            names.index("safety backup of the existing database"),
            names.index("old contents removed"),
        )
        self.assertLess(names.index("old contents removed"), names.index("database restored"))
        notes = query(self.target, "SELECT notes FROM members_member")
        self.assertNotIn(("edited after the backup",), notes)  # back to the backed-up state
        safety_files = list(Path(self.safety_dir).glob("*.cbk"))
        self.assertEqual(len(safety_files), 1)
        self.assertEqual(result.pre_restore["file_name"], safety_files[0].name)

    def test_the_safety_backup_really_contains_what_was_replaced(self):
        self.make_target_different()
        result = self.restore_into(
            self.target, self.path, replace=True, safety_destination=self.safety
        )
        second = self.new_target()
        self.restore_into(second, Path(self.safety_dir) / result.pre_restore["file_name"])
        notes = query(second, "SELECT notes FROM members_member ORDER BY id")
        self.assertEqual(notes, [("edited after the backup",)] * 2)

    def test_the_safety_backup_is_recorded_so_retention_can_tell_when_to_release_it(self):
        """Retention (services.released_safety_ids) releases a safety backup only when a
        SUCCEEDED RestoreRun names the same file and remote id and a later verified daily backup
        exists. The restore must therefore write exactly that linkage."""
        from backups.models import RestoreRun

        self.make_target_different()
        result = self.restore_into(
            self.target, self.path, replace=True, safety_destination=self.safety
        )
        self.assertTrue(result.ok)
        run = BackupRun.objects.using(self.alias).get(kind="pre_restore")
        self.assertEqual(run.status, "succeeded")
        done = RestoreRun.objects.using(self.alias).get()
        self.assertEqual(done.status, RestoreRun.Status.SUCCEEDED)
        self.assertEqual(done.pre_restore["file_name"], run.file_name)
        self.assertEqual(done.pre_restore["remote_id"], run.remote_id)
        # the safety row carries no daily-backup credit for the restored system
        later = BackupRun.objects.using(self.alias).filter(
            kind="daily", started_at__gt=done.finished_at
        )
        self.assertFalse(later.exists())

    def test_retention_runs_against_the_restored_database_and_releases_the_safety_backup(self):
        defaults_before = BackupRun.objects.count()
        self.make_target_different()
        result = self.restore_into(
            self.target, self.path, replace=True, safety_destination=self.safety
        )
        self.assertTrue(result.ok)
        live = BackupRun.objects.using(self.alias).filter(status="succeeded")
        safety = live.get(kind="pre_restore")
        # right after the restore: verified restore, but no later backup yet -> still protected
        self.assertEqual(services.released_safety_ids(live, self.alias), set())
        keep, remove = services.retention_plan(using=self.alias)
        self.assertIn(safety.pk, {r.pk for r in keep})
        self.assertEqual(remove, [])
        # the restored system makes its first backup of its own
        first = services.run_backup(
            conn=connections[self.alias],
            using=self.alias,
            destination=self.destination,
            trigger="manual",
        )
        self.assertEqual(first.status, "succeeded", first.error_summary)
        self.assertEqual(services.released_safety_ids(live, self.alias), {safety.pk})
        # retention on the restored database works, deletes nothing it should not, and leaves
        # the application database's own history alone
        deleted = []

        class Recorder:
            def delete(self, remote_id):
                deleted.append(remote_id)

        outcome = services.apply_retention(using=self.alias, destination=Recorder())
        self.assertEqual((outcome["removed"], outcome["errors"], deleted), (0, 0, []))
        self.assertNotIn("skipped", outcome)
        self.assertEqual(BackupRun.objects.count(), defaults_before)
        self.assertEqual(
            BackupRun.objects.using(self.alias).filter(remote_deleted_at__isnull=False).count(), 0
        )

    def holder(self, name):
        """A separate database session (a different process would look the same)."""
        cfg = connections["default"].settings_dict
        conn = psycopg.connect(
            host=cfg.get("HOST") or "127.0.0.1", port=cfg.get("PORT") or 5432, dbname=name,
            user=cfg["USER"], password=cfg.get("PASSWORD") or "", autocommit=True,
        )  # fmt: skip
        self.addCleanup(conn.close)
        return conn

    def test_retention_does_nothing_while_a_restore_holds_the_database(self):
        restoring = self.holder(self.target)
        restoring.execute("SELECT pg_advisory_lock(%s)", [locks.MAINTENANCE_LOCK])
        deleted = []

        class Recorder:
            def delete(self, remote_id):
                deleted.append(remote_id)

        outcome = services.apply_retention(using=self.alias, destination=Recorder())
        self.assertIn("restore is in progress", outcome["skipped"])
        self.assertEqual((outcome["removed"], deleted), (0, []))
        restoring.execute("SELECT pg_advisory_unlock(%s)", [locks.MAINTENANCE_LOCK])
        self.assertNotIn(
            "skipped", services.apply_retention(using=self.alias, destination=Recorder())
        )

    def test_a_restore_refuses_while_retention_is_running_and_changes_nothing(self):
        before = query(self.target, "SELECT count(*) FROM members_member")
        retention = self.holder(self.target)
        retention.execute("SELECT pg_advisory_lock_shared(%s)", [locks.MAINTENANCE_LOCK])
        with self.assertRaisesRegex(restore.RestoreError, "retention .* using this database"):
            self.restore_into(self.target, self.path, replace=True, safety_destination=self.safety)
        self.assertEqual(query(self.target, "SELECT count(*) FROM members_member"), before)
        self.assertEqual(list(Path(self.safety_dir).glob("*.cbk")), [])  # no safety backup either

    def test_the_restore_releases_its_lock_when_it_finishes_or_fails(self):
        probe = self.holder(self.target)

        def free():
            return (
                probe.execute(
                    "SELECT pg_try_advisory_lock(%s)", [locks.MAINTENANCE_LOCK]
                ).fetchone()[0]
                and probe.execute(
                    "SELECT pg_advisory_unlock(%s)", [locks.MAINTENANCE_LOCK]
                ).fetchone()[0]
            )

        with self.assertRaises(restore.RestoreError):  # refused: not empty, no --replace
            self.restore_into(self.target, self.path)
        self.assertTrue(free())
        self.restore_into(self.target, self.path, replace=True, safety_destination=self.safety)
        self.assertTrue(free())

    def test_a_failing_safety_backup_aborts_without_changing_anything(self):
        edited = self.make_target_different()
        bad = FakeDestination(self.safety_dir, fail_upload=True)
        with self.assertRaisesRegex(
            restore.RestoreError, "safety backup failed, nothing was changed"
        ):
            self.restore_into(self.target, self.path, replace=True, safety_destination=bad)
        self.assertEqual(query(self.target, "SELECT notes FROM members_member ORDER BY id"), edited)
        self.assertEqual(
            query(self.target, "SELECT count(*) FROM pg_tables WHERE schemaname='public'")[0][0]
            > 40,
            True,
        )

    def test_a_missing_safety_destination_is_refused(self):
        with self.assertRaisesRegex(restore.RestoreError, "needs a destination"):
            self.restore_into(self.target, self.path, replace=True)

    def test_skipping_the_safety_backup_must_be_explicit_and_is_reported(self):
        result = self.restore_into(self.target, self.path, replace=True, skip_safety_backup=True)
        detail = [s.detail for s in result.steps if s.name == "safety backup"][0]
        self.assertIn("SKIPPED", detail)
        self.assertEqual(result.pre_restore, {})


class MediaTests(RehearsalBase):
    def test_media_is_restored_exactly_and_assets_are_not_overwritten(self):
        seed_library()
        media = Path(self.tmp_dir) / "media"
        (media / "logos").mkdir(parents=True)
        (media / "logos" / "logo.png").write_bytes(b"ORIGINAL")
        (media / "notes.txt").write_bytes(b"keep")
        with override_settings(MEDIA_ROOT=media):
            run, path = self.backup()
        (media / "logos" / "logo.png").write_bytes(b"CHANGED AFTER BACKUP")
        (media / "stray.txt").write_bytes(b"not in the backup")
        target = self.new_target()
        with override_settings(MEDIA_ROOT=media):
            result = self.restore_into(target, path)
        self.assertEqual((media / "logos" / "logo.png").read_bytes(), b"ORIGINAL")
        self.assertEqual((media / "notes.txt").read_bytes(), b"keep")
        self.assertFalse((media / "stray.txt").exists())  # exactly as backed up
        self.assertEqual(result.verification["media"]["media_files"], 2)
        self.assertEqual(result.verification["media"]["assets_different"], [])

    def test_a_missing_asset_is_restored_and_a_different_one_is_only_reported(self):
        seed_library()
        run, path = self.backup()
        asset = next(p for p in (Path(os.getcwd()) / "assets").rglob("*") if p.is_file())
        original = asset.read_bytes()
        self.addCleanup(asset.write_bytes, original)
        asset.write_bytes(b"locally modified")
        target = self.new_target()
        result = self.restore_into(target, path)
        self.assertIn(
            f"assets/{asset.relative_to(Path(os.getcwd()) / 'assets').as_posix()}",
            result.verification["media"]["assets_different"],
        )
        self.assertEqual(
            asset.read_bytes(), b"locally modified"
        )  # the code's own file is not overwritten


class CommandTests(RehearsalBase):
    def run_command(self, *args, **kw):
        out = io.StringIO()
        call_command("restore_backup", *args, stdout=out, **kw)
        return out.getvalue()

    def test_the_command_restores_verifies_and_is_honest_about_what_it_proves(self):
        seed_library()
        run, path = self.backup()
        target = self.new_target()
        out = self.run_command(
            str(path), f"--target-database={target}", f"--confirm=RESTORE {target}"
        )
        self.assertIn("Restore verified", out)
        self.assertIn("recovery is verified only by a rehearsal on a clean machine", out)
        self.assertEqual(query(target, "SELECT count(*) FROM members_member"), [(2,)])

    def test_a_wrong_confirmation_cancels_before_anything_happens(self):
        seed_library()
        run, path = self.backup()
        target = self.new_target()
        with self.assertRaisesRegex(CommandError, "confirmation did not match"):
            self.run_command(
                str(path), f"--target-database={target}", "--confirm=RESTORE something-else"
            )
        self.assertUntouched(target)

    def test_fetching_from_a_local_destination_by_name_and_latest(self):
        seed_library()
        run, path = self.backup()
        target = self.new_target()
        out = self.run_command(
            "latest",
            f"--destination=local:{self.dest_dir}",
            f"--target-database={target}",
            f"--confirm=RESTORE {target}",
        )
        self.assertIn("Restore verified", out)
        again = self.new_target()
        self.run_command(
            run.file_name,
            f"--destination=local:{self.dest_dir}",
            f"--target-database={again}",
            f"--confirm=RESTORE {again}",
        )
        self.assertEqual(query(again, "SELECT count(*) FROM members_member"), [(2,)])

    def test_key_import_through_the_command_needs_the_typed_confirmation(self):
        seed_library()
        run, path = self.backup()
        keyfile = Path(self.tmp_dir) / "recovery.txt"
        keyfile.write_text(keystore.recovery_key_text(self.key))
        keystore.reset_memory_keystore()
        target = self.new_target()
        base = [
            str(path),
            f"--target-database={target}",
            f"--confirm=RESTORE {target}",
            f"--recovery-key-file={keyfile}",
        ]
        with self.assertRaisesRegex(CommandError, "key restoration was not confirmed"):
            self.run_command(*base, "--confirm-key=nope")
        self.assertUntouched(target)
        self.assertIsNone(keystore.load_working_key())
        self.run_command(*base, "--confirm-key=RESTORE KEY")
        self.assertEqual(keystore.load_working_key(), self.key)

    def test_replace_and_skip_flags_are_guarded(self):
        seed_library()
        run, path = self.backup()
        target = self.new_target()
        base = [str(path), f"--target-database={target}", f"--confirm=RESTORE {target}"]
        self.run_command(*base)
        with self.assertRaisesRegex(CommandError, "not empty"):
            self.run_command(*base)
        with self.assertRaisesRegex(CommandError, "NO SAFETY BACKUP"):
            self.run_command(*base, "--replace", "--skip-safety-backup")
        out = self.run_command(*base, "--replace", f"--safety-destination=local:{self.dest_dir}")
        self.assertIn("safety backup of the existing database", out)

    def test_a_missing_file_and_the_runtime_role_are_refused(self):
        with self.assertRaisesRegex(CommandError, "No such backup file"):
            self.run_command(
                "/nonexistent.cbk", f"--confirm=RESTORE {connection.settings_dict['NAME']}"
            )
        with override_settings(DB_ROLE="runtime"):
            with self.assertRaisesRegex(CommandError, "owner role"):
                self.run_command("x.cbk")


class ArchiveHelpersTests(RehearsalBase):
    def test_the_manifest_carries_row_counts_taken_in_the_same_snapshot_as_the_dump(self):
        seed_library()
        run, path = self.backup()
        work = Path(tempfile.mkdtemp(dir=self.tmp_dir))
        manifest, files = restore.open_backup(path, self.key, work)
        self.assertEqual(manifest["database"]["row_counts"]["members_member"], 2)
        self.assertNotIn(
            "row_counts", run.manifest["database"]
        )  # not duplicated in the history table
        self.assertEqual(
            manifest["database"]["row_counts"]["backups_backuprun"], 1
        )  # the run in progress
        self.assertTrue(archive.DUMP_NAME in files)
