from django.core.management.base import BaseCommand, CommandError

from audit.services import record
from backups import crypto, keystore


class Command(BaseCommand):
    help = (
        "Create the random backup key (once) and store it in the Windows Credential Manager. The "
        "RECOVERY KEY is printed once: write it down and keep it OFF this computer and away from "
        "the backups. If it is lost and this computer is lost, the backups cannot be opened."
    )

    def add_arguments(self, parser):
        parser.add_argument("--yes", action="store_true", help="Do not ask for confirmation.")

    def handle(self, *args, **options):
        store = keystore.get_keystore()
        try:
            existing = keystore.load_working_key(store)
        except keystore.KeyStoreError as exc:
            raise CommandError(
                f"{exc}. A new key is NEVER created automatically while the existing one cannot "
                "be read: fix the credential store or use the recovery key."
            ) from exc
        if existing is not None:
            raise CommandError(
                "A backup key already exists. It is never replaced automatically (older backups "
                "need it). Use show_recovery_key to see its recovery key again."
            )
        if not options["yes"]:
            answer = input("Type CREATE to generate the backup key: ")
            if answer.strip() != "CREATE":
                raise CommandError("Cancelled.")
        key = keystore.generate_key()
        keystore.save_working_key(key, store)
        record(
            "backup.key_created",
            entity_type="backup",
            summary={"fingerprint": crypto.fingerprint(key)},
        )
        self.stdout.write(
            self.style.WARNING("RECOVERY KEY (shown only now; store it away from this computer):")
        )
        self.stdout.write(keystore.recovery_key_text(key))
        self.stdout.write(f"Key fingerprint (not secret): {crypto.fingerprint(key)}")
