"""Phase 3d: token refund, forfeiture, reversals and owner-approved adjustments."""

import random
from datetime import date, timedelta

from django.db import transaction
from django.test import TestCase

from audit.models import AuditEvent
from core.errors import RuleViolation
from core.models import IdSequence
from finance import corrections as fc
from finance import reporting, tokens
from finance.models import FinancialTransaction
from finance.services import Entry
from memberships import services as ms
from memberships.tests.helpers import TODAY, make_owner, register

K = fc.K


class Base(TestCase):
    def setUp(self):
        self.owner = make_owner()
        self.member = register(self.owner).member

    def rule(self, fn, *args, **kwargs):
        with self.assertRaises(RuleViolation) as ctx:
            fn(*args, **kwargs)
        return ctx.exception.code

    def refund(self, entries=None, **kw):
        return fc.refund_token(
            actor=self.owner,
            member=self.member,
            entries=entries or [Entry("cash", 50000)],
            today=kw.pop("today", TODAY),
            **kw,
        )

    def forfeit(self, trigger="lost", **kw):
        kw.setdefault("owner_confirmed", True)
        kw.setdefault("reason", "book not returned")
        return fc.forfeit_token(
            actor=self.owner,
            member=self.member,
            trigger=trigger,
            today=kw.pop("today", TODAY),
            **kw,
        )

    def state(self):
        return tokens.token_state(self.member)[1]


class RefundTests(Base):
    def test_full_refund_of_a_held_token_with_a_ref_receipt(self):
        posted = self.refund(entries=[Entry("cash", 20000), Entry("upi", 30000, reference="R1")])
        txn = posted.transactions[0]
        self.assertEqual(
            (txn.kind, txn.cash_flow, txn.amount_paise), ("token_refund", "out", 50000)
        )
        self.assertEqual(txn.receipt_no, "REF-2026-000001")
        self.assertEqual(self.state(), tokens.REFUNDED)
        cycle = tokens.latest_cycle(self.member)
        self.assertEqual(tokens.balance_paise(cycle), 0)
        self.assertEqual(
            list(cycle.entries.values_list("entry_type", "delta_paise")),
            [("collection", 50000), ("refund", -50000)],
        )
        self.assertEqual(
            list(self.member.subscriptions.values_list("price_paise_snapshot", flat=True)), [80000]
        )  # the subscription fee is untouched

    def test_refund_is_always_the_full_amount_never_partial_or_excess(self):
        for entries in ([Entry("cash", 25000)], [Entry("cash", 50001)]):
            self.assertIn(self.rule(self.refund, entries), {"underpaid", "overpaid"})
        self.assertEqual(self.state(), tokens.HELD)
        self.assertEqual(FinancialTransaction.objects.filter(kind="token_refund").count(), 0)
        self.assertFalse(IdSequence.objects.filter(name="receipt_token_refund").exists())

    def test_no_double_refund(self):
        self.refund()
        self.assertEqual(self.rule(self.refund), "token_already_refunded")
        self.assertEqual(FinancialTransaction.objects.filter(kind="token_refund").count(), 1)

    def test_a_forfeited_token_is_never_refundable(self):
        self.forfeit()
        self.assertEqual(self.rule(self.refund), "token_forfeited")

    def test_refund_is_blocked_while_a_book_is_outstanding(self):
        fc.OUTSTANDING_LOAN_CHECKS.append(lambda member: member.pk == self.member.pk)
        self.addCleanup(fc.OUTSTANDING_LOAN_CHECKS.clear)
        self.assertEqual(self.rule(self.refund), "loans_outstanding")
        self.assertEqual(self.state(), tokens.HELD)

    def test_refund_without_a_token(self):
        from members.models import Member

        other = Member.objects.create(
            membership_no="COV-M-000900",
            full_name="X",
            mobile_e164="+919000000001",
            joined_on=TODAY,
        )
        self.assertEqual(
            self.rule(
                fc.refund_token,
                actor=self.owner,
                member=other,
                entries=[Entry("cash", 50000)],
                today=TODAY,
            ),
            "token_missing",
        )

    def test_after_refund_borrowing_and_renewal_need_a_fresh_token(self):
        self.refund()
        codes = [c for c, _ in ms.borrowing_blockers(self.member, TODAY)]
        self.assertEqual(codes, ["token_missing"])
        ms.pay_fresh_token(
            actor=self.owner, member=self.member, entries=[Entry("cash", 50000)], today=TODAY
        )
        self.assertEqual(self.state(), tokens.HELD)
        self.assertEqual(tokens.latest_cycle(self.member).cycle_no, 2)

    def test_past_dated_refund_needs_reason_and_confirmation(self):
        past = TODAY - timedelta(days=3)
        self.assertEqual(self.rule(self.refund, payment_date=past), "backdate_reason_required")
        posted = self.refund(
            payment_date=past, backdate_reason="paid in person", backdate_confirmed=True
        )
        self.assertTrue(posted.transactions[0].backdated)

    def test_refund_is_audited(self):
        self.refund()
        self.assertTrue(AuditEvent.objects.filter(action="token.refunded").exists())


