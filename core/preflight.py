"""Start-up checks for the launcher. Read-only: they never run migrations, create keys, change
permissions or restore anything.

Two stages so that the most useful message is given even when the application cannot load:
* ``stage_one()``  standard library only (no Django): configuration, secrets, PostgreSQL reachable.
* ``manage.py launcher_preflight`` (stage two) needs Django: database login, migrations, database
  security, the backup key and the collected static files.

Each failure is a ``Problem`` with a stable code, an exit code and plain-language text for the
owner. Technical detail goes to the protected log, never to the owner's screen.
"""

import json
import os
import socket
import sys
from dataclasses import dataclass

OK = 0
CODES = {
    "CONFIG": 20,
    "SECRETS": 21,
    "DB_DOWN": 22,
    "DB_AUTH": 23,
    "MIGRATIONS": 24,
    "DB_SECURITY": 25,
    "KEY_MISSING": 26,
    "KEY_UNAVAILABLE": 27,
    "STATIC": 28,
    "OTHER": 29,
    "STORE_UNAVAILABLE": 30,
    "KEY_MISMATCH": 31,
}
OWNER_TEXT = {
    "CONFIG": "The application is not set up correctly on this computer.",
    "SECRETS": "The saved database or security settings are missing.",
    "DB_DOWN": "The database is not running.",
    "DB_AUTH": "The application could not sign in to the database.",
    "MIGRATIONS": "The application needs an update before it can start.",
    "DB_SECURITY": "The database security check failed.",
    "KEY_MISSING": "The backup protection key is missing on this computer.",
    "KEY_UNAVAILABLE": "The backup protection key could not be read.",
    "STATIC": "The application's display files are missing.",
    "OTHER": "The application could not be checked.",
    "STORE_UNAVAILABLE": "The Windows credential store could not be read.",
    "KEY_MISMATCH": "The saved backup key does not match the library's newest backup.",
}


@dataclass
class Problem:
    code: str
    detail: str  # technical, for the protected log only (never contains a secret)

    @property
    def exit_code(self):
        return CODES[self.code]

    def line(self):
        return json.dumps(
            {"ok": False, "code": self.code, "owner": OWNER_TEXT[self.code], "detail": self.detail}
        )


def ok_line():
    return json.dumps({"ok": True})


def stage_one(environ=None, secret_lookup=None, connect=None):
    """Checks that need no Django. Returns a Problem or None."""
    env = os.environ if environ is None else environ
    if env.get("DJANGO_SETTINGS_MODULE") != "config.settings.production":
        return Problem("CONFIG", "DJANGO_SETTINGS_MODULE is not config.settings.production")
    for name in ("DB_NAME", "DB_USER"):
        if not env.get(name):
            return Problem("CONFIG", f"{name} is not set")
    if env.get("DB_USER") != "coven_app":
        return Problem("CONFIG", "DB_USER is not the runtime role coven_app")
    if env.get("DB_OWNER_USER") or env.get("DB_OWNER_PASSWORD"):
        return Problem("CONFIG", "owner database credentials are present in the environment")
    if (env.get("DJANGO_DEBUG") or "").strip().lower() in {"1", "true", "yes", "on"}:
        return Problem("CONFIG", "DJANGO_DEBUG is enabled")
    if env.get("COVEN_SECRET_STORE") != "credential-manager":
        return Problem("CONFIG", "COVEN_SECRET_STORE is not credential-manager")
    for name in ("DJANGO_SECRET_KEY", "DB_PASSWORD", "EMAIL_HOST_PASSWORD"):
        if env.get(name):  # a plaintext secret is refused, never used as a fallback
            return Problem("CONFIG", f"{name} is set as an environment variable (not allowed)")
    if secret_lookup is None:
        from config import secretstore

        def secret_lookup(name):
            return secretstore.get_checked(name)

    for name in ("DJANGO_SECRET_KEY", "DB_PASSWORD"):
        try:
            present = secret_lookup(name)
        except Exception as exc:  # noqa: BLE001 - the store itself is broken: fail closed
            return Problem(
                "STORE_UNAVAILABLE", f"credential store unreadable ({type(exc).__name__})"
            )
        if not present:
            return Problem("SECRETS", f"{name} is not saved in the Credential Manager")
    host, port = env.get("DB_HOST", "127.0.0.1"), int(env.get("DB_PORT", "5432") or 5432)
    probe = connect or (lambda: socket.create_connection((host, port), timeout=3).close())
    try:
        probe()
    except OSError as exc:
        return Problem(
            "DB_DOWN", f"cannot reach PostgreSQL at {host}:{port} ({type(exc).__name__})"
        )
    return None


def main():
    problem = stage_one()
    print(problem.line() if problem else ok_line())
    return problem.exit_code if problem else OK


if __name__ == "__main__":
    sys.exit(main())
