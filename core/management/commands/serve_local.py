from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError

LOOPBACK = "127.0.0.1"
MAX_BODY = 10 * 1024 * 1024  # imports are limited to 5 MB; this leaves room for the form


class Command(BaseCommand):
    help = (
        "Run the application with waitress on 127.0.0.1 ONLY (decision B37: no LAN). Before "
        "serving it runs the daily jobs once (launch catch-up: overdue forfeitures, notifications, "
        "a missed backup); a failing job is reported and the server still starts."
    )
    requires_system_checks = []  # wsgi.py refuses an insecure database setup itself

    def add_arguments(self, parser):
        parser.add_argument("--host", default=LOOPBACK, help="Only 127.0.0.1 is accepted.")
        parser.add_argument("--port", type=int, default=8000)
        parser.add_argument("--threads", type=int, default=4)
        parser.add_argument("--no-catchup", action="store_true", help="Skip the launch catch-up.")

    def handle(self, *args, **options):
        if options["host"] != LOOPBACK:
            raise CommandError(
                "The application listens on 127.0.0.1 only; it is never exposed to the network."
            )
        if not 1024 <= options["port"] <= 65535:
            raise CommandError("Choose a port between 1024 and 65535.")
        if not 1 <= options["threads"] <= 16:
            raise CommandError("Choose between 1 and 16 threads.")
        from config.wsgi import application  # refuses to serve on an insecure database setup

        if not options["no_catchup"]:
            self.catch_up()
        import waitress

        self.stdout.write(f"Serving on http://{LOOPBACK}:{options['port']}/ (Ctrl+C to stop)")
        waitress.serve(
            application,
            host=LOOPBACK,
            port=options["port"],
            ipv4=True,
            ipv6=False,
            threads=options["threads"],
            url_scheme="http",
            ident="",
            max_request_body_size=MAX_BODY,
            channel_timeout=120,
        )

    def catch_up(self):
        try:
            call_command("run_daily_jobs", stdout=self.stdout)
        except Exception as exc:  # noqa: BLE001 - the owner must still be able to open the application
            self.stderr.write(
                f"Launch catch-up failed ({type(exc).__name__}); the server starts anyway."
            )
