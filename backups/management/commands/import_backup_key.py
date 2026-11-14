import getpass

from django.core.management.base import BaseCommand, CommandError

from backups import keystore, rotation, services
from backups.destinations import DestinationError


class Command(BaseCommand):
    help = (
        "Put a backup key back into the credential store from its PAPER recovery key (for example "
        "a pending key that went missing during an unfinished rotation, or an old key on a new "
        "computer). The recovery key is typed hidden and checked; it must open a real backup (at "
        "the destination or a file you name); nothing existing is overwritten; only the "
        "fingerprint is printed or recorded. See docs/KEY_ROTATION_AND_RECOVERY.md."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--as",
            dest="slot",
            choices=["retired", "pending"],
            default="retired",
            help="'pending' only for a rotation left unfinished (then run rotate_backup_key "
            "--resume); otherwise 'retired' (then --set-current if it is the newest key).",
        )
        parser.add_argument("--backup-file", help="A local .cbk made with this key (the proof).")
        parser.add_argument(
            "--destination", help="'drive' or 'local:<folder>' (default: settings)."
        )
        parser.add_argument("--confirm", default="", help="The typed confirmation text.")

    def handle(self, *args, **options):
        try:
            destination = None
            if not options["backup_file"]:
                destination = services.get_destination(options["destination"])
            typed = options["confirm"] or input(f'Type "{rotation.CONFIRM_IMPORT}": ')
            text = getpass.getpass("Recovery key from your paper copy (hidden): ")
            result = rotation.import_key(
                text,
                confirm=typed.strip(),
                as_slot=options["slot"],
                destination=destination,
                backup_file=options["backup_file"],
            )
        except (rotation.RotationError, keystore.KeyStoreError, DestinationError) as exc:
            raise CommandError(str(exc)) from exc
        for name, value in result.items():
            self.stdout.write(f"{name}: {value}")
        if result["slot"] == "pending":
            self.stdout.write("Now run: rotate_backup_key --resume")
        else:
            self.stdout.write(
                "If this is the key of your newest backup, make it current with: "
                f"rotate_backup_key --set-current {result['imported']}"
            )
