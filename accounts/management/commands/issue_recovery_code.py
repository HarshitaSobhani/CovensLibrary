from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from accounts.recovery import EXPIRY, issue_code


class Command(BaseCommand):
    help = (
        "Local recovery: print a single-use password-recovery code (shown once, valid 15 minutes). "
        "Enter it on the /accounts/recover/ page."
    )

    def add_arguments(self, parser):
        parser.add_argument("--username", required=True)

    def handle(self, *args, **options):
        user = get_user_model().objects.filter(username=options["username"], is_active=True).first()
        if user is None:
            raise CommandError("No active account with that username.")
        code = issue_code(user)
        minutes = int(EXPIRY.total_seconds() // 60)
        self.stdout.write(
            f"Recovery code (shown once, valid {minutes} minutes, single use): {code}"
        )
