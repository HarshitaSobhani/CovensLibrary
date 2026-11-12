"""Concurrent registrations and renewals on separate PostgreSQL connections."""

import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date

from django.db import close_old_connections, connection
from django.test import TransactionTestCase

from core.errors import RuleViolation
from finance import tokens
from finance.models import FinancialTransaction
from finance.services import Entry
from memberships import services as ms
from memberships.models import MemberSubscription

from .helpers import TODAY, ensure_plans, make_owner, register, renew


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


class ConcurrencyTests(TransactionTestCase):
    def setUp(self):
        self.owner = make_owner()
        ensure_plans()

    def tearDown(self):
        close_old_connections()

    def test_parallel_registrations_get_distinct_member_ids_and_receipts(self):
        results = parallel(lambda i: register(self.owner, name=f"M{i}", number=f"98765{i:05d}"), 10)
        self.assertEqual([r for r in results if isinstance(r, Exception)], [])
        self.assertEqual(
            sorted(r.member.membership_no for r in results),
            [f"COV-M-{n:06d}" for n in range(1, 11)],
        )
        subs = sorted(t.receipt_no for r in results for t in r.transactions)
        self.assertEqual(subs[:10], [f"SUB-2026-{n:06d}" for n in range(1, 11)])
        self.assertEqual(subs[10:], [f"TOK-2026-{n:06d}" for n in range(1, 11)])

    def test_two_simultaneous_renewals_of_one_member_create_exactly_one(self):
        member = register(self.owner).member
        results = parallel(lambda i: renew(self.owner, member, today=date(2027, 1, 1)), 6)
        ok = [r for r in results if not isinstance(r, Exception)]
        rejected = [r for r in results if isinstance(r, RuleViolation)]
        self.assertEqual(len(ok), 1)
        self.assertEqual(len(rejected), 5)
        self.assertTrue(all(r.code == "scheduled_exists" for r in rejected))
        self.assertEqual(MemberSubscription.objects.filter(member=member).count(), 2)
        self.assertEqual(
            FinancialTransaction.objects.filter(kind="subscription_payment").count(), 2
        )

    def test_identical_double_submit_with_one_request_key_posts_once(self):
        member = register(self.owner).member
        key = uuid.uuid4()
        results = parallel(
            lambda i: renew(self.owner, member, today=date(2027, 1, 1), request_key=key), 6
        )
        self.assertEqual(len([r for r in results if not isinstance(r, Exception)]), 1)
        self.assertEqual(MemberSubscription.objects.filter(member=member).count(), 2)

    def test_two_simultaneous_fresh_token_payments_create_one_cycle(self):
        member = register(self.owner).member
        from .helpers import forfeit

        forfeit(self.owner, member)
        results = parallel(
            lambda i: ms.pay_fresh_token(
                actor=self.owner, member=member, entries=[Entry("cash", 50000)], today=TODAY
            ),
            6,
        )
        self.assertEqual(len([r for r in results if not isinstance(r, Exception)]), 1)
        codes = {r.code for r in results if isinstance(r, RuleViolation)}
        self.assertEqual(codes, {"token_already_held"})
        self.assertEqual(member.token_account.cycles.count(), 2)
        self.assertEqual(tokens.token_state(member)[1], tokens.HELD)

    def test_the_token_balance_trigger_holds_under_real_commits(self):
        member = register(self.owner).member
        self.assertEqual(tokens.balance_paise(tokens.latest_cycle(member)), 50000)
