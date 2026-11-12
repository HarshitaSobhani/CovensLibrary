"""Phase 4b: Poor / Damaged / Lost decisions, and loan renewals."""

from datetime import timedelta

from django.test import TestCase

from audit.models import AuditEvent
from catalogue import services as catalogue
from catalogue.models import BookCopy, CopyEvent
from circulation import services
from circulation.models import Loan, LoanRenewal
from core.errors import RuleViolation
from core.models import DomainEvent
from finance import tokens
from finance.models import FinancialTransaction
from finance.services import Entry
from memberships import services as ms

from .helpers import D, make_copies, make_owner, overdue_loan

NOW = D(2026, 12, 10)  # inside the loan period (due 31 Dec)


class OutcomeBase(TestCase):
    def setUp(self):
        self.owner = make_owner()
        self.member, self.copy, self.loan = overdue_loan(self.owner)

    def fresh(self):
        return BookCopy.objects.get(pk=self.copy.pk)

    def token(self):
        return tokens.token_state(self.member)[1]

    def refused(self, fn, code, **kw):
        with self.assertRaises(RuleViolation) as ctx:
            fn(**kw)
        self.assertEqual(ctx.exception.code, code)
        self.assertEqual(Loan.objects.get(pk=self.loan.pk).status, "active")
        self.assertEqual(self.token(), tokens.HELD)


