import keyring
from django.test import SimpleTestCase, override_settings
from keyring.backend import KeyringBackend

from backups import keystore
from backups.keystore import (
    KeyringKeyStore,
    KeyStoreError,
    MemoryKeyStore,
    RecoveryKeyError,
    parse_recovery_key,
    recovery_key_text,
)

KEY = bytes(range(32))


class MemoryBackend(KeyringBackend):
    priority = 1
    broken = False

    def __init__(self):
        self.data = {}

    def get_password(self, service, username):
        if self.broken:
            raise RuntimeError("backend down")
        return self.data.get((service, username))

    def set_password(self, service, username, password):
        if self.broken:
            raise RuntimeError("backend down")
        self.data[(service, username)] = password

    def delete_password(self, service, username):
        if self.broken:
            raise RuntimeError("backend down")
        del self.data[(service, username)]


class RecoveryKeyTests(SimpleTestCase):
    def test_round_trip_and_format(self):
        text = recovery_key_text(KEY)
        groups = text.split("-")
        self.assertTrue(all(len(g) == 5 for g in groups[:-1]))
        self.assertRegex(text, r"^[A-Z2-7-]+$")
        self.assertEqual(parse_recovery_key(text), KEY)

    def test_the_owner_can_type_it_sloppily(self):
        text = recovery_key_text(KEY)
        for variant in (
            text.lower(),
            text.replace("-", " "),
            text.replace("-", ""),
            "  " + text + "\n",
            text.replace("-", "\n"),
        ):
            self.assertEqual(parse_recovery_key(variant), KEY)

    def test_a_typing_mistake_is_caught_by_the_checksum(self):
        text = recovery_key_text(KEY)
        index = next(i for i, ch in enumerate(text) if ch != "-")
        wrong = "A" if text[index] != "A" else "B"
        with self.assertRaisesRegex(RecoveryKeyError, "typing mistake|wrong length|not valid"):
            parse_recovery_key(text[:index] + wrong + text[index + 1 :])

    def test_bad_input_is_refused(self):
        for bad in (
            "",
            "   ",
            "ABC",
            "1111-1111",
            recovery_key_text(KEY)[:-5],
            recovery_key_text(KEY) + "AAAAA",
            "!!!!",
        ):
            with self.assertRaises(RecoveryKeyError):
                parse_recovery_key(bad)

    def test_every_key_has_its_own_text_and_the_text_is_not_the_raw_key(self):
        other = bytes(range(1, 33))
        self.assertNotEqual(recovery_key_text(KEY), recovery_key_text(other))
        self.assertNotIn(KEY.hex(), recovery_key_text(KEY).lower())


class StoreTests(SimpleTestCase):
    def test_memory_store(self):
        store = MemoryKeyStore()
        self.assertIsNone(store.get("x"))
        store.set("x", "1")
        self.assertEqual(store.get("x"), "1")
        store.delete("x")
        store.delete("x")  # deleting twice is fine
        self.assertIsNone(store.get("x"))

    @override_settings(BACKUP_KEYSTORE="memory")
    def test_the_working_key_round_trips_through_the_store(self):
        keystore.reset_memory_keystore()
        self.assertIsNone(keystore.load_working_key())
        key = keystore.generate_key()
        self.assertEqual(len(key), 32)
        keystore.save_working_key(key)
        self.assertEqual(keystore.load_working_key(), key)
        self.assertNotEqual(keystore.generate_key(), keystore.generate_key())
        keystore.reset_memory_keystore()

    def test_a_damaged_stored_key_is_a_clear_error(self):
        store = MemoryKeyStore()
        for bad in ("not-base64!!", "AAAA"):
            store.set(keystore.WORKING_KEY, bad)
            with self.assertRaises(KeyStoreError):
                keystore.load_working_key(store)

    def test_the_credential_store_adapter_uses_keyring_and_wraps_failures(self):
        backend = MemoryBackend()
        previous = keyring.get_keyring()
        keyring.set_keyring(backend)
        try:
            store = KeyringKeyStore()
            store.set("k", "v")
            self.assertEqual(backend.data[(keystore.SERVICE, "k")], "v")
            self.assertEqual(store.get("k"), "v")
            store.delete("k")
            store.delete("k")
            self.assertIsNone(store.get("k"))
            backend.broken = True
            for call in (lambda: store.get("k"), lambda: store.set("k", "v")):
                with self.assertRaises(KeyStoreError) as ctx:
                    call()
                self.assertNotIn("backend down", str(ctx.exception))  # no backend detail leaks
        finally:
            keyring.set_keyring(previous)

    @override_settings(BACKUP_KEYSTORE="keyring")
    def test_the_default_store_is_the_credential_manager(self):
        self.assertIsInstance(keystore.get_keystore(), KeyringKeyStore)

    def test_the_service_name_and_item_names_are_fixed(self):
        self.assertEqual(keystore.SERVICE, "Coven's Library")
        self.assertEqual(keystore.WORKING_KEY, "backup-working-key")
