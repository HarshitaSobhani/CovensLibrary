from django.core.management.base import BaseCommand, CommandError

from communications import services


class Command(BaseCommand):
    help = (
        "Deployment preflight (D-27b). Lists notification records of ANONYMISED members that still "
        "hold personal data (subject, body, an address that should be gone). Without --apply it "
        "only reports and exits non-zero when anything is found. With --apply it redacts them "
        "(the same redaction the anonymisation uses), then re-checks and fails if anything "
        "remains. Run it BEFORE enabling notification processing on a database that already "
        "contains notification records."
    )

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Redact what is found.")

    def handle(self, *args, **options):
        found = services.find_unredacted()
        self.stdout.write(f"Records with personal data of anonymised members: {found.count()}")
        for note in found[:50]:
            self.stdout.write(
                f"  id={note.pk} member={note.member_id} status={note.status} "
                f"has_body={bool(note.body)} has_address={bool(note.recipient_email)}"
            )
        if not found.exists():
            self.stdout.write(self.style.SUCCESS("Nothing to redact."))
            return
        if not options["apply"]:
            raise CommandError("Personal data remains. Re-run with --apply to redact it.")
        result = services.redact_all_anonymised()
        self.stdout.write(f"Redacted: {result}")
        remaining = services.find_unredacted().count()
        if remaining:
            raise CommandError(f"{remaining} record(s) still hold personal data after --apply.")
        self.stdout.write(self.style.SUCCESS("Verified: nothing remains."))