class PoorAndDamagedTests(OutcomeBase):
    def ret(self, condition, **kw):
        kw.setdefault("owner_confirmed", True)
        kw.setdefault("reason", "cover torn off")
        return services.return_loan(
            actor=self.owner, loan=self.loan, condition=condition, today=NOW, **kw
        )

    def test_poor_means_condition_poor_status_withdrawn_and_a_confirmed_forfeiture(self):
        loan = self.ret("poor")
        copy = self.fresh()
        self.assertEqual((copy.condition, copy.status), ("poor", "withdrawn"))
        self.assertFalse(copy.is_issuable)
        self.assertEqual((loan.status, loan.return_condition), ("returned", "poor"))
        self.assertEqual(self.token(), tokens.FORFEITED)
        txn = FinancialTransaction.objects.get(kind="token_forfeiture")
        entry = txn.token_entries.get()
        # manual decisions keep the authenticated owner, the reason and the confirmation
        self.assertEqual((txn.recorded_by_id, txn.recorded_by_system), (self.owner.pk, ""))
        self.assertEqual(
            (entry.forfeiture_trigger, entry.owner_confirmed, entry.reason),
            ("poor", True, "cover torn off"),
        )
        self.assertEqual(loan.forfeiture_entry_id, entry.pk)
        self.assertEqual(txn.effective_date, NOW)

    def test_damaged_means_condition_damaged_status_damaged(self):
        self.ret("damaged")
        copy = self.fresh()
        self.assertEqual((copy.condition, copy.status), ("damaged", "damaged"))
        self.assertEqual(self.token(), tokens.FORFEITED)
        self.assertEqual(
            FinancialTransaction.objects.get(kind="token_forfeiture")
            .token_entries.get()
            .forfeiture_trigger,
            "damaged",
        )

    def test_poor_damaged_lost_and_withdrawn_stay_distinct(self):
        states = {}
        for outcome in ("poor", "damaged"):
            member, copy, loan = overdue_loan(
                self.owner, name=outcome, number=f"98111000{len(states)}1"
            )
            services.return_loan(
                actor=self.owner,
                loan=loan,
                condition=outcome,
                owner_confirmed=True,
                reason="r",
                today=NOW,
            )
            c = BookCopy.objects.get(pk=copy.pk)
            states[outcome] = (c.condition, c.status)
        member, copy, loan = overdue_loan(self.owner, name="lost", number="9811100099")
        services.mark_lost(actor=self.owner, loan=loan, owner_confirmed=True, reason="r", today=NOW)
        c = BookCopy.objects.get(pk=copy.pk)
        states["lost"] = (c.condition, c.status)
        withdrawn = make_copies(self.owner, 1, "W")[0]
        catalogue.set_copy_state(withdrawn, condition="good", status="withdrawn", actor=self.owner)
        c = BookCopy.objects.get(pk=withdrawn.pk)
        states["withdrawn"] = (c.condition, c.status)
        self.assertEqual(
            states,
            {
                "poor": ("poor", "withdrawn"),
                "damaged": ("damaged", "damaged"),
                "lost": ("lost", "lost"),
                "withdrawn": ("good", "withdrawn"),
            },
        )

    def test_the_owner_confirmation_and_a_mandatory_reason_are_required(self):
        for condition in ("poor", "damaged"):
            self.refused(
                services.return_loan,
                "confirmation_required",
                actor=self.owner,
                loan=self.loan,
                condition=condition,
                today=NOW,
            )
            self.refused(
                services.return_loan,
                "reason_required",
                actor=self.owner,
                loan=self.loan,
                condition=condition,
                owner_confirmed=True,
                reason="  ",
                today=NOW,
            )
            self.refused(
                services.return_loan,
                "reason_too_long",
                actor=self.owner,
                loan=self.loan,
                condition=condition,
                owner_confirmed=True,
                reason="x" * 2001,
                today=NOW,
            )
        self.assertEqual(self.fresh().status, "borrowed")  # nothing changed

    def test_history_and_audit_record_the_decision(self):
        self.ret("poor")
        types = list(CopyEvent.objects.filter(copy=self.copy).values_list("event_type", flat=True))
        self.assertEqual(types, ["acquired", "issued", "returned", "condition_changed"])
        audit = AuditEvent.objects.get(action="loan.returned")
        self.assertEqual(
            (audit.summary["outcome"], audit.summary["reason"]), ("poor", "cover torn off")
        )
        self.assertIs(audit.summary["confirmed_forfeiture"], True)
        self.assertEqual(audit.actor_id, self.owner.pk)
        self.assertTrue(
            AuditEvent.objects.filter(action="token.forfeited", actor=self.owner).exists()
        )

    def test_a_late_damaged_return_is_forfeited_once_automatically_and_not_again(self):
        services.return_loan(
            actor=self.owner,
            loan=self.loan,
            condition="damaged",
            owner_confirmed=True,
            reason="water damage",
            today=D(2027, 1, 20),
        )
        txns = FinancialTransaction.objects.filter(kind="token_forfeiture")
        self.assertEqual(txns.count(), 1)  # the day-8 forfeiture came first
        self.assertEqual(txns.get().recorded_by_system, "overdue_forfeiture_job")
        audit = AuditEvent.objects.get(action="loan.returned")
        self.assertEqual(
            (audit.summary["automatic_forfeiture"], audit.summary["confirmed_forfeiture"]),
            (True, False),
        )
        self.assertEqual(
            self.fresh().condition, "damaged"
        )  # the owner's classification still applies

    def test_a_backdated_decision_needs_a_reason_and_extra_confirmation(self):
        self.refused(
            services.return_loan,
            "backdate_reason_required",
            actor=self.owner,
            loan=self.loan,
            condition="poor",
            owner_confirmed=True,
            reason="r",
            returned_on=D(2026, 12, 5),
            today=NOW,
        )
        loan = self.ret(
            "poor",
            returned_on=D(2026, 12, 5),
            backdate_reason="entered late",
            backdate_confirmed=True,
        )
        self.assertEqual(loan.returned_on, D(2026, 12, 5))
        self.assertEqual(
            FinancialTransaction.objects.get(kind="token_forfeiture").effective_date, D(2026, 12, 5)
        )

    def test_a_borrowed_copy_cannot_be_reclassified_behind_the_loan(self):
        with self.assertRaises(RuleViolation) as ctx:
            catalogue.set_copy_state(
                self.copy, condition="poor", status="withdrawn", actor=self.owner
            )
        self.assertEqual(ctx.exception.code, "copy_borrowed")


