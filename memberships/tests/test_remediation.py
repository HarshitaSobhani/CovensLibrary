"""H2 backdating audit trail and D-12 anonymised-member eligibility."""

from datetime import date, timedelta

from django.test import TestCase

from audit.models import AuditEvent
from core.errors import RuleViolation
from finance import corrections as fc
from finance import tokens
from finance.models import FinancialTransaction
from finance.services import Entry
from members import services as members
from memberships import services as ms

from .helpers import TODAY, forfeit, make_owner, register, renew


def last(action):
    return AuditEvent.objects.filter(action=action).latest("id")


class BackdatingAuditTests(TestCase):
    def setUp(self):
        self.owner = make_owner()

    def test_a_normal_registration_records_that_it_was_not_backdated(self):
        register(self.owner)
        s = last("membership.registered").summary
        self.assertIs(s["backdated"], False)
        self.assertIs(s["event_date_backdated"], False)
        self.assertEqual((s["event_date"], s["effective_date"]), ("2026-11-11", "2026-11-11"))
        self.assertIn("processed_at", s)
        self.assertNotIn("backdate_reason", s)

    def test_financial_backdating_records_dates_reason_and_confirmation(self):
        past = date(2026, 11, 1)
        register(
            self.owner,
            activation_date=past,
            payment_date=past,
            backdate_reason="Joined on the 1st, entered late",
            backdate_confirmed=True,
        )
        for action in ("membership.registered", "token.collected"):
            s = last(action).summary
            with self.subTest(action):
                self.assertIs(s["backdated"], True)
                self.assertEqual(s["effective_date"], "2026-11-01")
                self.assertEqual(s["backdate_reason"], "Joined on the 1st, entered late")
                self.assertIs(s["backdate_owner_confirmed"], True)
                txn = FinancialTransaction.objects.exclude(receipt_no="").first()
                self.assertEqual(s["processed_at"][:10], txn.processed_at.date().isoformat())
        self.assertEqual(last("membership.registered").summary["event_date"], "2026-11-01")

    def test_a_backdated_start_with_a_payment_made_today_is_still_audited(self):
        """The non-financial case: only the subscription start date is in the past."""
        register(
            self.owner,
            activation_date=date(2026, 11, 1),
            backdate_reason="Started using the library on the 1st",
            backdate_confirmed=True,
        )
        txn = FinancialTransaction.objects.get(kind="subscription_payment")
        self.assertFalse(txn.backdated)  # the payment itself is dated today
        s = last("membership.registered").summary
        self.assertIs(s["backdated"], True)
        self.assertIs(s["event_date_backdated"], True)
        self.assertEqual((s["event_date"], s["effective_date"]), ("2026-11-01", "2026-11-11"))
        self.assertEqual(s["backdate_reason"], "Started using the library on the 1st")
        self.assertIs(s["backdate_owner_confirmed"], True)

    def test_late_renewal_backdated_to_the_expiry_is_audited(self):
        member = register(self.owner).member
        today = date(2027, 6, 20)
        renew(
            self.owner,
            member,
            today=today,
            activation_date=date(2027, 5, 11),
            backdate_reason="Forgot to record",
            backdate_confirmed=True,
            payment_date=today,
        )
        s = last("subscription.renewed").summary
        self.assertIs(s["backdated"], True)
        self.assertEqual(s["event_date"], "2027-05-11")
        self.assertEqual(s["backdate_reason"], "Forgot to record")

    def test_fresh_token_refund_reversal_and_adjustment_carry_the_same_fields(self):
        member = register(self.owner).member
        past = TODAY - timedelta(days=2)
        kw = {
            "backdate_reason": "Handled two days ago",
            "backdate_confirmed": True,
            "payment_date": past,
        }
        fc.post_adjustment(
            actor=self.owner,
            member=member,
            direction="in",
            amount_paise=500,
            reason="found",
            owner_approved=True,
            entries=[Entry("cash", 500)],
            today=TODAY,
            **kw,
        )
        fc.refund_token(
            actor=self.owner, member=member, entries=[Entry("cash", 50000)], today=TODAY, **kw
        )
        ms.pay_fresh_token(
            actor=self.owner, member=member, entries=[Entry("cash", 50000)], today=TODAY, **kw
        )
        adj = FinancialTransaction.objects.get(kind="adjustment")
        fc.reverse_transaction(
            actor=self.owner,
            transaction_id=adj.pk,
            reason="entered twice",
            entries=[Entry("cash", 500)],
            today=TODAY,
            **kw,
        )
        for action in (
            "finance.adjustment",
            "token.refunded",
            "token.collected",
            "finance.reversal",
        ):
            s = last(action).summary
            with self.subTest(action):
                self.assertIs(s["backdated"], True)
                self.assertEqual(s["effective_date"], past.isoformat())
                self.assertEqual(s["backdate_reason"], "Handled two days ago")
                self.assertIs(s["backdate_owner_confirmed"], True)
                self.assertIn("processed_at", s)

    def test_a_late_automatic_forfeiture_keeps_effective_and_processing_dates_apart(self):
        member = register(self.owner).member
        day8 = date(2026, 12, 18)
        fc.forfeit_token(
            actor=self.owner,
            member=member,
            trigger="overdue_auto",
            effective_date=day8,
            today=date(2026, 12, 25),
        )
        s = last("token.forfeited").summary
        self.assertEqual(s["effective_date"], "2026-12-18")
        self.assertIs(s["backdated"], False)  # a rule-dated record, not an owner backdating
        self.assertIn("processed_at", s)
        self.assertIs(s["owner_confirmed"], False)

    def test_the_audit_log_never_contains_payment_secrets_or_names(self):
        register(self.owner, name="Zainab Secret-Name")
        blob = " ".join(str(e.summary) for e in AuditEvent.objects.all())
        self.assertNotIn("Zainab", blob)