class ForfeitureTests(Base):
    def test_overdue_forfeiture_is_automatic_and_keeps_day_8_as_the_effective_date(self):
        day8 = date(2026, 12, 18)
        later = date(2026, 12, 25)  # the Dell was off: processed at the next launch
        posted = self.forfeit(
            "overdue_auto", effective_date=day8, owner_confirmed=False, reason="", today=later
        )
        txn = posted.transactions[0]
        self.assertEqual(
            (txn.kind, txn.cash_flow, txn.amount_paise), ("token_forfeiture", "none", 50000)
        )
        self.assertEqual(txn.effective_date, day8)
        self.assertGreater(txn.processed_at.date(), date(2000, 1, 1))
        self.assertFalse(txn.backdated)  # a rule-dated record is not an owner backdating
        self.assertEqual((txn.receipt_no, txn.receipt_document_id), ("", None))
        entry = txn.token_entries.get()
        self.assertEqual(
            (entry.forfeiture_trigger, entry.owner_confirmed, entry.delta_paise),
            ("overdue_auto", False, -50000),
        )
        self.assertEqual(self.state(), tokens.FORFEITED)

    def test_confirmed_forfeitures_need_confirmation_and_a_reason(self):
        for trigger in ("damaged", "poor", "lost", "grace_change_confirmed"):
            with self.subTest(trigger):
                self.assertEqual(
                    self.rule(self.forfeit, trigger, owner_confirmed=False), "confirmation_required"
                )
                self.assertEqual(self.rule(self.forfeit, trigger, reason="  "), "reason_required")
        self.assertEqual(self.state(), tokens.HELD)
        posted = self.forfeit("poor", reason="pages torn")
        entry = posted.transactions[0].token_entries.get()
        self.assertTrue(entry.owner_confirmed)
        self.assertEqual(entry.reason, "pages torn")

    def test_unknown_trigger(self):
        self.assertEqual(self.rule(self.forfeit, "boredom"), "trigger_invalid")

    def test_no_double_forfeiture_and_no_forfeiture_without_a_held_token(self):
        self.forfeit()
        self.assertEqual(self.rule(self.forfeit), "token_not_held")
        self.assertEqual(FinancialTransaction.objects.filter(kind="token_forfeiture").count(), 1)

    def test_forfeiture_has_its_own_uuid_and_does_not_consume_a_receipt_number(self):
        before = IdSequence.objects.filter(name__startswith="receipt_").count()
        txn = self.forfeit().transactions[0]
        self.assertIsNotNone(txn.internal_txn_id)
        self.assertEqual(IdSequence.objects.filter(name__startswith="receipt_").count(), before)

    def test_forfeiture_leaves_the_subscription_valid(self):
        self.forfeit()
        self.assertIsNotNone(ms.current_subscription(self.member, TODAY))

    def test_forfeiture_is_audited_with_both_dates(self):
        self.forfeit(
            "overdue_auto",
            effective_date=TODAY - timedelta(days=2),
            owner_confirmed=False,
            reason="",
        )
        event = AuditEvent.objects.get(action="token.forfeited")
        self.assertIn("processed_at", event.summary)
        self.assertEqual(event.summary["trigger"], "overdue_auto")


