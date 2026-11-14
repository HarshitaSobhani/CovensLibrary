"""Where the secrets live. The working backup key (and the Drive tokens) are held in the Windows
Credential Manager through ``keyring``; never in the repository, the database, the backups, logs or
ordinary configuration files. Tests use an in-memory store.

The recovery key is a printable copy of the working key with a checksum (decision D-29d): the owner
writes it down once and keeps it away from the backups."""

import base64
import hashlib
import secrets
import string

from django.conf import settings

SERVICE = "Coven's Library"
WORKING_KEY = "backup-working-key"  # the key NEW backups are made with
PENDING_KEY = "backup-pending-key"  # a new key during a rotation, before it is committed
RETIRED_PREFIX = "backup-retired-"  # + fingerprint: an old key kept to open the backups it made
RETIRED_INDEX = "backup-retired-index"  # the (non-secret) list of retired fingerprints
DRIVE_CLIENT = "google-oauth-client"
DRIVE_TOKEN = "google-drive-token"


class KeyStoreError(Exception):
    pass


class RecoveryKeyError(ValueError):
    pass


class MemoryKeyStore:
    """For tests and rehearsals only (nothing is persisted)."""

    def __init__(self):
        self.items = {}

    def get(self, name):
        return self.items.get(name)

    def set(self, name, value):
        self.items[name] = value

    def delete(self, name):
        self.items.pop(name, None)


class KeyringKeyStore:
    """Windows Credential Manager on the Dell (the macOS Keychain on the development machine)."""

    def get(self, name):
        import keyring

        try:
            return keyring.get_password(SERVICE, name)
        except Exception as exc:  # noqa: BLE001 - backend problems are one clear error
            raise KeyStoreError(
                f"the credential store could not be read ({type(exc).__name__})"
            ) from exc

    def set(self, name, value):
        import keyring

        try:
            keyring.set_password(SERVICE, name, value)
        except Exception as exc:  # noqa: BLE001
            raise KeyStoreError(
                f"the credential store could not be written ({type(exc).__name__})"
            ) from exc

    def delete(self, name):
        import keyring

        try:
            keyring.delete_password(SERVICE, name)
        except Exception:  # noqa: BLE001 - deleting something that is not there is fine
            pass


_memory = MemoryKeyStore()


def get_keystore():
    if getattr(settings, "BACKUP_KEYSTORE", "keyring") == "memory":
        return _memory
    return KeyringKeyStore()


def reset_memory_keystore():
    _memory.items.clear()


# ---------------------------------------------------------------- the working key
def generate_key():
    return secrets.token_bytes(32)


def encode_key(key):
    return base64.urlsafe_b64encode(key).decode("ascii")


def decode_key(text):
    try:
        key = base64.urlsafe_b64decode(text.encode("ascii"))
    except Exception as exc:  # noqa: BLE001
        raise KeyStoreError("the stored backup key is damaged") from exc
    if len(key) != 32:
        raise KeyStoreError("the stored backup key has the wrong length")
    return key


def load_working_key(store=None):
    """The working key, or None when none has been set up. A damaged value is an error, never
    'no key': nothing may silently replace a key it cannot read."""
    stored = (store or get_keystore()).get(WORKING_KEY)
    return decode_key(stored) if stored else None


def key_fingerprint(key):
    from .crypto import fingerprint

    return fingerprint(key)


def save_working_key(key, store=None, *, replace=False):
    """Write the working key. An existing DIFFERENT key is never overwritten unless the caller says
    ``replace=True`` (only the confirmed rotation / set-current commands do). The value is read
    back before returning; a store that cannot hold it is an error, never a silent success."""
    store = store or get_keystore()
    existing = store.get(WORKING_KEY)
    if existing and decode_key(existing) != key and not replace:
        raise KeyStoreError(
            "a different backup key is already stored; it is never replaced silently"
        )
    store.set(WORKING_KEY, encode_key(key))
    if load_working_key(store) != key:
        raise KeyStoreError("the backup key was saved but could not be read back")


