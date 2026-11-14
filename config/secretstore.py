"""Runtime secrets in the Windows Credential Manager (through ``keyring``).

In production this is the ONLY source: there is no fallback to an environment variable or a file,
and a store that cannot be read stops the application (fail closed).

Why: a user environment variable on Windows lives in the registry hive (NTUSER.DAT) as plain text
and is visible to every process of that account; Credential Manager entries are encrypted with the
signed-in user's DPAPI key. It is NOT a defence against code running as the same signed-in user
(that code can read both), only against reading the disk offline and against casual exposure.

This module imports nothing from Django (settings load it before Django exists).
* ``get_checked`` returns the value, ``None`` when the secret is genuinely not stored, and RAISES
  ``SecretStoreError`` when the store itself cannot be read: "missing" and "broken" are different
  problems and neither is ever answered with a made-up value.
* ``put`` never replaces an existing secret unless the caller passes ``replace=True``.
"""

SERVICE = "Coven's Library"
PREFIX = "runtime-"
NAMES = ("DJANGO_SECRET_KEY", "DB_PASSWORD", "EMAIL_HOST_PASSWORD")


class SecretStoreError(Exception):
    """The credential store could not be read or written (never contains a secret)."""


def item_name(name):
    return PREFIX + name


def get_checked(name):
    if name not in NAMES:
        raise SecretStoreError(f"{name} is not a runtime secret")
    try:
        import keyring

        value = keyring.get_password(SERVICE, item_name(name))
    except Exception as exc:  # noqa: BLE001 - no backend, locked store, wrong platform
        raise SecretStoreError(
            f"the Windows Credential Manager could not be read ({type(exc).__name__})"
        ) from exc
    return value or None


PROBE_ITEM = "setup-probe"


def probe():
    """Write, read back and delete a throw-away item in the credential store of THE ACCOUNT RUNNING
    THIS: proof that setup can really save secrets there. Never touches a real secret. Raises
    ``SecretStoreError`` on any problem; there is no fallback."""
    import secrets as _secrets

    value = _secrets.token_hex(8)
    try:
        import keyring

        keyring.set_password(SERVICE, PROBE_ITEM, value)
        stored = keyring.get_password(SERVICE, PROBE_ITEM)
        keyring.delete_password(SERVICE, PROBE_ITEM)
        remaining = keyring.get_password(SERVICE, PROBE_ITEM)
    except Exception as exc:  # noqa: BLE001
        raise SecretStoreError(
            f"the Windows Credential Manager could not be used ({type(exc).__name__})"
        ) from exc
    if stored != value or remaining:
        raise SecretStoreError("the Windows Credential Manager did not return what was written")


def get(name):
    """Like ``get_checked`` but answers None on any problem. For callers that only display state."""
    try:
        return get_checked(name)
    except SecretStoreError:
        return None


def put(name, value, replace=False):
    if name not in NAMES:
        raise ValueError(f"{name} is not a runtime secret")
    if not value:
        raise SecretStoreError("an empty secret is never saved")
    existing = get_checked(name)
    if existing and not replace:
        raise SecretStoreError(f"{name} is already saved; it is never replaced silently")
    try:
        import keyring

        keyring.set_password(SERVICE, item_name(name), value)
    except Exception as exc:  # noqa: BLE001
        raise SecretStoreError(
            f"the Windows Credential Manager could not be written ({type(exc).__name__})"
        ) from exc
    if get_checked(name) != value:
        raise SecretStoreError(f"{name} was saved but could not be read back")