class AnonymisedMemberEligibilityTests(TestCase):
    """D-12: an eligible full token refund is allowed after anonymisation; nothing else returns."""

    def setUp(self):
        self.owner = make_owner()
        self.member = register(self.owner).member

    def anonymise(self):
        members.anonymise_member(self.member, actor=self.owner, reason="request", confirm=True)
        self.member.refresh_from_db()

    def blockers(self):
        return [c for c, _ in ms.borrowing_blockers(self.member, TODAY)]

    def test_borrowing_stays_blocked_even_with_a_valid_subscription_and_held_token(self):
        self.assertEqual(self.blockers(), [])
        self.anonymise()
        self.assertIn("anonymised", self.blockers())
        self.assertIsNotNone(ms.current_subscription(self.member, TODAY))  # subscription untouched
        self.assertEqual(tokens.token_state(self.member)[1], tokens.HELD)  # token untouched

    def test_the_full_token_refund_is_allowed_after_anonymisation(self):
        self.anonymise()
        posted = fc.refund_token(
            actor=self.owner, member=self.member, entries=[Entry("cash", 50000)], today=TODAY
        )
        self.assertEqual(posted.transactions[0].amount_paise, 50000)
        self.assertEqual(tokens.token_state(self.member)[1], tokens.REFUNDED)
        self.assertIn("anonymised", self.blockers())

    def test_a_partial_or_excess_refund_is_still_refused(self):
        self.anonymise()
        with self.assertRaises(RuleViolation):
            fc.refund_token(
                actor=self.owner, member=self.member, entries=[Entry("cash", 25000)], today=TODAY
            )

    def test_renewal_and_a_fresh_token_remain_blocked_after_refund(self):
        self.anonymise()
        fc.refund_token(
            actor=self.owner, member=self.member, entries=[Entry("cash", 50000)], today=TODAY
        )
        for fn, kw in (
            (
                ms.renew_subscription,
                dict(plan_code="6m", entries=[Entry("cash", 80000)], activation_date=TODAY),
            ),
            (ms.pay_fresh_token, dict(entries=[Entry("cash", 50000)])),
        ):
            with self.assertRaises(RuleViolation) as ctx:
                fn(actor=self.owner, member=self.member, today=TODAY, **kw)
            self.assertEqual(ctx.exception.code, "anonymised")

    def test_anonymising_a_forfeited_member_changes_nothing_about_the_forfeiture(self):
        forfeit(self.owner, self.member)
        self.anonymise()
        self.assertEqual(tokens.token_state(self.member)[1], tokens.FORFEITED)
        self.assertEqual(
            sorted(self.blockers()), ["anonymised", "member_not_active", "token_forfeited"]
        )

    def test_a_refunded_token_cannot_be_refunded_again_after_anonymisation(self):
        self.anonymise()
        fc.refund_token(
            actor=self.owner, member=self.member, entries=[Entry("cash", 50000)], today=TODAY
        )
        with self.assertRaises(RuleViolation) as ctx:
            fc.refund_token(
                actor=self.owner, member=self.member, entries=[Entry("cash", 50000)], today=TODAY
            )
        self.assertEqual(ctx.exception.code, "token_already_refunded")

    def test_anonymise_rejects_an_overlong_reason(self):
        with self.assertRaises(RuleViolation) as ctx:
            members.anonymise_member(self.member, actor=self.owner, reason="z" * 2001, confirm=True)
        self.assertEqual(ctx.exception.code, "reason_too_long")
        self.member.refresh_from_db()
        self.assertIsNone(self.member.anonymised_at)
