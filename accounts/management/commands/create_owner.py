from getpass import getpass

from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from audit.services import record


class Command(BaseCommand):
    help = (
        "Create the one and only owner account (single-owner rule, PRD 4.1). Refuses if any "
        "account already exists. Use this instead of createsuperuser."
    )

    def add_arguments(self, parser):
        parser.add_argument("--username", required=True)
        parser.add_argument("--email", required=True, help="Used for password-reset email.")

    def handle(self, *args, **options):
        User = get_user_model()
        with transaction.atomic():
            if User.objects.select_for_update().exists():
                raise CommandError("An owner account already exists. Only one account is allowed.")
            password = getpass("Password: ")
            if password != getpass("Password (again): "):
                raise CommandError("The passwords did not match.")
            try:
                validate_password(password)
            except ValidationError as exc:
                raise CommandError("; ".join(exc.messages)) from None
            user = User.objects.create_user(
                username=options["username"],
                email=options["email"],
                password=password,
                is_staff=False,
                is_superuser=False,
            )
            record("owner.create", entity_type="user", entity_id=user.pk)
        self.stdout.write(f"Owner account '{user.get_username()}' created.")
