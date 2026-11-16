"""Failure injection for ``rotation.set_current``: a store failure at every write boundary must
never lose a key, and a retry must complete the swap.

set_current writes, in order: (1) keep the working key as retired, (2) make the target the working
key, (3) remove the target's retired copy. Each write is a store ``set`` or ``delete`` (the retired
index is also a ``set``), so the store can fail between any two of them.
"""

from django.test import TestCase

from backups import keystore, rotation
from backups.tests.helpers import fresh_key


class FaultyStore:
    """Delegates to a memory store and raises on the Nth write (set or delete)."""

    def __init__(self, inner, fail_at=None):
        self.inner, self.fail_at, self.writes = inner, fail_at, 0

    def get(self, name):
        return self.inner.get(name)

    def set(self, name, value):
        self._tick(f"set {name}")
        self.inner.set(name, value)

    def delete(self, name):
        self._tick(f"delete {name}")
        self.inner.delete(name)

    def _tick(self, label):
        self.writes += 1
        if self.writes == self.fail_at:
            raise OSError(f"injected failure at write {self.writes} ({label})")


class SetCurrentFailureInjectionTests(TestCase):
    def setUp(self):
        self.inner = keystore.MemoryKeyStore()
        self.old = fresh_key()
        self.target = fresh_key()
        keystore.save_working_key(self.old, self.inner)
        keystore.save_retired(self.target, self.inner)
        self.start = dict(self.inner.items)
        self.target_fp = rotation.fp(self.target)
        self.old_fp = rotation.fp(self.old)

    def run_set_current(self, store):
        return rotation.set_current(
            self.target_fp, confirm=rotation.CONFIRM_SET_CURRENT, store=store
        )

    def assert_nothing_lost(self, label):
        self.assertIsNotNone(keystore.load_working_key(self.inner), label)
        self.assertIsNotNone(keystore.find_key(self.old_fp, self.inner), f"old key lost: {label}")
        self.assertIsNotNone(
            keystore.find_key(self.target_fp, self.inner), f"target key lost: {label}"
        )

    def test_a_failure_at_every_write_loses_no_key_and_a_retry_completes(self):
        healthy = FaultyStore(self.inner)
        self.run_set_current(healthy)
        total = healthy.writes
        self.assertGreater(total, 2)
        self.assertEqual(keystore.load_working_key(self.inner), self.target)
        failures = []
        for fail_at in range(1, total + 1):
            self.inner.items = dict(self.start)
            try:
                self.run_set_current(FaultyStore(self.inner, fail_at=fail_at))
            except (OSError, keystore.KeyStoreError):
                pass
            else:
                failures.append(f"write {fail_at}: no failure was raised")
                continue
            try:
                self.assert_nothing_lost(f"write {fail_at}")
                self.run_set_current(self.inner_store())  # the retry, healthy store
            except AssertionError as exc:
                failures.append(f"write {fail_at}: {exc}")
                continue
            except rotation.RotationError as exc:
                failures.append(f"write {fail_at}: retry refused: {exc}")
                continue
            if keystore.load_working_key(self.inner) != self.target:
                failures.append(f"write {fail_at}: working key not the target after retry")
        self.assertEqual(failures, [])

    def inner_store(self):
        return FaultyStore(self.inner)

    def test_recovers_from_duplicate_slots_left_by_an_interrupted_swap(self):
        # State after an interruption between writes 3 and 4: the target is the working key AND
        # still a retired copy; the old key is retained. Must finish cleanly, not refuse.
        keystore.save_working_key(self.target, self.inner, replace=True)
        keystore.save_retired(self.old, self.inner)
        self.assertIsNotNone(keystore.load_retired_key(self.target_fp, self.inner))
        result = self.run_set_current(self.inner_store())
        self.assertEqual(result["working"], self.target_fp)
        self.assertEqual(keystore.load_working_key(self.inner), self.target)
        self.assertIsNone(keystore.load_retired_key(self.target_fp, self.inner))
        self.assertEqual(keystore.load_retired_key(self.old_fp, self.inner), self.old)
        self.assert_nothing_lost("duplicate slots")
