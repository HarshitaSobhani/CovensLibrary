"""Phase 4a: concurrent issue / return / refund on separate PostgreSQL connections."""

from concurrent.futures import ThreadPoolExecutor
from datetime import date

from django.db import close_old_connections, connection
from django.test import TransactionTestCase

from circulation import services
from circulation.models import Loan
from core.errors import RuleViolation
from finance import corrections as fc
from finance import tokens
from finance.services import Entry
from memberships.tests.helpers import ensure_plans

from .helpers import make_copies, make_owner, member_at

TODAY = date(2026, 12, 1)


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


class ConcurrentLoanTests(TransactionTestCase):
    def setUp(self):
        # guard against test pollution: the outstanding-loan hook must be registered
        self.assertIn(services.member_has_active_loan, fc.OUTSTANDING_LOAN_CHECKS)
        ensure_plans()
        self.owner = make_owner()
        self.members = [member_at(self.owner, f"M{i}", f"98765432{i:02d}") for i in range(6)]
        self.copies = make_copies(self.owner, 6)

    def tearDown(self):
        close_old_connections()

    def issue(self, member, copy):
        return lambda: services.issue_loan(actor=self.owner, member=member, copy=copy, today=TODAY)

    def test_many_members_racing_for_one_copy_get_exactly_one_loan(self):
        results = parallel([self.issue(m, self.copies[0]) for m in self.members])
        self.assertEqual(len([r for r in results if isinstance(r, Loan)]), 1)
        errors = [r for r in results if isinstance(r, Exception)]
        self.assertEqual(
            {e.code for e in errors if isinstance(e, RuleViolation)}
            - {"copy_unavailable", "loan_conflict"},
            set(),
        )
        self.assertEqual([e for e in errors if not isinstance(e, RuleViolation)], [])
        self.assertEqual(Loan.objects.filter(copy=self.copies[0], status="active").count(), 1)

    def test_one_member_racing_for_several_copies_gets_exactly_one_loan(self):
        results = parallel([self.issue(self.members[0], c) for c in self.copies])
        self.assertEqual(len([r for r in results if isinstance(r, Loan)]), 1)
        self.assertEqual(
            {r.code for r in results if isinstance(r, RuleViolation)}, {"loan_outstanding"}
        )
        self.assertEqual(Loan.objects.filter(member=self.members[0], status="active").count(), 1)

    def test_independent_members_and_copies_all_succeed_without_deadlock(self):
        results = parallel(
            [self.issue(m, c) for m, c in zip(self.members, self.copies, strict=True)]
        )
        self.assertEqual([r for r in results if not isinstance(r, Loan)], [])
        self.assertEqual(Loan.objects.filter(status="active").count(), 6)

    def test_simultaneous_returns_close_the_loan_once(self):
        loan = services.issue_loan(
            actor=self.owner, member=self.members[0], copy=self.copies[0], today=TODAY
        )
        results = parallel(
            [lambda: services.return_loan(actor=self.owner, loan=loan, today=date(2026, 12, 5))] * 6
        )
        self.assertEqual(len([r for r in results if isinstance(r, Loan)]), 1)
        self.assertEqual({r.code for r in results if isinstance(r, RuleViolation)}, {"loan_closed"})
        self.assertEqual(self.copies[0].events.filter(event_type="returned").count(), 1)

    def test_an_issue_and_a_token_refund_racing_cannot_both_succeed(self):
        member = self.members[0]

        def refund():
            return fc.refund_token(
                actor=self.owner, member=member, entries=[Entry("cash", 50000)], today=TODAY
            )

        results = parallel([self.issue(member, self.copies[0]), refund, refund])
        issued = [r for r in results if isinstance(r, Loan)]
        refunded = [r for r in results if hasattr(r, "transactions")]
        self.assertEqual(len(issued) + len(refunded), 1)
        if issued:
            self.assertEqual(tokens.token_state(member)[1], tokens.HELD)
        else:
            self.assertEqual(tokens.token_state(member)[1], tokens.REFUNDED)
            self.assertEqual(Loan.objects.count(), 0)
