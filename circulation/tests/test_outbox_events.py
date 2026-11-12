"""Outbox producers for every Phase 3 and Phase 4 workflow: what is stored, when, and with what
guarantees (same transaction, no duplicates, no personal data)."""

import json
from unittest import mock

from django.test import TestCase

from audit.models import AuditEvent
from catalogue.models import BookCopy
from circulation import forfeiture, grace, services
from circulation.models import Loan
from core import outbox
from core.errors import RuleViolation
from core.models import DomainEvent
from finance import corrections as fc
from finance.models import FinancialTransaction
from finance.services import Entry
from members import services as members
from memberships import services as ms
from memberships.models import MemberSubscription
from memberships.tests.helpers import register

from .helpers import D, make_copies, make_owner, member_at, overdue_loan


def types(member=None, **kw):
    qs = DomainEvent.objects.filter(**kw)
    if member is not None:
        qs = qs.filter(member_id=member.pk)
    return sorted(qs.values_list("event_type", flat=True))


class Phase3EventTests(TestCase):
    def setUp(self):
        self.owner = make_owner()

    def test_registration_stores_one_event_per_fact_and_one_per_receipt_document(self):
        result = register(self.owner, today=D(2026, 11, 11))
        member = result.member
        self.assertEqual(
            types(member),
            ["member.registered", "payment.receipted", "subscription.activated", "token.collected"],
        )
        receipt = DomainEvent.objects.get(event_type="payment.receipted")
        self.assertEqual(receipt.dedupe_key, f"receipt:{result.document.pk}")
        self.assertEqual(
            [r["receipt_no"] for r in receipt.payload["receipts"]],
            ["SUB-2026-000001", "TOK-2026-000001"],
        )  # the combined printout: ONE event
        activated = DomainEvent.objects.get(event_type="subscription.activated")
        self.assertEqual(
            (activated.payload["starts_on"], activated.payload["expires_on"]),
            ("2026-11-11", "2027-05-11"),
        )
        self.assertEqual(activated.dedupe_key, f"subscription:{result.subscription.pk}:activated")

    def test_renewal_stores_a_renewal_and_a_receipt_but_no_token_event(self):
        member = register(self.owner).member
        before = DomainEvent.objects.count()
        sub = ms.renew_subscription(
            actor=self.owner,
            member=member,
            plan_code="12m",
            entries=[Entry("cash", 150000)],
            today=D(2027, 1, 10),
        ).subscription
        new = list(DomainEvent.objects.order_by("id")[before:])
        self.assertEqual(
            sorted(e.event_type for e in new), ["payment.receipted", "subscription.renewed"]
        )
        renewed = next(e for e in new if e.event_type == "subscription.renewed")
        self.assertEqual(renewed.dedupe_key, f"subscription:{sub.pk}:renewed")
        self.assertEqual(renewed.event_date, D(2027, 1, 10))

    def test_token_collection_refund_and_forfeiture_events(self):
        member = register(self.owner).member
        fc.forfeit_token(
            actor=self.owner,
            member=member,
            trigger="lost",
            owner_confirmed=True,
            reason="r",
            today=D(2026, 12, 1),
        )
        ms.pay_fresh_token(
            actor=self.owner, member=member, entries=[Entry("cash", 50000)], today=D(2026, 12, 2)
        )
        fc.refund_token(
            actor=self.owner, member=member, entries=[Entry("cash", 50000)], today=D(2026, 12, 3)
        )
        self.assertEqual(
            types(member, event_type__startswith="token."),
            ["token.collected", "token.collected", "token.forfeited", "token.refunded"],
        )
        forfeited = DomainEvent.objects.get(event_type="token.forfeited")
        self.assertEqual(
            (forfeited.payload["trigger"], forfeited.payload["system"]), ("lost", False)
        )
        refunded = DomainEvent.objects.get(event_type="token.refunded")
        txn = FinancialTransaction.objects.get(kind="token_refund")
        self.assertEqual(refunded.dedupe_key, f"token:{txn.internal_txn_id}:refunded")
        self.assertEqual(refunded.payload["receipt_no"], "REF-2026-000001")
        # every receipt document stored exactly one receipt event, adjustments included
        fc.post_adjustment(
            actor=self.owner,
            member=member,
            direction="none",
            amount_paise=100,
            reason="note",
            owner_approved=True,
            today=D(2026, 12, 4),
        )
        docs = (
            FinancialTransaction.objects.exclude(receipt_document=None)
            .values("receipt_document")
            .distinct()
            .count()
        )
        self.assertEqual(DomainEvent.objects.filter(event_type="payment.receipted").count(), docs)

    def test_reversals_and_forfeiture_records_do_not_create_receipt_events(self):
        member = register(self.owner).member
        before = DomainEvent.objects.filter(event_type="payment.receipted").count()
        txn = fc.forfeit_token(
            actor=self.owner,
            member=member,
            trigger="lost",
            owner_confirmed=True,
            reason="r",
            today=D(2026, 12, 1),
        ).transactions[0]
        fc.reverse_transaction(
            actor=self.owner, transaction_id=txn.pk, reason="mistake", today=D(2026, 12, 1)
        )
        self.assertEqual(DomainEvent.objects.filter(event_type="payment.receipted").count(), before)

    def test_an_event_is_not_stored_when_a_registration_is_refused(self):
        with self.assertRaises(RuleViolation):
            register(self.owner, subscription_entries=[Entry("cash", 79999)])
        self.assertEqual(DomainEvent.objects.count(), 0)


