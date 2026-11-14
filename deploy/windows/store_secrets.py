"""One-time setup helper (run by Setup-CovensLibrary.ps1): save the runtime secrets in the Windows
Credential Manager so no secret is kept in an environment variable or a file.

* DJANGO_SECRET_KEY  generated here (never shown) when it does not exist yet.
* DB_PASSWORD        asked for twice, hidden: the password of the database role ``coven_app``.
* EMAIL_HOST_PASSWORD  optional (press Enter to skip): the SMTP / app password for e-mail.

An existing secret is never replaced unless ``--replace NAME`` is given. Nothing is printed except
which items were saved or kept. Standard library plus ``keyring`` only; uses config/secretstore.py.
"""

import argparse
import getpass
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from config import secretstore  # noqa: E402

PLACEHOLDERS = {"change-me", "changeme", "password", ""}


def _ask_twice(prompt, ask):
    first = ask(f"{prompt} (hidden): ")
    if first.strip().lower() in PLACEHOLDERS:
        raise SystemExit("That is not a real password.")
    if ask(f"{prompt} again: ") != first:
        raise SystemExit("The two entries did not match. Nothing was saved.")
    return first


def run(replace=(), ask=getpass.getpass, store=secretstore, out=print, ask_optional=None):
    ask_optional = ask_optional or ask
    saved, kept = [], []

    def wanted(name):
        try:
            existing = store.get_checked(name)
        except secretstore.SecretStoreError as exc:  # an unreadable store: stop, change nothing
            raise SystemExit(f"STOPPED: {exc}. Nothing was saved.") from exc
        if existing and name not in replace:
            kept.append(name)
            return False
        return True

    if wanted("DJANGO_SECRET_KEY"):
        store.put(
            "DJANGO_SECRET_KEY", secrets.token_urlsafe(64), replace="DJANGO_SECRET_KEY" in replace
        )
        saved.append("DJANGO_SECRET_KEY")
    if wanted("DB_PASSWORD"):
        store.put(
            "DB_PASSWORD",
            _ask_twice("Password of the database role coven_app", ask),
            replace="DB_PASSWORD" in replace,
        )
        saved.append("DB_PASSWORD")
    if wanted("EMAIL_HOST_PASSWORD"):
        value = ask_optional("E-mail (SMTP) password - press Enter to skip (hidden): ")
        if value:
            store.put("EMAIL_HOST_PASSWORD", value, replace="EMAIL_HOST_PASSWORD" in replace)
            saved.append("EMAIL_HOST_PASSWORD")
    out(
        f"Saved: {', '.join(saved) or 'nothing new'}. "
        f"Kept as they were: {', '.join(kept) or 'none'}."
    )
    return saved, kept


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--replace", action="append", default=[], choices=secretstore.NAMES)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Only prove that THIS account's Credential Manager can be written and read back.",
    )
    args = parser.parse_args(argv)
    if args.check:
        try:
            secretstore.probe()
        except secretstore.SecretStoreError as exc:
            raise SystemExit(f"FAILED: {exc}") from exc
        print(f"Credential Manager of {getpass.getuser()}: write, read and delete worked.")
        return
    run(replace=args.replace)


if __name__ == "__main__":
    main()
