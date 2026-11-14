import getpass
import os
import tempfile
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connections

from backups import crypto, keystore, restore
from backups.destinations import DestinationError, get_destination


class Command(BaseCommand):
    help = (
        "Restore a backup into a database (CLI-first, owner role). Run as: python manage.py "
        "restore_backup <file> --settings=config.settings.migrate. The target database must "
        "already exist and be owned by the owner role (a replacement computer provisions an "
        "empty one first). A non-empty target is replaced only with --replace and only after a "
        "safety backup. Recovery is NOT proven by a successful command: it is proven only by a "
        "restore rehearsal on a clean machine."
    )
    requires_system_checks = []  # the restore itself re-applies grants and checks security

    def add_arguments(self, parser):
        parser.add_argument("backup", help="Path of a .cbk file, or a name at --destination.")
        parser.add_argument("--destination", help="'local:<folder>' to fetch BACKUP from.")
        parser.add_argument(
            "--target-database", help="Database to restore into (default: the configured one)."
        )
        parser.add_argument(
            "--replace", action="store_true", help="Allow replacing a non-empty database."
        )
        parser.add_argument(
            "--safety-destination", help="Where the safety backup goes (default: settings)."
        )
        parser.add_argument("--skip-safety-backup", action="store_true")
        parser.add_argument("--confirm-no-safety-backup", default="")
        parser.add_argument(
            "--recovery-key-file",
            help="DISCOURAGED: a file holding the recovery key (otherwise typed hidden). Refused "
            "inside the application folder or when other users can read it.",
        )
        parser.add_argument(
            "--confirm", default="", help='Typed confirmation: "RESTORE <database>".'
        )
        parser.add_argument("--confirm-key", default="", help='Typed confirmation: "RESTORE KEY".')

    def handle(self, *args, **options):
        if settings.DB_ROLE != "owner":
            raise CommandError(
                "Restore needs the owner role: add --settings=config.settings.migrate"
            )
        name = options["target_database"] or connections.databases["default"]["NAME"]
        alias = restore.database_alias(name)
        conn = connections[alias]
        expected = f"RESTORE {name}"
        typed = options["confirm"] or input(f'Type "{expected}" to restore into database {name}: ')
        if typed.strip() != expected:
            raise CommandError("Cancelled: the confirmation did not match.")
        with tempfile.TemporaryDirectory(
            prefix="coven-fetch-", dir=settings.BACKUP_TMP_DIR or None
        ) as fetch:
            path = self.locate(options, Path(fetch))
            try:
                header_fp = crypto.read_header(path)[2]
            except crypto.DecryptionError as exc:
                raise CommandError(str(exc)) from exc
            recovery, confirm_key = self.key_inputs(options, header_fp)
            safety = None
            if options["replace"] and not options["skip_safety_backup"]:
                safety = get_destination(options.get("safety_destination"))
            if (
                options["skip_safety_backup"]
                and options["confirm_no_safety_backup"] != "NO SAFETY BACKUP"
            ):
                raise CommandError(
                    '--skip-safety-backup needs --confirm-no-safety-backup "NO SAFETY BACKUP"'
                )
            try:
                result = restore.restore(
                    encrypted=path, conn=conn, alias=alias, recovery_text=recovery,
                    confirm_key_import=confirm_key, replace=options["replace"],
                    safety_destination=safety, skip_safety_backup=options["skip_safety_backup"],
                )  # fmt: skip
            except restore.RestoreError as exc:
                raise CommandError(f"Restore refused or failed: {exc}") from exc
        for step in result.steps:
            mark = self.style.SUCCESS("ok  ") if step.ok else self.style.ERROR("FAIL")
            self.stdout.write(f"{mark} {step.name}" + (f" - {step.detail}" if step.detail else ""))
        if not result.ok:
            raise CommandError(
                "The data was restored but verification FAILED. Do not use this database."
            )
        self.stdout.write(
            self.style.SUCCESS(
                "Restore verified: row counts, migrations, ledger checks and "
                "database security all passed."
            )
        )
        self.stdout.write(
            "This does not prove that a replacement computer works end to end; recovery is "
            "verified only by a rehearsal on a clean machine (docs/OPERATIONS.md)."
        )

    def locate(self, options, fetch):
        spec = options.get("destination")
        name = options["backup"]
        if not spec:
            path = Path(name)
            if not path.is_file():
                raise CommandError(f"No such backup file: {name}")
            return path
        try:
            destination = get_destination(spec)
            if name == "latest":
                names = destination.list()
                if not names:
                    raise CommandError("There is no backup at that destination.")
                name = names[-1]
            target = fetch / Path(name).name
            destination.download(name, target)
        except DestinationError as exc:
            raise CommandError(str(exc)) from exc
        return target

    def read_key_file(self, name):
        """A recovery key in a plain file is risky (it stays on disk). It is only accepted for
        rehearsals: never inside the application folder, never readable by other users where the
        system can tell, and the owner is reminded to delete it. The hidden prompt is preferred."""
        path = Path(name).resolve()
        if Path(settings.BASE_DIR).resolve() in path.parents:
            raise CommandError(
                "Refused: a recovery key file must not be kept inside the application folder."
            )
        if os.name == "posix" and path.stat().st_mode & 0o077:
            raise CommandError(
                "Refused: the recovery key file is readable by other users (chmod 600 it)."
            )
        self.stderr.write(
            "WARNING: a recovery key in a file stays on disk. Delete this file now; next time "
            "type the key at the hidden prompt instead."
        )
        return path.read_text("utf-8")

    def key_inputs(self, options, header_fp):
        """(recovery text or None, confirmed). The credential store is used when its key fits."""
        if keystore.find_key(header_fp) is not None:
            return None, False
        self.stdout.write(
            f"This backup needs the key with fingerprint {header_fp}, "
            "which is not in the credential store."
        )
        if options["recovery_key_file"]:
            text = self.read_key_file(options["recovery_key_file"])
        else:
            text = getpass.getpass("Recovery key (typed characters are hidden): ")
        confirm = options["confirm_key"] or input(
            'Type "RESTORE KEY" to put this key into the credential store: '
        )
        if confirm.strip() != "RESTORE KEY":
            raise CommandError("Cancelled: key restoration was not confirmed.")
        return text, True
