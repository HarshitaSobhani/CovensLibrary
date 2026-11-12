from django.core.management.base import BaseCommand

from circulation.forfeiture import process_overdue_forfeitures


class Command(BaseCommand):
    help = (
        "Daily job (Windows Task Scheduler is wired up in Phase 7c) and application-launch "
        "catch-up. 1) Post the automatic day-8 token forfeitures that are due (a forfeiture "
        "missed while the application was off keeps its ORIGINAL effective date and records the "
        "real processing time). 2) Run the notification cycle: turn outbox events into emails "
        "(including the forfeiture notice, immediately after processing), create today's 09:00 "
        "reminders and send what is due. Idempotent; run it by hand until Phase 7c."
    )

    def handle(self, *args, **options):
        result = process_overdue_forfeitures()
        self.stdout.write(
            "Overdue forfeitures: checked {checked}, forfeited {forfeited} "
            "({late} processed late), waiting for an owner decision {skipped_decision}, "
            "no held token {skipped_token}.".format(**result)
        )
        from communications.services import run_cycle

        cycle = run_cycle()
        sent = ", ".join(f"{k}: {v}" for k, v in sorted(cycle["sent"].items())) or "none"
        self.stdout.write(
            f"Notifications: {cycle['consumed']} event(s) consumed, "
            f"{cycle['reminders']} reminder(s) created, sent: {sent}, "
            f"{cycle['redacted']} retained address(es) erased."
        )