class Phase4EventTests(TestCase):
    def setUp(self):
        self.owner = make_owner()
        self.member, self.copy, self.loan = overdue_loan(self.owner)

    def test_loan_issue_renewal_and_return_events(self):
        issued = DomainEvent.objects.get(event_type="loan.issued")
        self.assertEqual(issued.dedupe_key, f"loan:{self.loan.pk}:issued")
        self.assertEqual(
            (issued.event_date, issued.payload["due_on"]), (D(2026, 12, 1), "2026-12-31")
        )
        self.assertEqual(issued.occurred_at, self.loan.issued_processed_at)
        services.renew_loan(actor=self.owner, loan=self.loan, today=D(2026, 12, 20))
        renewed = DomainEvent.objects.get(event_type="loan.renewed")
        self.assertEqual(
            renewed.payload,
            {"loan_id": self.loan.pk, "old_due_on": "2026-12-31", "new_due_on": "2027-01-30"},
        )
        services.return_loan(actor=self.owner, loan=self.loan, today=D(2026, 12, 22))
        returned = DomainEvent.objects.get(event_type="loan.returned")
        self.assertEqual(
            (returned.payload["outcome"], returned.event_date), ("good", D(2026, 12, 22))
        )

    def test_condition_based_returns_and_lost_books(self):
        services.return_loan(
            actor=self.owner,
            loan=self.loan,
            condition="damaged",
            owner_confirmed=True,
            reason="water",
            today=D(2026, 12, 10),
        )
        returned = DomainEvent.objects.get(event_type="loan.returned")
        self.assertEqual(returned.payload["outcome"], "damaged")
        self.assertEqual(DomainEvent.objects.filter(event_type="token.forfeited").count(), 1)
        member2, copy2, loan2 = overdue_loan(self.owner, name="Bo", number="9811100011")
        services.mark_lost(
            actor=self.owner, loan=loan2, owner_confirmed=True, reason="gone", today=D(2026, 12, 12)
        )
        lost = DomainEvent.objects.get(event_type="loan.lost")
        self.assertEqual(
            (lost.dedupe_key, lost.payload["outcome"]), (f"loan:{loan2.pk}:lost", "lost")
        )

    def test_the_automatic_day_8_forfeiture_event_names_the_system_and_keeps_both_dates(self):
        forfeiture.process_overdue_forfeitures(today=D(2027, 3, 1))  # processed late
        event = DomainEvent.objects.get(event_type="token.forfeited")
        txn = FinancialTransaction.objects.get(kind="token_forfeiture")
        self.assertEqual(event.dedupe_key, f"token-forfeiture:{txn.internal_txn_id}")
        self.assertEqual(
            (event.payload["trigger"], event.payload["system"]), ("overdue_auto", True)
        )
        self.assertEqual(event.event_date, D(2027, 1, 8))  # the business date: day 8
        self.assertEqual(event.occurred_at, txn.processed_at)  # the real processing time
        self.assertEqual(txn.recorded_by_system, "overdue_forfeiture_job")
        forfeiture.process_overdue_forfeitures(today=D(2027, 3, 2))
        self.assertEqual(DomainEvent.objects.filter(event_type="token.forfeited").count(), 1)

    def test_a_grace_change_confirmation_stores_one_forfeiture_event_not_two(self):
        grace.change_grace_days(actor=self.owner, new_days=3, today=D(2027, 1, 5))
        pending = self.loan.pending_forfeitures.get()
        grace.confirm_pending(
            actor=self.owner, pending=pending, reason="shorter grace", today=D(2027, 1, 5)
        )
        events = DomainEvent.objects.filter(event_type="token.forfeited")
        self.assertEqual([e.payload["trigger"] for e in events], ["grace_change_confirmed"])

    def test_a_declined_decision_stores_no_forfeiture_event(self):
        grace.change_grace_days(actor=self.owner, new_days=3, today=D(2027, 1, 5))
        grace.decline_pending(
            actor=self.owner, pending=self.loan.pending_forfeitures.get(), reason="ill"
        )
        self.assertFalse(DomainEvent.objects.filter(event_type="token.forfeited").exists())


