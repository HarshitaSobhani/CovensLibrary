from django.conf import settings
from django.core.management.base import CommandError
from django.core.management.commands.migrate import Command as DjangoMigrate
from django.db import connections

from core import dbsecurity


class Command(DjangoMigrate):
    """Django's migrate, restricted to the owner role, with a security re-check afterwards.

    * The runtime role cannot (and must not) change the schema: running migrations with runtime
      settings is refused with a clear instruction.
    * The database-security system check is skipped *before* migrating (the grants may legitimately
      be out of date until this very command re-applies them in post_migrate) and enforced
      *afterwards* in strict mode, so a migration can never leave the database insecure.
    """

    requires_system_checks = []  # see the class docstring: checked after, not before

    def handle(self, *args, **options):
        if settings.DB_ROLE != "owner":
            raise CommandError(
                "Migrations must run as the database owner role. Use: "
                "python manage.py migrate --settings=config.settings.migrate"
            )
        result = super().handle(*args, **{**options, "skip_checks": True})
        # Enforce after a full forward migration only: a deliberate rollback of one app legitimately
        # leaves its tables/triggers absent until it is migrated forward again.
        full_forward = not options.get("app_label") and not options.get("plan")
        if full_forward and settings.DB_SECURITY_MODE == "strict" and settings.DB_RUNTIME_ROLE:
            findings = dbsecurity.inspect(
                connections[options["database"]], settings.DB_RUNTIME_ROLE, "owner"
            )
            errors = [f for f in findings if f.level == "error"]
            if errors:
                raise CommandError(
                    "Database security check failed: " + dbsecurity.summarise(findings)
                )
        return result
