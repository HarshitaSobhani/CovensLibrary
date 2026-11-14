import getpass

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from backups import keystore, rotation, services
from backups.destinations import DestinationError


class Command(BaseCommand):
    help = (
        "Rotate the backup key SAFELY: a new key for new backups, the old key kept (and proven) "
        "so every older backup still restores. --dry-run only checks. An interrupted rotation is "
        "finished with --resume or cancelled (before the commit) with --abort; --set-current "
        "swaps back (rollback). See docs/KEY_ROTATION_AND_RECOVERY.md. Have a verified backup "
        "and your paper recovery key in hand first."
    )

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Check only; change nothing.")
        parser.add_argument("--resume", action="store_true")
        parser.add_argument("--abort", action="store_true")
        parser.add_argument(
            "--set-current", metavar="FINGERPRINT", help="Make a retired key current."
        )
        parser.add_argument(
            "--destination", help="'drive' or 'local:<folder>' (default: settings)."
        )
        parser.add_argument("--confirm", default="", help="The typed confirmation text.")

    # -- prompts (replaced in tests) ----------------------------------------------------------
    def ask_current_recovery_key(self):
        """Hidden input only: there is deliberately no option to read it from a file."""
        return getpass.getpass("Type the CURRENT recovery key from your paper copy (hidden): ")

    def show_and_ask_new_recovery_key(self, text):
        """The new key is shown ONCE so it can be written down; typing it back is hidden, so it is
        never echoed a second time (and never in a log: only this stdout line carries it)."""
        self.stdout.write(self.style.WARNING("NEW RECOVERY KEY (shown once; write it down now):"))
        self.stdout.write(text)
        self.stdout.write("Keep it away from this computer. KEEP the old paper key too.")
        return getpass.getpass(
            "Type the new recovery key back (hidden) to prove you wrote it down: "
        )

    def handle(self, *args, **options):
        destination = services.get_destination(options["destination"])
        try:
            if options["abort"]:
                typed = options["confirm"] or input(f'Type "{rotation.CONFIRM_ABORT}": ')
                self.report(rotation.abort(confirm=typed.strip(), destination=destination))
            elif options["set_current"]:
                typed = options["confirm"] or input(f'Type "{rotation.CONFIRM_SET_CURRENT}": ')
                self.report(rotation.set_current(options["set_current"], confirm=typed.strip()))
            elif options["resume"]:
                self.report(
                    rotation.resume(
                        new_recovery_prompt=self.show_and_ask_new_recovery_key,
                        destination=destination,
                    )
                )
            elif options["dry_run"]:
                self.dry_run(destination)
            else:
                self.run_rotation(options, destination)
        except services.BackupBusy as exc:
            raise CommandError(
                "A backup is running right now; nothing was lost. Wait for it to finish, then "
                "run rotate_backup_key --resume (or try again)."
            ) from exc
        except (rotation.RotationError, keystore.KeyStoreError, DestinationError) as exc:
            raise CommandError(str(exc)) from exc

    def dry_run(self, destination):
        report = rotation.preflight(destination=destination, now=timezone.now())
        for check in report.checks:
            mark = "ok    " if check.ok else "FAILED"
            self.stdout.write(f"{mark} {check.name}" + ("" if check.ok else f" - {check.detail}"))
        self.stdout.write(
            "Dry run: nothing was changed. " + ("Ready to rotate." if report.ok else "Not ready.")
        )
        if not report.ok:
            raise CommandError("The preflight did not pass.")

    def run_rotation(self, options, destination):
        typed = options["confirm"] or input(f'Type "{rotation.CONFIRM_ROTATE}" to rotate the key: ')
        old_text = self.ask_current_recovery_key()
        result = rotation.rotate(
            confirm=typed.strip(),
            old_recovery_text=old_text,
            new_recovery_prompt=self.show_and_ask_new_recovery_key,
            destination=destination,
        )
        self.report(result)

    def report(self, result):
        # fingerprints are public identifiers; no key material is ever printed here
        for name, value in result.items():
            self.stdout.write(f"{name}: {value}")
        self.stdout.write(
            "Done. KEEP every paper recovery key until 'prune_backup_keys' says an old key is no "
            "longer needed by any backup."
        )
