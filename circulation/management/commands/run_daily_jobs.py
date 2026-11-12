from django.core.management.base import BaseCommand

from circulation.forfeiture import process_overdue_forfeitures


class Command(BaseCommand):
    help = (
        "Daily job (Windows Task Scheduler) and application-launch catch-up: post the automatic "
        "day-8 token forfeitures that are due. Idempotent; a forfeiture missed while the "
        "application was off keeps its ORIGINAL effective date (day 8) and records the real "
        "processing time. Later phases add reminders and backups to this one command."
    )

    def handle(self, *args, **options):
        result = process_overdue_forfeitures()
        self.stdout.write(
            "Overdue forfeitures: checked {checked}, forfeited {forfeited} "
            "({late} processed late), waiting for an owner decision {skipped_decision}, "
            "no held token {skipped_token}.".format(**result)
        )
