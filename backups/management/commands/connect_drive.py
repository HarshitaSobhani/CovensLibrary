import json

from django.core.management.base import BaseCommand, CommandError

from backups import keystore
from backups.destinations import SCOPES


class Command(BaseCommand):
    help = (
        "Connect Google Drive (drive.file scope). Needs the OAuth client ID and secret you created "
        "in Google Cloud (not stored in any file: they go to the credential store). Opens your "
        "browser on this computer (127.0.0.1) for consent. NOT verified against real Google."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--replace",
            action="store_true",
            help="Replace an existing Google connection (it is never replaced silently).",
        )
        parser.add_argument("--client-id", help="Asked for when left out.")
        parser.add_argument(
            "--client-secret",
            help="DISCOURAGED: a value given here is visible in the process list and in the "
            "shell history. Leave it out and it is asked for without being shown.",
        )

    def handle(self, *args, **options):
        from google_auth_oauthlib.flow import InstalledAppFlow

        existing = keystore.get_keystore()
        try:
            connected = existing.get(keystore.DRIVE_TOKEN) or existing.get(keystore.DRIVE_CLIENT)
        except keystore.KeyStoreError as exc:
            raise CommandError(f"{exc}. Nothing was changed.") from exc
        if connected and not options["replace"]:
            raise CommandError(
                "Google Drive is already connected. It is never replaced silently: add "
                "--replace if you really want to connect it again."
            )

        if not options["client_id"]:
            options["client_id"] = input("Google OAuth client ID: ").strip()
        if not options["client_secret"]:
            from getpass import getpass

            options["client_secret"] = getpass("Google OAuth client secret (hidden): ").strip()
        if not options["client_id"] or not options["client_secret"]:
            raise CommandError("The client ID and the client secret are both required.")

        config = {
            "installed": {
                "client_id": options["client_id"],
                "client_secret": options["client_secret"],
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": ["http://localhost"],
            }
        }
        flow = InstalledAppFlow.from_client_config(config, SCOPES)
        credentials = flow.run_local_server(host="127.0.0.1", port=0, open_browser=True)
        if not credentials.refresh_token:
            raise CommandError("Google returned no refresh token; revoke access and try again.")
        store = keystore.get_keystore()
        store.set(
            keystore.DRIVE_CLIENT,
            json.dumps(
                {"client_id": options["client_id"], "client_secret": options["client_secret"]}
            ),
        )
        store.set(keystore.DRIVE_TOKEN, json.dumps({"refresh_token": credentials.refresh_token}))
        self.stdout.write(
            "Google Drive is connected. The backup folder is created on the first backup."
        )
