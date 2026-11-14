import os
import signal
import sys
import threading

from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError

from core import runtime

LOOPBACK = "127.0.0.1"
MAX_BODY = 10 * 1024 * 1024  # imports are limited to 5 MB; this leaves room for the form
EXIT_ALREADY_RUNNING = 3
EXIT_PORT_BUSY = 4


class Command(BaseCommand):
    help = (
        "Run the application with waitress on 127.0.0.1 ONLY (decision B37: no LAN). Before "
        "serving it runs the daily jobs once (launch catch-up: overdue forfeitures, notifications, "
        "a missed backup); a failing job is reported and the server still starts. It records "
        "itself in COVEN_RUN_DIR and stops cleanly when 'Stop Library' asks (or on Ctrl+C)."
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
        port = options["port"]
        base = self.run_dir()
        self.refuse_second_instance(port, base)
        from config.wsgi import application  # refuses to serve on an insecure database setup

        if not options["no_catchup"]:
            self.catch_up()
        import waitress

        runtime.clear_stop(base)  # a stale request from an earlier run must not stop this one
        server = waitress.create_server(
            application,
            host=LOOPBACK,
            port=port,
            ipv4=True,
            ipv6=False,
            threads=options["threads"],
            url_scheme="http",
            ident="",
            max_request_body_size=MAX_BODY,
            channel_timeout=120,
        )
        runtime.write_instance(port, base)
        stopping = threading.Event()
        self.install_signal_handlers(stopping)
        watcher = threading.Thread(
            target=self.watch_for_stop, args=(server, stopping, base), daemon=True
        )
        watcher.start()
        self.stdout.write(f"Serving on http://{LOOPBACK}:{port}/ (Ctrl+C to stop)")
        self.stdout.flush()
        try:
            server.run()
        except (KeyboardInterrupt, SystemExit):
            stopping.set()
        except OSError:
            if not stopping.is_set():  # the socket was closed by the watcher: an orderly stop
                raise
        finally:
            stopping.set()
            try:
                server.close()
            except Exception:  # noqa: BLE001 - already closed
                pass
            runtime.clear_instance(base, pid=os.getpid())
            runtime.clear_stop(base)
        self.stdout.write("Stopped.")

    # -- helpers ----------------------------------------------------------------------------
    def run_dir(self):
        from django.conf import settings

        return runtime.run_dir(settings.COVEN_RUN_DIR)

    def refuse_second_instance(self, port, base):
        """Never start a second copy, never take over a port something else holds."""
        state = runtime.health(port)
        if state in ("ok", "unhealthy"):
            self.stderr.write("Coven's Library is already running.")
            sys.exit(EXIT_ALREADY_RUNNING)
        if state == "foreign":
            self.stderr.write(f"Port {port} is in use by another program.")
            sys.exit(EXIT_PORT_BUSY)
        recorded = runtime.read_instance(base)
        if recorded is not None:  # the server that wrote it is gone: clear the stale file
            runtime.clear_instance(base)

    def install_signal_handlers(self, stopping):
        def stop(*_):
            stopping.set()

        for name in ("SIGTERM", "SIGBREAK"):  # SIGBREAK: Ctrl+Break / CTRL_BREAK_EVENT on Windows
            sig = getattr(signal, name, None)
            if sig is not None:
                try:
                    signal.signal(sig, stop)
                except (ValueError, OSError):  # not the main thread
                    pass

    def watch_for_stop(self, server, stopping, base):
        """Ends the server when 'Stop Library' (or a signal) asks: stop accepting, let running
        requests finish, then close."""
        while not stopping.wait(0.5):
            if runtime.stop_requested(base):
                break
        stopping.set()
        try:
            server.task_dispatcher.shutdown()  # waits for requests that are being served
        except Exception:  # noqa: BLE001
            pass
        try:
            server.close()
        except Exception:  # noqa: BLE001
            pass

    def catch_up(self):
        try:
            call_command("run_daily_jobs", stdout=self.stdout)
        except Exception as exc:  # noqa: BLE001 - the owner must still be able to open the application
            self.stderr.write(
                f"Launch catch-up failed ({type(exc).__name__}); the server starts anyway."
            )
