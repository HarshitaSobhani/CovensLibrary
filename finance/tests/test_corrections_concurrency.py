"""Concurrent refunds, forfeitures, reversals and adjustments on real connections."""

from concurrent.futures import ThreadPoolExecutor

from django.db import close_old_connections, connection
from django.test import TransactionTestCase

from core.errors import RuleViolation
from finance import corrections as fc
from finance import tokens
from finance.models import FinancialTransaction, TokenLedgerEntry
from finance.services import Entry
from memberships.tests.helpers import TODAY, ensure_plans, make_owner, register


def parallel(fn, n):
    def wrapped(i):
        try:
            return fn(i)
        except Exception as exc:
            return exc
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=n) as pool:
        return list(pool.map(wrapped, range(n)))


class ConcurrentCorrectionTests(TransactionTestCase):
    def setUp(self):
        ensure_plans()
        self.owner = make_owner()
        self.member = register(self.owner).member

    def tearDown(self):
        close_old_connections()

    def refund(self, i):
        return fc.refund_token(
            actor=self.owner, member=self.member, entries=[Entry("cash", 50000)], today=TODAY
        )

    def forfeit(self, i):
        return fc.forfeit_token(
            actor=self.owner,
            member=self.member,
            trigger="lost",
            owner_confirmed=True,
            reason="r",
            today=TODAY,
        )

    def test_simultaneous_refunds_pay_out_exactly_once(self):
        results = parallel(self.refund, 8)
        self.assertEqual(len([r for r in results if not isinstance(r, Exception)]), 1)
        self.assertEqual(
            {r.code for r in results if isinstance(r, RuleViolation)}, {"token_already_refunded"}
        )
        self.assertEqual(FinancialTransaction.objects.filter(kind="token_refund").count(), 1)
        self.assertEqual(tokens.balance_paise(tokens.latest_cycle(self.member)), 0)

    def test_a_refund_and_a_forfeiture_racing_cannot_both_win(self):
        def work(i):
            return self.refund(i) if i % 2 == 0 else self.forfeit(i)

        results = parallel(work, 8)
        self.assertEqual(len([r for r in results if not isinstance(r, Exception)]), 1)
        self.assertEqual(
            [r for r in results if isinstance(r, Exception) and not isinstance(r, RuleViolation)],
            [],
        )
        cycle = tokens.latest_cycle(self.member)
        types = sorted(cycle.entries.values_list("entry_type", flat=True))
        self.assertIn(types, (["collection", "forfeiture"], ["collection", "refund"]))
        self.assertEqual(tokens.balance_paise(cycle), 0)

    def test_simultaneous_forfeitures_post_once(self):
        results = parallel(self.forfeit, 8)
        self.assertEqual(len([r for r in results if not isinstance(r, Exception)]), 1)
        self.assertEqual(TokenLedgerEntry.objects.filter(entry_type="forfeiture").count(), 1)

    def test_simultaneous_reversals_of_one_record_post_once(self):
        original = self.forfeit(0).transactions[0]

        def work(i):
            return fc.reverse_transaction(
                actor=self.owner, transaction_id=original.pk, reason="mistake", today=TODAY
            )

        results = parallel(work, 6)
        self.assertEqual(len([r for r in results if not isinstance(r, Exception)]), 1)
        self.assertEqual(FinancialTransaction.objects.filter(reverses=original).count(), 1)
        self.assertEqual(tokens.token_state(self.member)[1], tokens.HELD)

    def test_parallel_adjustments_get_unique_gap_free_numbers(self):
        def work(i):
            return (
                fc.post_adjustment(
                    actor=self.owner,
                    member=self.member,
                    direction="in",
                    amount_paise=100 + i,
                    reason="r",
                    owner_approved=True,
                    entries=[Entry("cash", 100 + i)],
                    today=TODAY,
                )
                .transactions[0]
                .receipt_no
            )

        results = parallel(work, 10)
        self.assertEqual([r for r in results if isinstance(r, Exception)], [])
        self.assertEqual(sorted(results), [f"ADJ-2026-{n:06d}" for n in range(1, 11)])
