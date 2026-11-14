import sys

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.db.utils import OperationalError

from core import dbsecurity
from core.preflight import OK, Problem, ok_line


class Command(BaseCommand):
    help = (
        "Read-only start-up checks used by the Windows launcher: database login, applied "
        "migrations, database security, backup key (never created here) and display files. "
        "Prints one JSON line; the exit code names the problem (core/preflight.py)."
    )
    requires_system_checks = []

    def handle(self, *args, **options):
        problem = self.check()
        self.stdout.write(problem.line() if problem else ok_line())
        sys.exit(problem.exit_code if problem else OK)

    def check(self):
        try:
            connection.ensure_connection()
        except OperationalError as exc:
            text = str(exc).lower()
            code = "DB_AUTH" if "password" in text or "authentication" in text else "DB_DOWN"
            return Problem(code, f"database connection failed ({type(exc).__name__})")
        try:
            executor = MigrationExecutor(connection)
            plan = executor.migration_plan(executor.loader.graph.leaf_nodes())
        except Exception as exc:  # noqa: BLE001
            return Problem("OTHER", f"migration state could not be read ({type(exc).__name__})")
        if plan:
            return Problem("MIGRATIONS", f"{len(plan)} migration(s) are not applied")
        if settings.DB_RUNTIME_ROLE:
            kind = "owner" if settings.DB_ROLE == "owner" else "runtime"
            findings = [
                f
                for f in dbsecurity.inspect(connection, settings.DB_RUNTIME_ROLE, kind)
                if f.level == "error"
            ]
            if findings:
                return Problem("DB_SECURITY", "; ".join(sorted({f.code for f in findings})))
        problem = self.check_key()
        if problem:
            return problem
        root = settings.STATIC_ROOT
        if not settings.WHITENOISE_USE_FINDERS and not (root / "css" / "app.css").is_file():
            return Problem("STATIC", "collectstatic has not been run")
        return None

    def check_key(self):
        """The backup key must be readable, valid, and the key of the library's newest backup must
        be available. Nothing is ever created here."""
        from backups import keystore, rotation

        try:
            stored = keystore.get_keystore().get(keystore.WORKING_KEY)
        except keystore.KeyStoreError as exc:
            return Problem("KEY_UNAVAILABLE", str(exc))
        if not stored:
            return Problem("KEY_MISSING", "no backup working key in the credential store")
        try:
            keystore.decode_key(stored)
        except keystore.KeyStoreError as exc:
            return Problem("KEY_UNAVAILABLE", str(exc))
        try:
            newest = rotation.current_fingerprint()
            if newest and keystore.find_key(newest) is None:
                return Problem(
                    "KEY_MISMATCH", f"the key of the newest backup ({newest}) is not in the store"
                )
        except keystore.KeyStoreError as exc:
            return Problem("KEY_UNAVAILABLE", str(exc))
        return None
