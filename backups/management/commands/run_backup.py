from django.core.management.base import BaseCommand, CommandError

from backups import services


class Command(BaseCommand):
    help = "Make a backup now (database + media + manifest, encrypted, uploaded and verified)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--destination", help="'drive' or 'local:<folder>' (default: settings)."
        )

    def handle(self, *args, **options):
        from backups.destinations import get_destination

        try:
            destination = get_destination(options.get("destination"))
            run = services.run_backup(trigger="manual", destination=destination)
        except services.BackupBusy as exc:
            raise CommandError(str(exc)) from exc
        if run.status != "succeeded":
            raise CommandError(f"Backup failed: {run.error_summary}")
        self.stdout.write(
            f"Backup succeeded: {run.file_name} ({run.size_bytes} bytes), stored at "
            f"{run.destination} and verified. Key fingerprint {run.key_fingerprint}."
        )