class LostTests(OutcomeBase):
    def lost(self, **kw):
        kw.setdefault("owner_confirmed", True)
        kw.setdefault("reason", "member says it was stolen")
        return services.mark_lost(actor=self.owner, loan=self.loan, today=NOW, **kw)

    def test_a_lost_book_closes_the_loan_as_lost_and_forfeits_the_token(self):
        loan = self.lost()
        copy = self.fresh()
        self.assertEqual(
            (loan.status, loan.return_condition, loan.returned_on), ("lost", "lost", NOW)
        )
        self.assertEqual((copy.condition, copy.status), ("lost", "lost"))
        self.assertEqual(self.token(), tokens.FORFEITED)
        entry = FinancialTransaction.objects.get(kind="token_forfeiture").token_entries.get()
        self.assertEqual((entry.forfeiture_trigger, entry.owner_confirmed), ("lost", True))
        self.assertEqual(
            FinancialTransaction.objects.count(), 3
        )  # sub, token, forfeiture: no replacement charge
        self.assertEqual(AuditEvent.objects.get(action="loan.lost").actor_id, self.owner.pk)

    def test_confirmation_and_reason_are_mandatory(self):
        self.refused(
            services.mark_lost, "confirmation_required", actor=self.owner, loan=self.loan, today=NOW
        )
        self.refused(
            services.mark_lost,
            "reason_required",
            actor=self.owner,
            loan=self.loan,
            owner_confirmed=True,
            today=NOW,
        )

    def test_a_recovered_lost_book_never_reverses_the_forfeiture_automatically(self):
        self.lost()
        catalogue.set_copy_state(
            self.copy,
            condition="good",
            status="available",
            actor=self.owner,
            note="found behind the shelf",
        )
        self.assertEqual(self.token(), tokens.FORFEITED)  # B12: only an owner-approved adjustment
        self.assertEqual(Loan.objects.get(pk=self.loan.pk).status, "lost")
        self.assertEqual(FinancialTransaction.objects.filter(kind="reversal").count(), 0)

    def test_the_member_can_borrow_again_only_after_a_fresh_token(self):
        self.lost()
        other = make_copies(self.owner, 1, "Next")[0]
        with self.assertRaises(RuleViolation) as ctx:
            services.issue_loan(actor=self.owner, member=self.member, copy=other, today=NOW)
        self.assertEqual(ctx.exception.code, "token_forfeited")
        ms.pay_fresh_token(
            actor=self.owner, member=self.member, entries=[Entry("cash", 50000)], today=NOW
        )
        self.assertEqual(
            services.issue_loan(actor=self.owner, member=self.member, copy=other, today=NOW).status,
            "active",
        )

    def test_an_already_forfeited_token_is_not_forfeited_again_by_a_loss(self):
        from circulation import forfeiture

        forfeiture.process_overdue_forfeitures(today=D(2027, 1, 8))
        services.mark_lost(
            actor=self.owner,
            loan=self.loan,
            owner_confirmed=True,
            reason="gone",
            today=D(2027, 1, 9),
        )
        self.assertEqual(FinancialTransaction.objects.filter(kind="token_forfeiture").count(), 1)
        self.assertEqual(Loan.objects.get(pk=self.loan.pk).status, "lost")

    def test_a_closed_loan_cannot_be_lost_again(self):
        self.lost()
        with self.assertRaises(RuleViolation) as ctx:
            self.lost()
        self.assertEqual(ctx.exception.code, "loan_closed")


