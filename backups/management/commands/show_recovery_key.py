from django.core.management.base import BaseCommand, CommandError

from audit.services import record
from backups import keystore, rotation


class Command(BaseCommand):
    help = (
        "Show the recovery key of the CURRENT backup key again (typed confirmation required). "
        "--all shows every key that retained backups may need (current and retired), each labelled "
        "with its fingerprint and the backups it protects: keep one paper copy per key."
    )

    def add_arguments(self, parser):
        parser.add_argument("--yes", action="store_true", help="Do not ask for confirmation.")
        parser.add_argument("--all", action="store_true", help="Every key, with its fingerprint.")

    def handle(self, *args, **options):
        try:
            key = keystore.load_working_key()
        except keystore.KeyStoreError as exc:
            raise CommandError(str(exc)) from exc
        if key is None:
            raise CommandError("No backup key exists yet (run setup_backup_key).")
        if not options["yes"]:
            answer = input("Type SHOW to display the recovery key on this screen: ")
            if answer.strip() != "SHOW":
                raise CommandError("Cancelled.")
        if not options["all"]:
            record(
                "backup.recovery_key_shown",
                entity_type="backup",
                summary={"fingerprints": [keystore.key_fingerprint(key)]},
            )
            self.stdout.write(keystore.recovery_key_text(key))
            return
        bundle = rotation.recovery_bundle()
        record(
            "backup.recovery_key_shown",
            entity_type="backup",
            summary={"fingerprints": [item["fingerprint"] for item in bundle], "all": True},
        )
        for item in bundle:
            span = ""
            if item["first"]:
                first, last = item["first"], item["last"]
                span = f", {item['backups']} backup(s) from {first:%d %b %Y} to {last:%d %b %Y}"
            self.stdout.write(
                f"--- {item['role'].upper()}  fingerprint {item['fingerprint']}{span}"
            )
            self.stdout.write(item["recovery"])
