from django.core.management.base import BaseCommand

from backups import services


class Command(BaseCommand):
    help = "Apply the retention rules (30 daily + 12 monthly). --dry-run only reports."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--destination")

    def handle(self, *args, **options):
        from backups.destinations import get_destination

        destination = None if options["dry_run"] else get_destination(options.get("destination"))
        result = services.apply_retention(destination=destination, dry_run=options["dry_run"])
        self.stdout.write(f"Retention: {result}")