class AtomicityTests(TestCase):
    """A failure AFTER the event was inserted rolls back the business data AND the event."""

    def run_failing(self, module_path, operation, *, tables):
        real = outbox.emit

        def emit_then_fail(*args, **kwargs):
            real(*args, **kwargs)
            raise RuntimeError("boom after the event was stored")

        before = {name: model.objects.count() for name, model in tables.items()}
        events_before = DomainEvent.objects.count()
        with mock.patch(module_path, side_effect=emit_then_fail):
            with self.assertRaises(RuntimeError):
                operation()
        self.assertEqual({n: m.objects.count() for n, m in tables.items()}, before)
        self.assertEqual(DomainEvent.objects.count(), events_before)

    def setUp(self):
        self.owner = make_owner()

    def tables(self):
        return {
            "txns": FinancialTransaction,
            "subs": MemberSubscription,
            "loans": Loan,
            "audit": AuditEvent,
        }

    def test_registration_rolls_back_with_its_events(self):
        from members.models import Member

        tables = {**self.tables(), "members": Member}
        self.run_failing(
            "memberships.services.outbox.emit", lambda: register(self.owner), tables=tables
        )

    def test_receipt_event_failure_rolls_back_the_payment(self):
        member = member_at(self.owner)
        self.run_failing(
            "finance.services.outbox.emit",
            lambda: fc.post_adjustment(
                actor=self.owner,
                member=member,
                direction="none",
                amount_paise=100,
                reason="r",
                owner_approved=True,
                today=D(2026, 12, 1),
            ),
            tables=self.tables(),
        )

    def test_refund_and_forfeiture_event_failures_roll_back_the_ledger(self):
        member = member_at(self.owner)
        self.run_failing(
            "finance.corrections.outbox.emit",
            lambda: fc.refund_token(
                actor=self.owner,
                member=member,
                entries=[Entry("cash", 50000)],
                today=D(2026, 12, 1),
            ),
            tables=self.tables(),
        )

    def test_loan_event_failures_roll_back_the_loan_and_the_copy_state(self):
        member = member_at(self.owner)
        copy = make_copies(self.owner, 1)[0]
        self.run_failing(
            "circulation.services.outbox.emit",
            lambda: services.issue_loan(
                actor=self.owner, member=member, copy=copy, today=D(2026, 12, 1)
            ),
            tables=self.tables(),
        )
        self.assertEqual(BookCopy.objects.get(pk=copy.pk).status, "available")


class PrivacyTests(TestCase):
    def test_no_event_ever_contains_a_name_contact_detail_or_free_text(self):
        owner = make_owner()
        member, copy, loan = overdue_loan(owner, name="Zainab Qureshi", number="9876512345")
        services.renew_loan(actor=owner, loan=loan, today=D(2026, 12, 20))
        forfeiture.process_overdue_forfeitures(today=D(2027, 3, 1))
        members.anonymise_member(member, actor=owner, reason="member request", confirm=True)
        fc.refund_token(
            actor=owner, member=member, entries=[Entry("cash", 50000)], today=D(2027, 3, 2)
        ) if False else None
        blob = json.dumps([e.payload for e in DomainEvent.objects.all()]) + " ".join(
            e.dedupe_key for e in DomainEvent.objects.all()
        )
        for needle in ("Zainab", "Qureshi", "9876512345", "member request", "@"):
            self.assertNotIn(needle, blob)

    def test_events_emitted_after_anonymisation_do_not_expose_the_old_name(self):
        owner = make_owner()
        member = register(owner, name="Farida Khan", number="9811144444").member
        members.anonymise_member(member, actor=owner, reason="request", confirm=True)
        fc.refund_token(
            actor=owner, member=member, entries=[Entry("cash", 50000)], today=D(2026, 12, 3)
        )
        after = list(
            DomainEvent.objects.filter(
                event_type__in=["token.refunded", "payment.receipted"]
            ).order_by("-id")[:2]
        )
        self.assertEqual(len(after), 2)
        for event in after:
            self.assertNotIn("Farida", json.dumps(event.payload))
            self.assertNotIn("Khan", json.dumps(event.payload))
        self.assertEqual(
            DomainEvent.objects.get(event_type="member.registered").payload,
            {"membership_no": member.membership_no},
        )  # an identifier, not a name