class ReversalTests(Base):
    def reverse(self, txn, **kw):
        kw.setdefault("reason", "entered by mistake")
        return fc.reverse_transaction(
            actor=self.owner, transaction_id=txn.pk, today=kw.pop("today", TODAY), **kw
        )

    def test_reversing_a_forfeiture_restores_the_token_and_keeps_the_original(self):
        original = self.forfeit().transactions[0]
        snapshot = FinancialTransaction.objects.filter(pk=original.pk).values().get()
        reversal = self.reverse(original).transactions[0]
        self.assertEqual(
            FinancialTransaction.objects.filter(pk=original.pk).values().get(), snapshot
        )
        self.assertEqual(reversal.kind, "reversal")
        self.assertEqual(
            (reversal.reverses_id, reversal.cash_flow, reversal.receipt_no),
            (original.pk, "none", ""),
        )
        self.assertEqual(original.reversed_by.pk, reversal.pk)
        self.assertEqual(self.state(), tokens.HELD)
        self.assertEqual(tokens.balance_paise(tokens.latest_cycle(self.member)), 50000)
        self.assertEqual(self.rule(self.reverse, original), "already_reversed")

    def test_a_reversed_forfeiture_cannot_simply_be_forfeited_again(self):
        original = self.forfeit().transactions[0]
        self.reverse(original)
        self.assertEqual(self.rule(self.forfeit), "forfeiture_already_recorded")
        self.refund()  # but the restored token can be refunded
        self.assertEqual(self.state(), tokens.REFUNDED)

    def test_reversing_an_unused_token_collection_pays_it_back_and_voids_it(self):
        original = FinancialTransaction.objects.get(kind="token_collection")
        self.assertEqual(self.rule(self.reverse, original), "payment_required")
        reversal = self.reverse(original, entries=[Entry("cash", 50000)]).transactions[0]
        self.assertEqual((reversal.cash_flow, reversal.amount_paise), ("out", 50000))
        self.assertEqual(self.state(), tokens.REVERSED)
        self.assertEqual(tokens.balance_paise(tokens.latest_cycle(self.member)), 0)
        self.assertEqual(
            [c for c, _ in ms.borrowing_blockers(self.member, TODAY)], ["token_missing"]
        )
        ms.pay_fresh_token(
            actor=self.owner, member=self.member, entries=[Entry("cash", 50000)], today=TODAY
        )
        self.assertEqual(self.state(), tokens.HELD)

    def test_a_used_token_collection_cannot_be_reversed(self):
        original = FinancialTransaction.objects.get(kind="token_collection")
        self.forfeit()
        self.assertEqual(
            self.rule(self.reverse, original, entries=[Entry("cash", 50000)]), "token_in_use"
        )

    def test_only_the_latest_token_cycle_can_be_corrected(self):
        first = self.forfeit().transactions[0]
        ms.pay_fresh_token(
            actor=self.owner, member=self.member, entries=[Entry("cash", 50000)], today=TODAY
        )
        self.assertEqual(self.rule(self.reverse, first), "cycle_superseded")
        self.assertEqual(self.state(), tokens.HELD)

    def test_subscription_fees_are_never_reversed_or_refunded(self):
        fee = FinancialTransaction.objects.get(kind="subscription_payment")
        self.assertEqual(
            self.rule(self.reverse, fee, entries=[Entry("cash", 80000)]), "fee_not_reversible"
        )
        self.assertEqual(FinancialTransaction.objects.filter(kind="reversal").count(), 0)

    def test_refunds_and_reversals_cannot_be_reversed(self):
        refund = self.refund().transactions[0]
        self.assertEqual(
            self.rule(self.reverse, refund, entries=[Entry("cash", 50000)]), "refund_not_reversible"
        )
        adj = fc.post_adjustment(
            actor=self.owner,
            member=self.member,
            direction="none",
            amount_paise=100,
            reason="note",
            owner_approved=True,
            today=TODAY,
        ).transactions[0]
        rev = self.reverse(adj).transactions[0]
        self.assertEqual(self.rule(self.reverse, rev), "reversal_not_reversible")

    def test_a_reason_is_mandatory(self):
        original = self.forfeit().transactions[0]
        self.assertEqual(self.rule(self.reverse, original, reason="  "), "reason_required")

    def test_reversing_a_cash_adjustment_moves_cash_the_other_way(self):
        adj = fc.post_adjustment(
            actor=self.owner,
            member=self.member,
            direction="in",
            amount_paise=700,
            reason="found cash",
            owner_approved=True,
            entries=[Entry("upi", 700)],
            today=TODAY,
        ).transactions[0]
        rev = self.reverse(adj, entries=[Entry("cash", 700)]).transactions[0]
        self.assertEqual(rev.cash_flow, "out")
        out = reporting.collections(TODAY, TODAY)
        self.assertEqual((out["adjustments_in"], out["reversals_out"]), (700, 700))

    def test_reversal_is_audited_and_linked_in_the_audit_summary(self):
        original = self.forfeit().transactions[0]
        self.reverse(original)
        event = AuditEvent.objects.get(action="finance.reversal")
        self.assertEqual(event.entity_id, str(original.internal_txn_id))


