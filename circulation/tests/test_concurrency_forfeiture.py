"""Phase 4b: concurrent job runs, returns, renewals and owner decisions (separate connections)."""

from concurrent.futures import ThreadPoolExecutor

from django.db import close_old_connections, connection
from django.test import TransactionTestCase

from circulation import forfeiture, grace, services
from circulation.models import Loan, LoanRenewal, PendingForfeiture
from core.errors import RuleViolation
from finance import tokens
from finance.models import FinancialTransaction
from memberships.tests.helpers import ensure_plans

from .helpers import D, make_owner, overdue_loan

DAY8 = D(2027, 1, 8)


def parallel(fns):
    def wrapped(fn):
        try:
            return fn()
        except Exception as exc:
            return exc
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=len(fns)) as pool:
        return list(pool.map(wrapped, fns))


class Base(TransactionTestCase):
    def setUp(self):
        ensure_plans()
        self.owner = make_owner()
        self.member, self.copy, self.loan = overdue_loan(self.owner)

    def tearDown(self):
        close_old_connections()

    def forfeitures(self):
        return FinancialTransaction.objects.filter(kind="token_forfeiture").count()


class JobConcurrencyTests(Base):
    def test_simultaneous_job_runs_forfeit_exactly_once(self):
        results = parallel([lambda: forfeiture.process_overdue_forfeitures(today=DAY8)] * 6)
        self.assertEqual([r for r in results if isinstance(r, Exception)], [])
        self.assertEqual(sum(r["forfeited"] for r in results), 1)
        self.assertEqual(self.forfeitures(), 1)
        self.assertEqual(tokens.token_state(self.member)[1], tokens.FORFEITED)

    def test_a_day_8_return_racing_the_job_forfeits_exactly_once(self):
        def ret():
            return services.return_loan(actor=self.owner, loan=self.loan, today=DAY8)

        results = parallel([ret, lambda: forfeiture.process_overdue_forfeitures(today=DAY8), ret])
        unexpected = [
            r for r in results if isinstance(r, Exception) and not isinstance(r, RuleViolation)
        ]
        self.assertEqual(unexpected, [])
        self.assertEqual(self.forfeitures(), 1)
        loan = Loan.objects.get(pk=self.loan.pk)
        self.assertEqual(loan.forfeiture_entry.effective_date, DAY8)
        self.assertIn(loan.status, ("returned", "active"))

    def test_a_renewal_racing_a_return_leaves_one_consistent_outcome(self):
        today = D(2026, 12, 20)
        results = parallel(
            [
                lambda: services.renew_loan(actor=self.owner, loan=self.loan, today=today),
                lambda: services.return_loan(actor=self.owner, loan=self.loan, today=today),
            ]
        )
        self.assertEqual(
            [r for r in results if isinstance(r, Exception) and not isinstance(r, RuleViolation)],
            [],
        )
        loan = Loan.objects.get(pk=self.loan.pk)
        renewed = LoanRenewal.objects.filter(loan=loan).count()
        if loan.status == "returned":
            self.assertIn(renewed, (0, 1))  # renewed then returned, or returned first
        else:
            self.assertEqual((loan.status, renewed), ("active", 1))

    def test_a_grace_change_racing_the_job_never_posts_a_forfeiture_without_the_owner(self):
        today = D(2027, 1, 5)  # eligible at grace 3, not at grace 7
        results = parallel(
            [
                lambda: grace.change_grace_days(actor=self.owner, new_days=3, today=today),
                lambda: forfeiture.process_overdue_forfeitures(today=today),
            ]
        )
        self.assertEqual([r for r in results if isinstance(r, Exception)], [])
        self.assertEqual(self.forfeitures(), 0)
        self.assertEqual(
            PendingForfeiture.objects.filter(loan=self.loan, status="pending").count(), 1
        )


class DecisionConcurrencyTests(Base):
    def setUp(self):
        super().setUp()
        grace.change_grace_days(actor=self.owner, new_days=3, today=D(2027, 1, 5))
        self.pending = PendingForfeiture.objects.get(loan=self.loan)

    def test_confirm_and_decline_racing_resolve_exactly_once(self):
        results = parallel(
            [
                lambda: grace.confirm_pending(
                    actor=self.owner, pending=self.pending, reason="r", today=D(2027, 1, 5)
                ),
                lambda: grace.decline_pending(actor=self.owner, pending=self.pending, reason="r"),
                lambda: grace.confirm_pending(
                    actor=self.owner, pending=self.pending, reason="r", today=D(2027, 1, 5)
                ),
            ]
        )
        won = [r for r in results if isinstance(r, PendingForfeiture)]
        self.assertEqual(len(won), 1)
        self.assertEqual(
            {r.code for r in results if isinstance(r, RuleViolation)}, {"already_resolved"}
        )
        status = PendingForfeiture.objects.get(pk=self.pending.pk).status
        self.assertEqual(self.forfeitures(), 1 if status == "confirmed" else 0)