class RenewalTests(OutcomeBase):
    def renew(self, day, **kw):
        return services.renew_loan(actor=self.owner, loan=self.loan, today=day, **kw)

    def test_a_renewal_adds_30_days_to_the_existing_due_date(self):
        renewal = self.renew(D(2026, 12, 20), notes="wants to finish it")
        self.assertEqual(
            (renewal.old_due_on, renewal.new_due_on), (D(2026, 12, 31), D(2027, 1, 30))
        )
        self.assertEqual(Loan.objects.get(pk=self.loan.pk).due_on, D(2027, 1, 30))
        self.assertEqual(
            (renewal.renewed_on, renewal.notes, renewal.renewed_by_id),
            (D(2026, 12, 20), "wants to finish it", self.owner.pk),
        )
        audit = AuditEvent.objects.get(action="loan.renewed")
        self.assertEqual(
            (audit.summary["old_due_on"], audit.summary["new_due_on"]), ("2026-12-31", "2027-01-30")
        )

    def test_renewals_are_unlimited(self):
        day = D(2026, 12, 20)
        for _ in range(3):
            day = self.renew(day).new_due_on - timedelta(days=5)
        self.assertEqual(LoanRenewal.objects.filter(loan=self.loan).count(), 3)
        self.assertEqual(Loan.objects.get(pk=self.loan.pk).due_on, D(2027, 3, 31))

    def test_renewal_on_the_due_date_is_allowed_but_never_after_it(self):
        self.assertEqual(self.renew(D(2026, 12, 31)).new_due_on, D(2027, 1, 30))
        member, _, loan = overdue_loan(self.owner, name="Bo", number="9811100011")
        for day in (D(2027, 1, 1), D(2027, 1, 5), D(2027, 1, 7)):  # inside the grace period
            with self.subTest(day=day), self.assertRaises(RuleViolation) as ctx:
                services.renew_loan(actor=self.owner, loan=loan, today=day)
            self.assertEqual(ctx.exception.code, "renewal_after_due_date")

    def test_a_renewal_may_not_pass_the_last_valid_day_and_is_never_extended_silently(self):
        setting_member, _, loan = overdue_loan(
            self.owner, issued_on=D(2027, 3, 1), name="Cy", number="9811100022"
        )
        loan_due = Loan.objects.get(pk=loan.pk).due_on  # 31 Mar
        self.assertEqual(loan_due, D(2027, 3, 31))
        first = services.renew_loan(actor=self.owner, loan=loan, today=D(2027, 3, 31))  # to 30 Apr
        self.assertEqual(first.new_due_on, D(2027, 4, 30))
        with self.assertRaises(RuleViolation) as ctx:  # 30 Apr + 30 = 30 May > 10 May
            services.renew_loan(actor=self.owner, loan=loan, today=D(2027, 4, 30))
        self.assertEqual(ctx.exception.code, "due_after_last_valid_day")
        self.assertIn("Renew the membership first", ctx.exception.message)
        self.assertEqual(Loan.objects.get(pk=loan.pk).due_on, D(2027, 4, 30))
        # the owner renews the membership first; then the renewal fits
        ms.renew_subscription(
            actor=self.owner,
            member=setting_member,
            plan_code="6m",
            entries=[Entry("cash", 80000)],
            today=D(2027, 4, 30),
        )
        self.assertEqual(
            services.renew_loan(actor=self.owner, loan=loan, today=D(2027, 4, 30)).new_due_on,
            D(2027, 5, 30),
        )

    def test_a_renewal_needs_a_valid_subscription_on_the_day(self):
        with self.assertRaises(RuleViolation) as ctx:
            self.renew(D(2027, 5, 11))  # first expired day: also after the due date
        self.assertIn(ctx.exception.code, {"renewal_after_due_date", "no_valid_subscription"})

    def test_a_forfeited_token_does_not_stop_renewing_a_book_already_out(self):
        from finance import corrections

        corrections.forfeit_token(
            actor=self.owner,
            member=self.member,
            trigger="lost",
            owner_confirmed=True,
            reason="other book",
            today=D(2026, 12, 15),
        )
        self.assertEqual(self.renew(D(2026, 12, 20)).new_due_on, D(2027, 1, 30))  # B16

    def test_closed_loans_cannot_be_renewed(self):
        services.return_loan(actor=self.owner, loan=self.loan, today=D(2026, 12, 20))
        with self.assertRaises(RuleViolation) as ctx:
            self.renew(D(2026, 12, 21))
        self.assertEqual(ctx.exception.code, "loan_closed")

    def test_the_event_is_stored_in_the_same_transaction(self):
        self.renew(D(2026, 12, 20))
        keys = list(
            DomainEvent.objects.filter(event_type="loan.renewed").values_list(
                "dedupe_key", flat=True
            )
        )
        self.assertEqual(keys, [f"loan:{self.loan.pk}:renewed:2027-01-30"])