class AdjustmentTests(Base):
    def adjust(self, **kw):
        kw.setdefault("direction", "out")
        kw.setdefault("amount_paise", 50000)
        kw.setdefault("reason", "lost book was recovered")
        kw.setdefault("owner_approved", True)
        kw.setdefault("confirm_not_fee_refund", True)
        kw.setdefault("entries", [Entry("cash", kw["amount_paise"])])
        return fc.post_adjustment(actor=self.owner, member=self.member, today=TODAY, **kw)

    def test_recovered_lost_book_is_settled_by_an_adjustment_not_an_automatic_reversal(self):
        original = self.forfeit("lost").transactions[0]
        txn = self.adjust(related_id=original.pk).transactions[0]
        self.assertEqual(
            (txn.kind, txn.receipt_no, txn.adjusts_id),
            ("adjustment", "ADJ-2026-000001", original.pk),
        )
        self.assertEqual(self.state(), tokens.FORFEITED)  # the forfeiture stands
        self.assertEqual(FinancialTransaction.objects.filter(kind="reversal").count(), 0)

    def test_approval_and_reason_are_mandatory(self):
        self.assertEqual(self.rule(self.adjust, owner_approved=False), "approval_required")
        self.assertEqual(self.rule(self.adjust, reason=" "), "reason_required")
        self.assertEqual(FinancialTransaction.objects.filter(kind="adjustment").count(), 0)

    def test_directions_and_split_payments(self):
        self.adjust(
            direction="in",
            amount_paise=1000,
            entries=[Entry("cash", 400), Entry("upi", 600, reference="X")],
        )
        self.adjust(direction="none", amount_paise=250, entries=[])
        out = reporting.collections(TODAY, TODAY)
        self.assertEqual(out["adjustments_in"], 1000)
        self.assertEqual(out["adjustments_out"], 0)
        self.assertEqual(
            self.rule(self.adjust, direction="none", entries=[Entry("cash", 1)]),
            "unexpected_payment",
        )
        self.assertEqual(self.rule(self.adjust, direction="sideways"), "direction_invalid")

    def test_amounts_must_be_positive_whole_paise(self):
        for bad in (0, -5, 10.5):
            self.assertEqual(
                self.rule(self.adjust, amount_paise=bad, entries=[Entry("cash", 100)]),
                "amount_invalid",
            )

    def test_unrelated_transactions_cannot_be_referenced(self):
        other = register(self.owner, name="Other", number="9811122233").member
        foreign = FinancialTransaction.objects.filter(member=other).first()
        self.assertEqual(self.rule(self.adjust, related_id=foreign.pk), "related_unknown")

    def test_adjustment_numbering_is_separate_from_the_other_sequences(self):
        a = self.adjust().transactions[0]
        b = self.adjust().transactions[0]
        self.assertEqual((a.receipt_no, b.receipt_no), ("ADJ-2026-000001", "ADJ-2026-000002"))
        self.assertEqual(
            FinancialTransaction.objects.get(kind="subscription_payment").receipt_no,
            "SUB-2026-000001",
        )

    def test_backdated_adjustment_needs_reason_and_confirmation(self):
        past = TODAY - timedelta(days=1)
        self.assertEqual(self.rule(self.adjust, payment_date=past), "backdate_reason_required")
        txn = self.adjust(
            payment_date=past, backdate_reason="found later", backdate_confirmed=True
        ).transactions[0]
        self.assertTrue(txn.backdated)

    def test_failed_adjustment_leaves_no_trace(self):
        with self.assertRaises(RuleViolation):
            self.adjust(entries=[Entry("cash", 1)])
        self.assertFalse(IdSequence.objects.filter(name="receipt_adjustment").exists())
        self.assertFalse(AuditEvent.objects.filter(action="finance.adjustment").exists())


