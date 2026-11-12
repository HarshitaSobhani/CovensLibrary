from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction

from core import dbsecurity


class Command(BaseCommand):
    help = (
        "Apply the least-privilege grants for the runtime role (idempotent). Run as the OWNER "
        "role: python manage.py apply_db_grants --settings=config.settings.migrate"
    )

    # These commands exist to fix or report the very state the system check validates.
    requires_system_checks = []

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run", action="store_true", help="Print the SQL without running it."
        )

    def handle(self, *args, **options):
        if settings.DB_ROLE != "owner":
            raise CommandError(
                "Run this command with --settings=config.settings.migrate (owner role)."
            )
        role = settings.DB_RUNTIME_ROLE
        try:
            if options["dry_run"]:
                for line in dbsecurity.render(connection, dbsecurity.plan_grants(connection, role)):
                    self.stdout.write(line + ";")
                return
            with transaction.atomic():
                statements = dbsecurity.apply_grants(connection, role)
        except ImproperlyConfigured as exc:
            raise CommandError(str(exc)) from None
        self.stdout.write(f"Applied {len(statements)} statements for runtime role '{role}'.")
