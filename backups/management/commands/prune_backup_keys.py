from django.core.management.base import BaseCommand, CommandError

from backups import keystore, rotation, services
from backups.destinations import DestinationError


class Command(BaseCommand):
    help = (
        "List the old (retired) backup keys and whether each can be removed; remove one ONLY when "
        "the backup destination itself (every file's header) and the database history both show "
        "that no backup needs it. Refuses when the destination cannot be fully listed or inspected "
        "or when it and the history disagree. Never runs by itself. Keep the paper recovery key "
        "until you are sure no backup copy you hold anywhere still needs it."
    )

    def add_arguments(self, parser):
        parser.add_argument("--fingerprint", help="The retired key to remove.")
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument(
            "--destination", help="'drive' or 'local:<folder>' (default: settings)."
        )
        parser.add_argument("--confirm", default="")

    def handle(self, *args, **options):
        try:
            destination = services.get_destination(options["destination"])
            report = rotation.prune_report(destination=destination)
            for item in report:
                if item.safe:
                    self.stdout.write(
                        f"{item.fingerprint}: CAN be removed ({'; '.join(item.notes)})"
                    )
                else:
                    self.stdout.write(f"{item.fingerprint}: KEPT")
                    for reason in item.blockers:
                        self.stdout.write(f"    - {reason}")
            if not report:
                self.stdout.write("There are no retired keys.")
            if options["dry_run"] or not options["fingerprint"]:
                return
            typed = options["confirm"] or input(f'Type "{rotation.CONFIRM_REMOVE}": ')
            result = rotation.prune(
                options["fingerprint"], confirm=typed.strip(), destination=destination
            )
            self.stdout.write(
                f"Removed {result['removed']} from the credential store. You may now destroy the "
                "matching paper recovery key only if you hold no other copy of a backup made "
                "with it."
            )
        except (rotation.RotationError, keystore.KeyStoreError, DestinationError) as exc:
            raise CommandError(str(exc)) from exc