class LedgerInvariantTests(TestCase):
    """Random sequences of legal and illegal operations never break the ledger rules."""

    def test_random_operations_keep_every_invariant(self):
        owner = make_owner()
        member = register(owner).member
        rng = random.Random(20261111)
        ops = []
        for _ in range(120):
            op = rng.choice(["refund", "forfeit", "fresh", "reverse", "adjust", "renew"])
            ops.append(op)
            try:
                with transaction.atomic():
                    if op == "refund":
                        fc.refund_token(
                            actor=owner, member=member, entries=[Entry("cash", 50000)], today=TODAY
                        )
                    elif op == "forfeit":
                        fc.forfeit_token(
                            actor=owner,
                            member=member,
                            trigger="lost",
                            owner_confirmed=True,
                            reason="r",
                            today=TODAY,
                        )
                    elif op == "fresh":
                        ms.pay_fresh_token(
                            actor=owner, member=member, entries=[Entry("upi", 50000)], today=TODAY
                        )
                    elif op == "reverse":
                        txn = rng.choice(list(FinancialTransaction.objects.filter(member=member)))
                        entries = (
                            [Entry("cash", txn.amount_paise)]
                            if txn.cash_flow == "in"
                            else (
                                [Entry("cash", txn.amount_paise)] if txn.cash_flow == "out" else []
                            )
                        )
                        fc.reverse_transaction(
                            actor=owner,
                            transaction_id=txn.pk,
                            reason="r",
                            entries=entries,
                            today=TODAY,
                        )
                    elif op == "adjust":
                        fc.post_adjustment(
                            actor=owner,
                            member=member,
                            direction="none",
                            amount_paise=rng.randint(1, 999),
                            reason="r",
                            owner_approved=True,
                            today=TODAY,
                        )
                    else:
                        ms.renew_subscription(
                            actor=owner,
                            member=member,
                            plan_code="6m",
                            entries=[Entry("cash", 80000)],
                            today=TODAY + timedelta(days=rng.randint(0, 400)),
                            activation_date=None,
                        )
            except RuleViolation:
                pass
        for cycle in member.token_account.cycles.all():
            self.assertGreaterEqual(tokens.balance_paise(cycle), 0)
            self.assertLessEqual(tokens.balance_paise(cycle), cycle.amount_snapshot_paise)
            kinds = list(cycle.entries.values_list("entry_type", flat=True))
            self.assertEqual(kinds.count("collection"), 1)
            self.assertLessEqual(kinds.count("refund"), 1)
            self.assertLessEqual(kinds.count("forfeiture"), 1)
        held = [
            c for c in member.token_account.cycles.all() if tokens.cycle_state(c) == tokens.HELD
        ]
        self.assertLessEqual(len(held), 1)
        for t in FinancialTransaction.objects.all():
            if t.cash_flow == "none":
                self.assertEqual(t.entries.count(), 0)
            else:
                self.assertEqual(sum(e.amount_paise for e in t.entries.all()), t.amount_paise)
        subs = list(member.subscriptions.order_by("starts_on"))
        for a, b in zip(subs, subs[1:], strict=False):
            self.assertLessEqual(a.expires_on, b.starts_on)
        self.assertGreater(len(set(ops)), 3)