def retired_name(fingerprint):
    return f"{RETIRED_PREFIX}{fingerprint}"


def _load_slot(store, name):
    stored = store.get(name)
    return decode_key(stored) if stored else None


def load_pending_key(store=None):
    return _load_slot(store or get_keystore(), PENDING_KEY)


def load_retired_key(fingerprint, store=None):
    return _load_slot(store or get_keystore(), retired_name(fingerprint))


def save_slot(name, key, store, *, replace=False):
    """Write a key slot (read back and verified). Never overwrites a different key silently."""
    existing = store.get(name)
    if existing and decode_key(existing) != key and not replace:
        raise KeyStoreError(f"{name} already holds a different key; it is never replaced silently")
    store.set(name, encode_key(key))
    if _load_slot(store, name) != key:
        raise KeyStoreError(f"{name} was saved but could not be read back")


def retired_fingerprints(store=None):
    """Fingerprints of the retired keys that are really in the store (the index is only a list;
    the slot is the truth)."""
    import json

    store = store or get_keystore()
    try:
        listed = json.loads(store.get(RETIRED_INDEX) or "[]")
    except ValueError:
        listed = []
    return [f for f in listed if store.get(retired_name(f))]


def _write_index(store, fingerprints):
    import json

    store.set(RETIRED_INDEX, json.dumps(sorted(set(fingerprints))))


def save_retired(key, store=None):
    """Keep a key as a retired key (verified; never overwrites a different key) and list it."""
    store = store or get_keystore()
    fingerprint = key_fingerprint(key)
    save_slot(retired_name(fingerprint), key, store)
    _write_index(store, [*retired_fingerprints(store), fingerprint])


def delete_retired(fingerprint, store=None):
    store = store or get_keystore()
    delete_slot(retired_name(fingerprint), store)
    _write_index(store, [f for f in retired_fingerprints(store) if f != fingerprint])


def delete_slot(name, store):
    """Delete and VERIFY it is gone (the Windows store swallows nothing here)."""
    store.delete(name)
    if store.get(name):
        raise KeyStoreError(f"{name} could not be removed from the credential store")


def find_key(fingerprint, store=None):
    """The key with this fingerprint from the working, pending or retired slot, else None."""
    store = store or get_keystore()
    for loader in (
        lambda: load_working_key(store),
        lambda: load_pending_key(store),
        lambda: load_retired_key(fingerprint, store),
    ):
        key = loader()
        if key is not None and key_fingerprint(key) == fingerprint:
            return key
    return None


# ---------------------------------------------------------------- the recovery key
_ALPHABET = set(string.ascii_uppercase + "234567")


def _checksum(key):
    return hashlib.sha256(b"covens-library-recovery-v1" + key).digest()[:3]


def recovery_key_text(key):
    """The working key as printable groups of five characters, with a checksum."""
    raw = base64.b32encode(key + _checksum(key)).decode("ascii").rstrip("=")
    return "-".join(raw[i : i + 5] for i in range(0, len(raw), 5))


def parse_recovery_key(text):
    """The 32-byte key from what the owner typed; spaces, dashes and case do not matter."""
    cleaned = "".join(ch for ch in text.upper() if ch not in " -\t\r\n")
    if not cleaned or set(cleaned) - _ALPHABET:
        raise RecoveryKeyError("The recovery key contains characters that cannot belong to it.")
    padded = cleaned + "=" * (-len(cleaned) % 8)
    try:
        raw = base64.b32decode(padded)
    except Exception as exc:  # noqa: BLE001
        raise RecoveryKeyError("The recovery key is not valid.") from exc
    if len(raw) != 35:
        raise RecoveryKeyError("The recovery key has the wrong length.")
    key, check = raw[:32], raw[32:]
    if not secrets.compare_digest(check, _checksum(key)):
        raise RecoveryKeyError(
            "The recovery key has a typing mistake (the checksum does not match)."
        )
    return key
