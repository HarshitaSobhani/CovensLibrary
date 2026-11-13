from django.core.management.base import BaseCommand, CommandError

from audit.services import record
from backups import crypto, keystore


class Command(BaseCommand):
    help = "Show the recovery key of the existing backup key again (typed confirmation required)."

    def add_arguments(self, parser):
        parser.add_argument("--yes", action="store_true", help="Do not ask for confirmation.")

    def handle(self, *args, **options):
        key = keystore.load_working_key()
        if key is None:
            raise CommandError("No backup key exists yet (run setup_backup_key).")
        if not options["yes"]:
            answer = input("Type SHOW to display the recovery key on this screen: ")
            if answer.strip() != "SHOW":
                raise CommandError("Cancelled.")
        record(
            "backup.recovery_key_shown",
            entity_type="backup",
            summary={"fingerprint": crypto.fingerprint(key)},
        )
        self.stdout.write(keystore.recovery_key_text(key))
