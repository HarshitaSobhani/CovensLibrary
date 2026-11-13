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
        parser.add_argument("--client-id", required=True)
        parser.add_argument("--client-secret", required=True)

    def handle(self, *args, **options):
        from google_auth_oauthlib.flow import InstalledAppFlow

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
