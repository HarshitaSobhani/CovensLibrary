from django.core.management.base import BaseCommand
from django.utils import timezone

from backups import crypto, keystore, services


class Command(BaseCommand):
    help = "Show backup health: last success, age, problems, recent failures, retention plan."

    def handle(self, *args, **options):
        now = timezone.now()
        try:
            key = keystore.load_working_key()
            self.stdout.write(
                f"Backup key: present (fingerprint {crypto.fingerprint(key)})"
                if key
                else "Backup key: MISSING (run setup_backup_key)"
            )
        except keystore.KeyStoreError as exc:
            self.stdout.write(f"Backup key: UNAVAILABLE ({exc})")
        info = services.health(now)
        last = info["last_success"]
        if last:
            when = timezone.localtime(last.started_at)
            self.stdout.write(f"Last successful backup: {when:%d %b %Y %H:%M} ({last.file_name})")
        for problem in info["problems"]:
            self.stdout.write(self.style.ERROR(problem))
        for run in info["failures"]:
            self.stdout.write(
                self.style.WARNING(
                    f"Failed {timezone.localtime(run.started_at):%d %b %H:%M}: {run.error_summary}"
                )
            )
        due, trigger = services.backup_due(now)
        self.stdout.write(f"Backup due now: {'yes (' + trigger + ')' if due else 'no'}")
        keep, remove = services.retention_plan(now)
        self.stdout.write(f"Retention: keep {len(keep)}, would remove {len(remove)}.")
