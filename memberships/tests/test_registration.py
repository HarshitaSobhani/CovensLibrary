"""Phase 3c: atomic registration, renewal and fresh-token payment (PostgreSQL)."""

from datetime import date
from unittest import mock

from django.db import transaction
from django.test import TestCase

from audit.models import AuditEvent
from core.errors import RuleViolation
from core.models import IdSequence
from finance import tokens
from finance.models import FinancialTransaction, PaymentEntry, ReceiptDocument, TokenLedgerEntry
from finance.services import Entry
from members.models import Member
from memberships import policy
from memberships import services as ms
from memberships.models import MemberSubscription

from .helpers import TODAY, fields, forfeit, make_owner, register, renew


class RegistrationTests(TestCase):
    def setUp(self):
        self.owner = make_owner()

    def test_registration_creates_member_subscription_token_and_one_printout(self):
        result = register(self.owner)
        m, sub = result.member, result.subscription
        self.assertEqual(m.membership_no, "COV-M-000001")
        self.assertEqual((sub.starts_on, sub.expires_on), (date(2026, 11, 11), date(2027, 5, 11)))
        self.assertEqual(sub.last_valid_day, date(2027, 5, 10))
        self.assertEqual(
            (sub.plan_name_snapshot, sub.price_paise_snapshot), ("6-month subscription", 80000)
        )
        self.assertEqual(policy.state(sub.period, TODAY), policy.ACTIVE)
        cycle, state = tokens.token_state(m)
        self.assertEqual(
            (cycle.cycle_no, state, cycle.amount_snapshot_paise), (1, tokens.HELD, 50000)
        )
        self.assertEqual(tokens.balance_paise(cycle), 50000)
        lines = result.document.snapshot["lines"]
        self.assertEqual(
            [(ln["kind"], ln["receipt_no"], ln["amount_paise"]) for ln in lines],
            [
                ("subscription_payment", "SUB-2026-000001", 80000),
                ("token_collection", "TOK-2026-000001", 50000),
            ],
        )
        self.assertEqual(result.document.kind, "registration")
        self.assertEqual(sub.payment_txn.receipt_document_id, result.document.pk)
        self.assertEqual(ms.borrowing_blockers(m, TODAY), [])

    def test_twelve_month_plan_costs_1500_and_has_the_same_benefits(self):
        result = register(self.owner, plan="12m")
        sub = result.subscription
        self.assertEqual((sub.price_paise_snapshot, sub.duration_months_snapshot), (150000, 12))
        self.assertEqual(sub.expires_on, date(2027, 11, 11))
        self.assertEqual(ms.borrowing_blockers(result.member, TODAY), [])

    def test_split_cash_and_upi_for_both_obligations(self):
        result = register(
            self.owner,
            subscription_entries=[Entry("cash", 30000), Entry("upi", 50000, reference="A")],
            token_entries=[Entry("cash", 20000), Entry("upi", 30000)],
        )
        self.assertEqual(PaymentEntry.objects.count(), 4)
        self.assertEqual(result.transactions[0].entries.count(), 2)

    def test_partial_subscription_payment_creates_nothing_at_all(self):
        before = (Member.objects.count(), IdSequence.objects.count())
        with self.assertRaises(RuleViolation) as ctx:
            register(self.owner, subscription_entries=[Entry("cash", 79999)])
        self.assertEqual(ctx.exception.code, "underpaid")
        self.assertEqual((Member.objects.count(), IdSequence.objects.count()), before)
        for model in (MemberSubscription, FinancialTransaction, ReceiptDocument, PaymentEntry):
            self.assertEqual(model.objects.count(), 0, model.__name__)
        self.assertEqual(register(self.owner).member.membership_no, "COV-M-000001")  # no ID gap

    def test_missing_token_payment_blocks_activation(self):
        for entries in ([], [Entry("cash", 49999)]):
            with self.assertRaises(RuleViolation):
                register(self.owner, token_entries=entries)
        self.assertEqual(Member.objects.count(), 0)
        self.assertEqual(MemberSubscription.objects.count(), 0)

    def test_duplicate_mobile_number_rolls_everything_back(self):
        register(self.owner)
        before = (
            FinancialTransaction.objects.count(),
            IdSequence.objects.get(name="member").last_value,
        )
        with self.assertRaises(RuleViolation) as ctx:
            register(self.owner, name="Other", number="98765 43210")
        self.assertEqual(ctx.exception.code, "mobile_duplicate")
        self.assertEqual(FinancialTransaction.objects.count(), before[0])
        self.assertEqual(IdSequence.objects.get(name="member").last_value, before[1])

    def test_failure_after_payment_leaves_no_partial_registration(self):
        with mock.patch.object(ms, "_create_subscription", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                register(self.owner)
        for model in (
            Member,
            MemberSubscription,
            FinancialTransaction,
            ReceiptDocument,
            PaymentEntry,
            TokenLedgerEntry,
        ):
            self.assertEqual(model.objects.count(), 0, model.__name__)
        self.assertFalse(IdSequence.objects.filter(last_value__gt=0).exists())
        self.assertEqual(AuditEvent.objects.filter(action__startswith="membership").count(), 0)

    def test_failure_while_recording_the_token_rolls_back_the_subscription_payment(self):
        with mock.patch.object(tokens, "record_entry", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                register(self.owner)
        self.assertEqual(FinancialTransaction.objects.count(), 0)
        self.assertEqual(Member.objects.count(), 0)

    def test_unknown_or_inactive_plan_is_rejected(self):
        with self.assertRaises(RuleViolation) as ctx:
            register(self.owner, plan="6m", **{})  # sanity: valid
            ms.register_member(
                actor=self.owner,
                member_fields=fields("B", "9876543299"),
                plan_code="nope",
                subscription_entries=[],
                token_entries=[],
                today=TODAY,
            )
        self.assertEqual(ctx.exception.code, "plan_unknown")

    def test_backdated_activation_needs_reason_and_confirmation(self):
        past = date(2026, 11, 1)
        for kwargs, code in (
            ({}, "backdate_reason_required"),
            ({"backdate_reason": "joined last week"}, "backdate_confirmation_required"),
        ):
            with self.assertRaises(RuleViolation) as ctx:
                register(self.owner, activation_date=past, **kwargs)
            self.assertEqual(ctx.exception.code, code)
        self.assertEqual(Member.objects.count(), 0)
        result = register(
            self.owner,
            activation_date=past,
            payment_date=past,
            backdate_reason="joined last week",
            backdate_confirmed=True,
        )
        self.assertEqual(result.subscription.starts_on, past)
        self.assertTrue(all(t.backdated for t in result.transactions))
        self.assertEqual(result.transactions[0].receipt_year, 2026)  # issuance year

    def test_future_activation_is_scheduled_and_not_yet_borrowing(self):
        result = register(self.owner, activation_date=date(2026, 12, 1))
        self.assertEqual(policy.state(result.subscription.period, TODAY), policy.SCHEDULED)
        codes = [c for c, _ in ms.borrowing_blockers(result.member, TODAY)]
        self.assertEqual(codes, ["no_valid_subscription"])

    def test_registration_is_audited_without_payment_details(self):
        register(self.owner)
        actions = set(AuditEvent.objects.values_list("action", flat=True))
        self.assertTrue({"membership.registered", "token.collected", "member.create"} <= actions)

    def test_phone_must_still_be_valid(self):
        with self.assertRaises(RuleViolation):
            register(self.owner, number="123")
        self.assertEqual(FinancialTransaction.objects.count(), 0)


class RenewalTests(TestCase):
    def setUp(self):
        self.owner = make_owner()
        self.member = register(self.owner).member

    def test_early_renewal_starts_on_the_first_expired_day(self):
        result = renew(self.owner, self.member, today=date(2027, 1, 15))
        sub = result.subscription
        self.assertEqual((sub.starts_on, sub.expires_on), (date(2027, 5, 11), date(2027, 11, 11)))
        self.assertEqual(result.transactions[0].receipt_no, "SUB-2027-000001")  # issuance year
        self.assertEqual(result.document.kind, "subscription")

    def test_renewal_on_the_last_valid_day_is_still_early(self):
        sub = renew(self.owner, self.member, today=date(2027, 5, 10)).subscription
        self.assertEqual(sub.starts_on, date(2027, 5, 11))

    def test_late_renewal_uses_the_owner_entered_activation_date(self):
        today = date(2027, 6, 20)
        with self.assertRaises(RuleViolation) as ctx:
            renew(self.owner, self.member, today=today)
        self.assertEqual(ctx.exception.code, "activation_date_required")
        sub = renew(self.owner, self.member, today=today, activation_date=today).subscription
        self.assertEqual((sub.starts_on, sub.expires_on), (today, date(2027, 12, 20)))

    def test_late_renewal_may_be_backdated_to_the_expiry_with_a_reason(self):
        today = date(2027, 6, 20)
        kwargs = dict(activation_date=date(2027, 5, 11))
        with self.assertRaises(RuleViolation) as ctx:
            renew(self.owner, self.member, today=today, **kwargs)
        self.assertEqual(ctx.exception.code, "backdate_reason_required")
        sub = renew(
            self.owner,
            self.member,
            today=today,
            backdate_reason="forgot to record",
            backdate_confirmed=True,
            payment_date=today,
            **kwargs,
        ).subscription
        self.assertEqual(sub.starts_on, date(2027, 5, 11))

    def test_overlapping_activation_dates_are_refused(self):
        with self.assertRaises(RuleViolation) as ctx:
            renew(
                self.owner,
                self.member,
                today=date(2027, 6, 20),
                activation_date=date(2027, 5, 1),
                backdate_reason="x",
                backdate_confirmed=True,
            )
        self.assertEqual(ctx.exception.code, "activation_overlap")

    def test_at_most_one_scheduled_subscription(self):
        renew(self.owner, self.member, today=date(2027, 1, 1))
        with self.assertRaises(RuleViolation) as ctx:
            renew(self.owner, self.member, today=date(2027, 1, 2))
        self.assertEqual(ctx.exception.code, "scheduled_exists")
        self.assertEqual(self.member.subscriptions.count(), 2)

    def test_chain_of_renewals_never_overlaps(self):
        renew(self.owner, self.member, today=date(2027, 1, 1))
        # once the scheduled period is current it may be renewed again
        third = renew(self.owner, self.member, today=date(2027, 6, 1), plan="12m").subscription
        self.assertEqual(
            (third.starts_on, third.expires_on), (date(2027, 11, 11), date(2028, 11, 11))
        )
        subs = list(self.member.subscriptions.all())
        for a, b in zip(subs, subs[1:], strict=False):
            self.assertEqual(a.expires_on, b.starts_on)

    def test_partial_renewal_payment_activates_nothing(self):
        before = (
            FinancialTransaction.objects.count(),
            IdSequence.objects.get(name="receipt_subscription").last_value,
        )
        with self.assertRaises(RuleViolation) as ctx:
            renew(self.owner, self.member, today=date(2027, 1, 1), entries=[Entry("cash", 79999)])
        self.assertEqual(ctx.exception.code, "underpaid")
        self.assertEqual(self.member.subscriptions.count(), 1)
        self.assertEqual(FinancialTransaction.objects.count(), before[0])
        self.assertEqual(IdSequence.objects.get(name="receipt_subscription").last_value, before[1])

    def test_token_carries_forward_and_is_not_collected_again(self):
        result = renew(self.owner, self.member, today=date(2027, 1, 1))
        cycle = tokens.latest_cycle(self.member)
        self.assertEqual(cycle.cycle_no, 1)
        self.assertEqual(tokens.balance_paise(cycle), 50000)
        kinds = list(cycle.entries.values_list("entry_type", "delta_paise"))
        self.assertEqual(kinds, [("collection", 50000), ("carry_forward", 0)])
        self.assertEqual([t.kind for t in result.transactions], ["subscription_payment"])
        self.assertEqual(FinancialTransaction.objects.filter(kind="token_collection").count(), 1)
        self.assertEqual(result.subscription.token_cycle_id, cycle.pk)

    def test_renewal_failure_leaves_no_partial_record(self):
        before = FinancialTransaction.objects.count()
        with mock.patch.object(tokens, "record_entry", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                renew(self.owner, self.member, today=date(2027, 1, 1))
        self.assertEqual(FinancialTransaction.objects.count(), before)
        self.assertEqual(self.member.subscriptions.count(), 1)
        self.assertEqual(IdSequence.objects.get(name="receipt_subscription").last_value, 1)

    def test_renewal_clears_the_new_subscription_required_flag(self):
        Member.objects.filter(pk=self.member.pk).update(needs_new_subscription=True)
        self.member.refresh_from_db()
        renew(self.owner, self.member, today=date(2027, 1, 1))
        self.member.refresh_from_db()
        self.assertFalse(self.member.needs_new_subscription)

    def test_anonymised_member_cannot_renew(self):
        Member.objects.filter(pk=self.member.pk).update(anonymised_at="2027-01-01T00:00:00Z")
        self.member.refresh_from_db()
        with self.assertRaises(RuleViolation) as ctx:
            renew(self.owner, self.member, today=date(2027, 1, 1))
        self.assertEqual(ctx.exception.code, "anonymised")


class ForfeitedTokenTests(TestCase):
    """B15/B16/Q1/N2: renewal and borrowing stop; the subscription stays valid; a fresh token
    starts a new cycle for the same member and restores both."""

    def setUp(self):
        self.owner = make_owner()
        self.member = register(self.owner).member
        self.day = date(2027, 2, 1)
        forfeit(self.owner, self.member, today=self.day)

    def test_subscription_stays_valid_but_renewal_and_borrowing_are_blocked(self):
        self.assertEqual(tokens.token_state(self.member)[1], tokens.FORFEITED)
        self.assertIsNotNone(ms.current_subscription(self.member, self.day))
        self.assertEqual(
            [c for c, _ in ms.borrowing_blockers(self.member, self.day)], ["token_forfeited"]
        )
        with self.assertRaises(RuleViolation) as ctx:
            renew(self.owner, self.member, today=self.day)
        self.assertEqual(ctx.exception.code, "token_forfeited")

    def test_fresh_token_starts_a_new_cycle_and_restores_borrowing_and_renewal(self):
        subs_before = list(self.member.subscriptions.values_list("starts_on", "expires_on"))
        result = ms.pay_fresh_token(
            actor=self.owner, member=self.member, entries=[Entry("cash", 50000)], today=self.day
        )
        cycle, state = tokens.token_state(self.member)
        self.assertEqual((cycle.cycle_no, state), (2, tokens.HELD))
        self.assertEqual(result.transactions[0].receipt_no, "TOK-2027-000001")
        self.assertEqual(ms.borrowing_blockers(self.member, self.day), [])
        self.assertEqual(
            list(self.member.subscriptions.values_list("starts_on", "expires_on")), subs_before
        )
        self.assertEqual(self.member.membership_no, "COV-M-000001")
        renewal = renew(self.owner, self.member, today=self.day)
        self.assertEqual(renewal.subscription.token_cycle.cycle_no, 2)
        self.assertEqual(renewal.subscription.starts_on, date(2027, 5, 11))  # no overlap, no reset

    def test_the_old_forfeiture_is_never_restored(self):
        ms.pay_fresh_token(
            actor=self.owner, member=self.member, entries=[Entry("cash", 50000)], today=self.day
        )
        old = self.member.token_account.cycles.get(cycle_no=1)
        self.assertEqual(tokens.cycle_state(old), tokens.FORFEITED)
        self.assertEqual(tokens.balance_paise(old), 0)

    def test_partial_fresh_token_payment_changes_nothing(self):
        with self.assertRaises(RuleViolation):
            ms.pay_fresh_token(
                actor=self.owner, member=self.member, entries=[Entry("cash", 49999)], today=self.day
            )
        self.assertEqual(self.member.token_account.cycles.count(), 1)
        self.assertEqual(tokens.token_state(self.member)[1], tokens.FORFEITED)
        self.assertEqual(ms.borrowing_blockers(self.member, self.day)[0][0], "token_forfeited")

    def test_a_held_token_cannot_be_collected_twice(self):
        ms.pay_fresh_token(
            actor=self.owner, member=self.member, entries=[Entry("cash", 50000)], today=self.day
        )
        with self.assertRaises(RuleViolation) as ctx:
            ms.pay_fresh_token(
                actor=self.owner, member=self.member, entries=[Entry("cash", 50000)], today=self.day
            )
        self.assertEqual(ctx.exception.code, "token_already_held")
        self.assertEqual(self.member.token_account.cycles.count(), 2)

    def test_member_without_any_token_can_pay_the_first_one(self):
        other = Member.objects.create(
            membership_no="COV-M-000900",
            full_name="X",
            mobile_e164="+919000000001",
            joined_on=self.day,
        )
        self.assertEqual(tokens.token_state(other), (None, tokens.NONE))
        ms.pay_fresh_token(
            actor=self.owner, member=other, entries=[Entry("cash", 50000)], today=self.day
        )
        self.assertEqual(tokens.token_state(other)[1], tokens.HELD)
        with transaction.atomic():
            self.assertEqual(
                [c for c, _ in ms.borrowing_blockers(other, self.day)], ["no_valid_subscription"]
            )
