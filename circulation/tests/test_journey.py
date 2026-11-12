"""One end-to-end PostgreSQL regression test of the Phase 4 borrowing journey.

issue -> renew -> overdue and automatic day-8 forfeiture (system actor) -> late return ->
borrowing blocked -> fresh token -> second loan returned DAMAGED (owner decision) -> fresh token ->
third loan overdue, grace shortened, owner DECLINES -> returned -> fourth loan marked LOST (owner
decision). Final checks: ledger, attribution, copy states, history, audit trail, events.
"""

from django.db import DatabaseError, connection, transaction
from django.test import TestCase

from audit.models import AuditEvent
from catalogue.models import BookCopy
from circulation import forfeiture, grace, services
from circulation.models import Loan, PendingForfeiture
from core.errors import RuleViolation
from core.models import DomainEvent
from finance import tokens
from finance.models import FinancialTransaction, TokenLedgerEntry
from finance.services import Entry
from memberships import services as ms

from .helpers import D, make_copies, make_owner, member_at


class BorrowingJourneyTests(TestCase):
    def test_the_complete_borrowing_journey(self):
        owner = make_owner()
        member = member_at(owner)  # subscription 11 Nov 2026 - 10 May 2027
        books = make_copies(owner, 4)

        def blockers(day):
            return [c for c, _ in ms.borrowing_blockers(member, day)]

        def token():
            return tokens.token_state(member)[1]

        if True:
            # 1. issue, 30 days; the second book is refused while one is out
            loan1 = services.issue_loan(
                actor=owner, member=member, copy=books[0], today=D(2026, 12, 1)
            )
            self.assertEqual(loan1.due_on, D(2026, 12, 31))
            with self.assertRaises(RuleViolation) as ctx:
                services.issue_loan(actor=owner, member=member, copy=books[1], today=D(2026, 12, 2))
            self.assertEqual(ctx.exception.code, "loan_outstanding")
            # 2. renew once (not after the due date)
            renewal = services.renew_loan(actor=owner, loan=loan1, today=D(2026, 12, 31))
            self.assertEqual(renewal.new_due_on, D(2027, 1, 30))
            # 3. overdue (due 30 Jan, grace to 6 Feb): nothing until day 8 (7 Feb), then the system
            self.assertEqual(
                forfeiture.process_overdue_forfeitures(today=D(2027, 2, 6))["forfeited"], 0
            )
            self.assertEqual(
                forfeiture.process_overdue_forfeitures(today=D(2027, 2, 7))["forfeited"], 1
            )
            auto = FinancialTransaction.objects.get(kind="token_forfeiture")
            self.assertEqual(
                (auto.recorded_by_id, auto.recorded_by_system, auto.effective_date),
                (None, "overdue_forfeiture_job", D(2027, 2, 7)),
            )
            self.assertEqual(token(), tokens.FORFEITED)
            self.assertIsNotNone(ms.current_subscription(member, D(2027, 2, 7)))
            # 4. a late good return does not forfeit twice; borrowing stays suspended
            services.return_loan(actor=owner, loan=loan1, today=D(2027, 2, 10))
            self.assertEqual(
                FinancialTransaction.objects.filter(kind="token_forfeiture").count(), 1
            )
            self.assertEqual(blockers(D(2027, 2, 10)), ["token_forfeited"])
            # 5. fresh token (new cycle), second loan returned DAMAGED by the owner's decision
            ms.pay_fresh_token(
                actor=owner, member=member, entries=[Entry("cash", 50000)], today=D(2027, 2, 11)
            )
            loan2 = services.issue_loan(
                actor=owner, member=member, copy=books[1], today=D(2027, 2, 12)
            )
            services.return_loan(
                actor=owner,
                loan=loan2,
                condition="damaged",
                owner_confirmed=True,
                reason="water damage",
                today=D(2027, 2, 20),
            )
            self.assertEqual(token(), tokens.FORFEITED)
            owner_txn = FinancialTransaction.objects.filter(kind="token_forfeiture").order_by("id")[
                1
            ]
            self.assertEqual(
                (owner_txn.recorded_by_id, owner_txn.recorded_by_system), (owner.pk, "")
            )
            self.assertEqual(BookCopy.objects.get(pk=books[1].pk).status, "damaged")
            # 6. fresh token, third loan overdue by 5 days, grace shortened: the owner DECLINES
            ms.pay_fresh_token(
                actor=owner, member=member, entries=[Entry("upi", 50000)], today=D(2027, 3, 1)
            )
            loan3 = services.issue_loan(
                actor=owner, member=member, copy=books[2], today=D(2027, 3, 1)
            )
            event = grace.change_grace_days(actor=owner, new_days=3, today=D(2027, 4, 5))
            self.assertEqual([r["loan_id"] for r in event.affected_loans], [loan3.pk])
            self.assertEqual(token(), tokens.HELD)  # nothing posted without the owner
            self.assertEqual(
                forfeiture.process_overdue_forfeitures(today=D(2027, 4, 5))["skipped_decision"], 1
            )
            grace.decline_pending(
                actor=owner, pending=PendingForfeiture.objects.get(), reason="member was ill"
            )
            services.return_loan(actor=owner, loan=loan3, today=D(2027, 4, 5))
            self.assertEqual(token(), tokens.HELD)
            # 7. fourth loan marked LOST by the owner's decision (no replacement charge)
            loan4 = services.issue_loan(
                actor=owner, member=member, copy=books[3], today=D(2027, 4, 6), due_on=D(2027, 5, 6)
            )
            services.mark_lost(
                actor=owner,
                loan=loan4,
                owner_confirmed=True,
                reason="member reports it stolen",
                today=D(2027, 4, 12),
            )
            self.assertEqual(token(), tokens.FORFEITED)
        # --- final state -------------------------------------------------------------------------
        loans = list(Loan.objects.order_by("id"))
        self.assertEqual(
            [(x.status, x.return_condition) for x in loans],
            [("returned", "good"), ("returned", "damaged"), ("returned", "good"), ("lost", "lost")],
        )
        self.assertEqual(Loan.objects.filter(status="active").count(), 0)
        self.assertEqual([bool(x.forfeiture_entry_id) for x in loans], [True, True, False, True])
        self.assertEqual(
            [
                c.status
                for c in BookCopy.objects.filter(pk__in=[b.pk for b in books]).order_by("id")
            ],
            ["available", "damaged", "available", "lost"],
        )
        forfeitures = FinancialTransaction.objects.filter(kind="token_forfeiture").order_by("id")
        self.assertEqual(
            [(f.recorded_by_system or "owner") for f in forfeitures],
            ["overdue_forfeiture_job", "owner", "owner"],
        )
        self.assertEqual(
            [
                e.forfeiture_trigger
                for e in TokenLedgerEntry.objects.filter(entry_type="forfeiture").order_by("id")
            ],
            ["overdue_auto", "damaged", "lost"],
        )
        for cycle in member.token_account.cycles.all():
            self.assertEqual(tokens.balance_paise(cycle), 0)  # all three tokens forfeited
        # audit trail with attribution; the automatic one has no actor and names the system
        trail = list(AuditEvent.objects.order_by("id").values_list("action", flat=True))
        for needed in (
            "loan.issued",
            "loan.renewed",
            "loan.token_forfeited",
            "loan.returned",
            "setting.grace_days_changed",
            "forfeiture.grace_change_declined",
            "loan.lost",
        ):
            self.assertIn(needed, trail)
        self.assertIsNone(AuditEvent.objects.get(action="loan.token_forfeited").actor_id)
        for action in ("loan.lost", "forfeiture.grace_change_declined"):
            self.assertEqual(AuditEvent.objects.get(action=action).actor_id, owner.pk)
        # outbox: stable unique keys, one durable row per notifiable fact
        stored = list(DomainEvent.objects.order_by("id"))
        keys = [e.dedupe_key for e in stored]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertEqual(
            sorted({e.event_type for e in stored}),
            sorted(
                [
                    "loan.issued",
                    "loan.renewed",
                    "loan.returned",
                    "loan.lost",
                    "token.forfeited",
                    "payment.receipted",
                    "token.collected",
                    "member.registered",
                    "subscription.activated",
                ]
            ),
        )
        self.assertEqual(sum(e.event_type == "token.forfeited" for e in stored), 3)
        # history is immutable
        with self.assertRaises(DatabaseError), transaction.atomic(), connection.cursor() as cur:
            cur.execute("UPDATE circulation_loan SET notes = 'edited' WHERE id = %s", [loan1.pk])
