import threading

from django.db import IntegrityError, connection, transaction
from django.test import TestCase, TransactionTestCase

from core.models import IdSequence, LibrarySetting
from core.sequences import next_value


class SequenceTests(TestCase):
    def test_increments_per_name_and_year(self):
        self.assertEqual([next_value("member") for _ in range(3)], [1, 2, 3])
        self.assertEqual(next_value("receipt_sub", 2026), 1)
        self.assertEqual(next_value("receipt_sub", 2027), 1)
        self.assertEqual(next_value("receipt_sub", 2026), 2)

    def test_unique_constraint(self):
        IdSequence.objects.create(name="x", year=0)
        with self.assertRaises(IntegrityError), transaction.atomic():
            IdSequence.objects.create(name="x", year=0)

    def test_rollback_does_not_burn_numbers(self):
        class Boom(Exception):
            pass

        try:
            with transaction.atomic():
                next_value("member")
                raise Boom
        except Boom:
            pass
        self.assertEqual(next_value("member"), 1)


class SequenceConcurrencyTests(TransactionTestCase):
    def test_concurrent_callers_get_unique_numbers(self):
        results, errors = [], []

        def worker():
            try:
                for _ in range(10):
                    results.append(next_value("concurrent"))
            except Exception as exc:  # pragma: no cover
                errors.append(exc)
            finally:
                connection.close()

        threads = [threading.Thread(target=worker) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(sorted(results), list(range(1, 61)))


class LibrarySettingTests(TestCase):
    def test_singleton_defaults(self):
        s = LibrarySetting.load()
        self.assertEqual((s.pk, s.library_name), (1, "Coven's Library"))
        self.assertEqual((s.loan_days, s.grace_days, s.token_amount_paise), (30, 7, 50000))
        self.assertEqual(s.issue_block_days, 15)

    def test_only_one_row(self):
        LibrarySetting.load()
        second = LibrarySetting(library_name="Other")
        second.save()
        self.assertEqual(LibrarySetting.objects.count(), 1)
        self.assertEqual(LibrarySetting.objects.get().library_name, "Other")
