"""One end-to-end PostgreSQL regression test of the complete Phase 3 member journey.

registration -> early renewal -> late renewal -> token forfeiture -> fresh token -> eligible token
refund -> (a further token, reversed) -> owner-approved adjustment -> anonymisation.

Each step asserts eligibility; the end asserts ledger integrity, gap-free receipt sequences, the
audit history and the member's final eligibility. Dates are injected (``today=``), so the test is
deterministic. The reversal step uses an unused second fresh token because, by the approved rules
(D-9), only the latest unused token collection can be reversed once a refund has happened.
"""

from datetime import date

from django.db import DatabaseError, connection, transaction
from django.test import TestCase
from django.urls import reverse

from audit.models import AuditEvent
from core.errors import RuleViolation
from finance import corrections as fc
from finance import reporting, tokens
from finance.models import FinancialTransaction, TokenLedgerEntry
from finance.services import Entry
from members import services as members
from memberships import policy
from memberships import services as ms

from .helpers import PW, make_owner

D = date


def codes(member, today):
    return [c for c, _ in ms.borrowing_blockers(member, today)]


class MemberJourneyTests(TestCase):
    def setUp(self):
        self.owner = make_owner()
        self.client.login(username="owner", password=PW)

    def refuses(self, fn, code, **kw):
        with self.assertRaises(RuleViolation) as ctx:
            fn(**kw)
        self.assertEqual(ctx.exception.code, code)

    def test_the_complete_journey(self):
        # 1. registration: subscription (split cash/UPI) and token, one combined printout --------
        reg = ms.register_member(
            actor=self.owner,
            member_fields={
                "full_name": "Asha Rao",
                "national_number": "9876543210",
                "region": "IN",
            },
            plan_code="6m",
            subscription_entries=[Entry("cash", 30000), Entry("upi", 50000, reference="U-1")],
            token_entries=[Entry("cash", 50000)],
            today=D(2026, 11, 11),
        )
        member = reg.member
        self.assertEqual(member.membership_no, "COV-M-000001")
        self.assertEqual(reg.document.kind, "registration")
        self.assertEqual(
            [line["receipt_no"] for line in reg.document.snapshot["lines"]],
            ["SUB-2026-000001", "TOK-2026-000001"],
        )
        first = reg.subscription
        self.assertEqual((first.starts_on, first.expires_on), (D(2026, 11, 11), D(2027, 5, 11)))
        self.assertEqual(codes(member, D(2026, 11, 11)), [])

        # 2. early renewal starts on the first expired day ---------------------------------------
        early = ms.renew_subscription(
            actor=self.owner,
            member=member,
            plan_code="12m",
            entries=[Entry("cash", 150000)],
            today=D(2027, 1, 10),
        )
        second = early.subscription
        self.assertEqual((second.starts_on, second.expires_on), (D(2027, 5, 11), D(2028, 5, 11)))
        self.assertEqual(early.transactions[0].receipt_no, "SUB-2027-000001")  # issuance year
        self.refuses(  # at most one scheduled subscription
            ms.renew_subscription,
            "scheduled_exists",
            actor=self.owner,
            member=member,
            plan_code="6m",
            entries=[Entry("cash", 80000)],
            today=D(2027, 1, 11),
        )

        # 3. late renewal starts on the owner-entered activation date ----------------------------
        late_day = D(2028, 8, 5)
        self.refuses(
            ms.renew_subscription,
            "activation_date_required",
            actor=self.owner,
            member=member,
            plan_code="6m",
            entries=[Entry("cash", 80000)],
            today=late_day,
        )
        late = ms.renew_subscription(
            actor=self.owner,
            member=member,
            plan_code="6m",
            entries=[Entry("cash", 80000)],
            activation_date=late_day,
            today=late_day,
        )
        third = late.subscription
        self.assertEqual((third.starts_on, third.expires_on), (late_day, D(2029, 2, 5)))
        self.assertEqual(third.token_cycle.cycle_no, 1)  # the token carried across all renewals
        self.assertEqual(codes(member, late_day), [])

        # 4. token forfeiture: subscription stays valid; renewal and borrowing are blocked -------
        forfeiture = fc.forfeit_token(
            actor=self.owner,
            member=member,
            trigger="lost",
            owner_confirmed=True,
            reason="book not returned",
            today=D(2028, 8, 6),
        ).transactions[0]
        self.assertEqual(tokens.token_state(member)[1], tokens.FORFEITED)
        self.assertIsNotNone(ms.current_subscription(member, D(2028, 8, 6)))
        self.assertEqual(codes(member, D(2028, 8, 6)), ["token_forfeited"])
        self.refuses(
            ms.renew_subscription,
            "token_forfeited",
            actor=self.owner,
            member=member,
            plan_code="6m",
            entries=[Entry("cash", 80000)],
            today=D(2028, 8, 6),
        )
        self.assertEqual(forfeiture.receipt_no, "")  # own record, no customer receipt

        # 5. fresh token: same member, new cycle, subscriptions untouched ------------------------
        before = list(member.subscriptions.values_list("starts_on", "expires_on"))
        fresh = ms.pay_fresh_token(
            actor=self.owner,
            member=member,
            entries=[Entry("upi", 50000, reference="U-2")],
            today=D(2028, 8, 6),
        )
        self.assertEqual(fresh.transactions[0].receipt_no, "TOK-2028-000001")
        self.assertEqual(tokens.token_state(member)[0].cycle_no, 2)
        self.assertEqual(before, list(member.subscriptions.values_list("starts_on", "expires_on")))
        self.assertEqual(member.membership_no, "COV-M-000001")
        self.assertEqual(codes(member, D(2028, 8, 6)), [])

        # 6. eligible full token refund ------------------------------------------------------------
        refund = fc.refund_token(
            actor=self.owner, member=member, entries=[Entry("cash", 50000)], today=D(2028, 8, 7)
        ).transactions[0]
        self.assertEqual((refund.receipt_no, refund.amount_paise), ("REF-2028-000001", 50000))
        self.assertEqual(tokens.token_state(member)[1], tokens.REFUNDED)
        self.assertEqual(codes(member, D(2028, 8, 7)), ["token_missing"])
        self.refuses(
            fc.reverse_transaction,
            "refund_not_reversible",
            actor=self.owner,
            transaction_id=refund.pk,
            reason="x",
            entries=[Entry("cash", 50000)],
            today=D(2028, 8, 7),
        )

        # 7. transaction reversal: a token collected by mistake is reversed ----------------------
        mistaken = ms.pay_fresh_token(
            actor=self.owner, member=member, entries=[Entry("cash", 50000)], today=D(2028, 8, 8)
        ).transactions[0]
        self.assertEqual(mistaken.receipt_no, "TOK-2028-000002")
        fee = FinancialTransaction.objects.filter(kind="subscription_payment").first()
        self.refuses(
            fc.reverse_transaction,
            "fee_not_reversible",
            actor=self.owner,
            transaction_id=fee.pk,
            reason="x",
            entries=[Entry("cash", fee.amount_paise)],
            today=D(2028, 8, 8),
        )
        reversal = fc.reverse_transaction(
            actor=self.owner,
            transaction_id=mistaken.pk,
            reason="entered by mistake",
            entries=[Entry("cash", 50000)],
            today=D(2028, 8, 8),
        ).transactions[0]
        self.assertEqual(
            (reversal.reverses_id, reversal.cash_flow, reversal.receipt_no),
            (mistaken.pk, "out", ""),
        )
        self.assertEqual(tokens.token_state(member)[1], tokens.REVERSED)
        self.assertEqual(codes(member, D(2028, 8, 8)), ["token_missing"])

        # 8. owner-approved adjustment (recovered lost book), never a fee refund -----------------
        self.refuses(
            fc.post_adjustment,
            "fee_refund_blocked",
            actor=self.owner,
            member=member,
            direction="out",
            amount_paise=50000,
            reason="x",
            owner_approved=True,
            confirm_not_fee_refund=True,
            related_id=fee.pk,
            entries=[Entry("cash", 50000)],
            today=D(2028, 8, 9),
        )
        adjustment = fc.post_adjustment(
            actor=self.owner,
            member=member,
            direction="out",
            amount_paise=50000,
            reason="the lost book was recovered",
            owner_approved=True,
            confirm_not_fee_refund=True,
            related_id=forfeiture.pk,
            entries=[Entry("cash", 50000)],
            today=D(2028, 8, 9),
        ).transactions[0]
        self.assertEqual(
            (adjustment.receipt_no, adjustment.adjusts_id), ("ADJ-2028-000001", forfeiture.pk)
        )
        self.assertEqual(tokens.token_state(member)[1], tokens.REVERSED)  # ledger not rewritten

        # 9. anonymisation: nothing is restored, the refund history and receipts survive ---------
        members.anonymise_member(member, actor=self.owner, reason="member request", confirm=True)
        member.refresh_from_db()
        day = D(2028, 8, 10)
        self.assertIn("anonymised", codes(member, day))
        self.refuses(
            ms.renew_subscription,
            "anonymised",
            actor=self.owner,
            member=member,
            plan_code="6m",
            entries=[Entry("cash", 80000)],
            today=day,
            activation_date=day,
        )
        self.refuses(
            ms.pay_fresh_token,
            "anonymised",
            actor=self.owner,
            member=member,
            entries=[Entry("cash", 50000)],
            today=day,
        )
        page = self.client.get(reverse("finance:receipt", args=[reg.document.pk]))
        self.assertContains(page, "Anonymised member")
        self.assertNotContains(page, "Asha")
        self.assertContains(page, "SUB-2026-000001")

        # ---- final ledger integrity -------------------------------------------------------------
        every = FinancialTransaction.objects.filter(member=member)
        self.assertEqual(every.count(), 10)
        for txn in every:
            paid = sum(e.amount_paise for e in txn.entries.all())
            self.assertEqual(paid, 0 if txn.cash_flow == "none" else txn.amount_paise, txn)
        self.assertEqual(len({t.internal_txn_id for t in every}), 10)
        balances = {c.cycle_no: tokens.balance_paise(c) for c in member.token_account.cycles.all()}
        self.assertEqual(balances, {1: 0, 2: 0, 3: 0})  # forfeited, refunded, reversed
        self.assertEqual(
            sorted(TokenLedgerEntry.objects.values_list("entry_type", flat=True)),
            sorted(
                ["collection"] * 3 + ["carry_forward"] * 2 + ["forfeiture", "refund", "reversal"]
            ),
        )
        # sequences: four separate per-year sequences, gap-free, issuance-year based
        numbers = sorted(every.exclude(receipt_no="").values_list("receipt_no", flat=True))
        self.assertEqual(
            numbers,
            sorted(
                [
                    "SUB-2026-000001",
                    "SUB-2027-000001",
                    "SUB-2028-000001",
                    "TOK-2026-000001",
                    "TOK-2028-000001",
                    "TOK-2028-000002",
                    "REF-2028-000001",
                    "ADJ-2028-000001",
                ]
            ),
        )
        self.assertEqual(
            list(every.filter(receipt_no="").order_by("id").values_list("kind", flat=True)),
            ["token_forfeiture", "reversal"],
        )
        # subscriptions: contiguous where renewed early, no overlap, snapshots intact
        subs = list(member.subscriptions.order_by("starts_on"))
        self.assertEqual(subs[0].expires_on, subs[1].starts_on)
        self.assertTrue(all(policy.last_valid_day(s.expires_on) < s.expires_on for s in subs))
        self.assertEqual([s.price_paise_snapshot for s in subs], [80000, 150000, 80000])
        # reports keep every category apart
        out = reporting.collections(D(2026, 1, 1), D(2030, 1, 1))
        self.assertEqual(
            (
                out["subscription_fees"],
                out["token_collections"],
                out["token_refunds"],
                out["adjustments_out"],
                out["reversals_out"],
                out["tokens_forfeited"],
            ),
            (310000, 150000, 50000, 50000, 50000, 50000),
        )
        # immutability still holds after the whole journey
        for table in (
            "finance_financialtransaction",
            "finance_paymententry",
            "finance_tokenledgerentry",
            "finance_receiptdocument",
        ):
            with self.assertRaisesMessage(DatabaseError, "append-only"):
                with transaction.atomic(), connection.cursor() as cur:
                    cur.execute(f"UPDATE {table} SET id = id")

        # ---- audit history, in order, without personal data ----
        trail = list(AuditEvent.objects.order_by("id").values_list("action", flat=True))
        expected = [
            "member.create",
            "membership.registered",
            "token.collected",
            "subscription.renewed",
            "subscription.renewed",
            "token.forfeited",
            "token.collected",
            "token.refunded",
            "token.collected",
            "finance.reversal",
            "finance.adjustment",
            "member.anonymise",
        ]
        it = iter(trail)
        self.assertTrue(all(step in it for step in expected), trail)  # in this order
        blob = " ".join(str(e.summary) for e in AuditEvent.objects.all())
        self.assertNotIn("Asha", blob)
        self.assertNotIn("9876543210", blob)
        # every audited financial action names the owner
        for event in AuditEvent.objects.filter(action__startswith=("finance.", "token.")):
            self.assertEqual(event.actor_id, self.owner.pk)
