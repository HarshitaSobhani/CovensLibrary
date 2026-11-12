import json

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection
from django.db.utils import OperationalError

from core import dbsecurity


class Command(BaseCommand):
    help = (
        "Verify the two-role database security model (ownership, grants, table classification, "
        "append-only triggers). Exits non-zero on any error. Prints no credentials."
    )

    # These commands exist to fix or report the very state the system check validates.
    requires_system_checks = []

    def add_arguments(self, parser):
        parser.add_argument("--json", action="store_true", help="Machine-readable output.")

    def handle(self, *args, **options):
        kind = "owner" if settings.DB_ROLE == "owner" else "runtime"
        try:
            findings = dbsecurity.inspect(connection, settings.DB_RUNTIME_ROLE, kind)
        except OperationalError:
            raise CommandError("The database is unreachable; cannot verify its security.") from None
        errors = [f for f in findings if f.level == "error"]
        if options["json"]:
            self.stdout.write(json.dumps([f._asdict() for f in findings], indent=2))
        else:
            for f in findings:
                self.stdout.write(f"{f.level.upper():7} {f.code} {f.message}")
            self.stdout.write(
                f"Checked as the {kind} role, mode={settings.DB_SECURITY_MODE}: "
                f"{len(errors)} error(s), {len(findings) - len(errors)} warning(s)."
            )
        if errors:
            raise CommandError("Database security check failed.")
