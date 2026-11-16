"""Phase 4b: grace period, automatic day-8 token forfeiture, catch-up, system attribution."""

from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from audit.models import AuditEvent
from circulation import forfeiture, services
from circulation.models import Loan
from core.errors import RuleViolation
from core.models import DomainEvent, LibrarySetting
from finance import corrections as fc
from finance import tokens
from finance.models import SYSTEM_OVERDUE_FORFEITURE, FinancialTransaction
from finance.services import Entry
from memberships import policy
from memberships import services as ms

from .helpers import D, make_owner, overdue_loan

DUE = D(2026, 12, 31)
GRACE_END = D(2027, 1, 7)
DAY8 = D(2027, 1, 8)


class Base(TestCase):
    def setUp(self):
        self.owner = make_owner()
        self.member, self.copy, self.loan = overdue_loan(self.owner)

    def job(self, day):
        return forfeiture.process_overdue_forfeitures(today=day)

    def state(self):
        return tokens.token_state(self.member)[1]

    def reload(self):
        return Loan.objects.get(pk=self.loan.pk)


class DayEightTests(Base):
    def test_the_dates_follow_the_approved_example(self):
        # due 31 Dec, 7 grace days -> grace 1-7 Jan -> day 8 = 8 Jan
        self.assertEqual(policy.grace_end(DUE, 7), GRACE_END)
        self.assertEqual(policy.forfeiture_day(DUE, 7), DAY8)

    def test_nothing_is_forfeited_through_the_end_of_the_grace_period(self):
        for day in (DUE, D(2027, 1, 1), D(2027, 1, 4), GRACE_END):
            with self.subTest(day=day):
                self.assertEqual(self.job(day)["forfeited"], 0)
        self.assertEqual(self.state(), tokens.HELD)
        self.assertIsNone(self.reload().forfeiture_entry_id)

    def test_on_day_8_the_token_is_forfeited_automatically_by_the_system(self):
        result = self.job(DAY8)
        self.assertEqual((result["forfeited"], result["late"]), (1, 0))
        txn = FinancialTransaction.objects.get(kind="token_forfeiture")
        self.assertEqual((txn.amount_paise, txn.cash_flow, txn.receipt_no), (50000, "none", ""))
        self.assertEqual(txn.effective_date, DAY8)
        # D-15: system attribution, never a user
        self.assertIsNone(txn.recorded_by_id)
        self.assertEqual(txn.recorded_by_system, SYSTEM_OVERDUE_FORFEITURE)
        entry = txn.token_entries.get()
        self.assertEqual((entry.forfeiture_trigger, entry.owner_confirmed), ("overdue_auto", False))
        self.assertEqual(self.reload().forfeiture_entry_id, entry.pk)
        self.assertEqual(self.state(), tokens.FORFEITED)
        # the existing subscription stays valid; renewal and new borrowing are blocked
        self.assertIsNotNone(ms.current_subscription(self.member, DAY8))
        self.assertEqual(
            [c for c, _ in ms.borrowing_blockers(self.member, DAY8)], ["token_forfeited"]
        )
        audit = AuditEvent.objects.get(action="loan.token_forfeited")
        self.assertIsNone(audit.actor_id)
        self.assertEqual(audit.summary["system"], SYSTEM_OVERDUE_FORFEITURE)

    def test_the_job_is_idempotent(self):
        self.job(DAY8)
        again = self.job(D(2027, 1, 9))
        self.assertEqual(again["forfeited"], 0)
        self.assertEqual(FinancialTransaction.objects.filter(kind="token_forfeiture").count(), 1)

    def test_missed_processing_keeps_the_original_effective_date(self):
        result = self.job(D(2027, 2, 20))  # the Dell was off for six weeks
        self.assertEqual((result["forfeited"], result["late"]), (1, 1))
        txn = FinancialTransaction.objects.get(kind="token_forfeiture")
        self.assertEqual(txn.effective_date, DAY8)  # not the day it was processed
        self.assertLess((timezone.now() - txn.processed_at).total_seconds(), 60)  # real time
        self.assertIs(
            AuditEvent.objects.get(action="loan.token_forfeited").summary["processed_late"], True
        )

    def test_a_return_on_day_8_does_not_prevent_forfeiture(self):
        services.return_loan(actor=self.owner, loan=self.loan, today=DAY8)
        loan = self.reload()
        self.assertEqual(loan.status, "returned")
        self.assertEqual(self.state(), tokens.FORFEITED)
        self.assertEqual(loan.forfeiture_entry.effective_date, DAY8)

    def test_a_return_on_the_last_grace_day_does_not_forfeit(self):
        services.return_loan(actor=self.owner, loan=self.loan, today=GRACE_END)
        self.assertEqual(self.state(), tokens.HELD)
        self.assertIsNone(self.reload().forfeiture_entry_id)

    def test_a_late_return_is_forfeited_with_the_original_effective_date(self):
        services.return_loan(actor=self.owner, loan=self.loan, today=D(2027, 1, 20))
        self.assertEqual(self.reload().forfeiture_entry.effective_date, DAY8)
        txn = FinancialTransaction.objects.get(kind="token_forfeiture")
        self.assertEqual(txn.recorded_by_system, SYSTEM_OVERDUE_FORFEITURE)

    def test_a_backdated_return_before_day_8_avoids_forfeiture_only_with_extra_confirmation(self):
        today = D(2027, 1, 20)
        with self.assertRaises(Exception) as ctx:
            services.return_loan(
                actor=self.owner, loan=self.loan, returned_on=D(2027, 1, 5), today=today
            )
        self.assertEqual(ctx.exception.code, "backdate_reason_required")
        services.return_loan(
            actor=self.owner,
            loan=self.loan,
            returned_on=D(2027, 1, 5),
            today=today,
            backdate_reason="returned on the 5th, entered late",
            backdate_confirmed=True,
        )
        self.assertEqual(self.state(), tokens.HELD)

    def test_the_grace_period_setting_drives_day_8(self):
        setting = LibrarySetting.load()
        setting.grace_days = 3
        setting.save()
        self.assertEqual(self.job(D(2027, 1, 3))["forfeited"], 0)  # due + 3
        self.assertEqual(self.job(D(2027, 1, 4))["forfeited"], 1)  # due + 3 + 1
        self.assertEqual(
            FinancialTransaction.objects.get(kind="token_forfeiture").effective_date, D(2027, 1, 4)
        )

    def test_a_renewal_moves_the_forfeiture_day(self):
        renewal = services.renew_loan(actor=self.owner, loan=self.loan, today=DUE)
        self.assertEqual(renewal.new_due_on, D(2027, 1, 30))
        self.assertEqual(self.job(DAY8)["forfeited"], 0)
        self.assertEqual(self.job(D(2027, 2, 8))["forfeited"], 1)  # 30 Jan + 7 + 1

    def test_no_held_token_means_nothing_to_forfeit(self):
        # O-07: a token collection cannot be reversed while a book is out, so the "no token
        # held" state is reached another way: the token is already forfeited (owner-confirmed)
        # while the loan is still active. The day-8 job must then skip the loan.
        fc.forfeit_token(
            actor=self.owner,
            member=self.member,
            trigger="lost",
            owner_confirmed=True,
            reason="test: token already forfeited",
            today=D(2026, 12, 2),
        )
        with self.assertRaises(RuleViolation) as ctx:
            fc.reverse_transaction(
                actor=self.owner,
                transaction_id=FinancialTransaction.objects.get(kind="token_collection").pk,
                reason="mistake",
                entries=[Entry("cash", 50000)],
                today=D(2026, 12, 2),
            )
        self.assertEqual(ctx.exception.code, "token_in_use")  # already forfeited: blocked first
        result = self.job(DAY8)
        self.assertEqual((result["forfeited"], result["skipped_token"]), (0, 1))
        self.assertEqual(FinancialTransaction.objects.filter(kind="token_forfeiture").count(), 1)

    def test_a_loan_already_forfeited_manually_is_not_forfeited_twice(self):
        services.mark_lost(
            actor=self.owner,
            loan=self.loan,
            owner_confirmed=True,
            reason="lost",
            today=D(2027, 1, 2),
        )
        self.assertEqual(self.job(DAY8)["checked"], 0)  # a closed loan is out of scope
        self.assertEqual(FinancialTransaction.objects.filter(kind="token_forfeiture").count(), 1)


class EventsAndCommandTests(Base):
    def test_the_forfeiture_event_is_stored_with_the_business_data_and_a_stable_key(self):
        self.job(DAY8)  # no commit callbacks needed: the event is a row in the same transaction
        txn = FinancialTransaction.objects.get(kind="token_forfeiture")
        stored = list(DomainEvent.objects.filter(event_type="token.forfeited"))
        self.assertEqual(
            [e.dedupe_key for e in stored], [f"token-forfeiture:{txn.internal_txn_id}"]
        )
        self.assertEqual(stored[0].payload["trigger"], "overdue_auto")
        self.assertIs(stored[0].payload["system"], True)
        self.assertEqual(stored[0].event_date, DAY8)  # business date; occurred_at is the real time
        self.job(D(2027, 1, 9))  # idempotent job: still one event
        self.assertEqual(DomainEvent.objects.filter(event_type="token.forfeited").count(), 1)

    def test_the_management_command_runs_the_job_for_today(self):
        out = StringIO()
        with mock.patch("circulation.forfeiture.timezone.localdate", return_value=D(2027, 2, 20)):
            call_command("run_daily_jobs", stdout=out)
        self.assertIn("forfeited 1 (1 processed late)", out.getvalue())
        self.assertEqual(
            FinancialTransaction.objects.get(kind="token_forfeiture").effective_date, DAY8
        )
        again = StringIO()
        with mock.patch("circulation.forfeiture.timezone.localdate", return_value=D(2027, 2, 21)):
            call_command("run_daily_jobs", stdout=again)
        self.assertIn("forfeited 0", again.getvalue())

    def test_the_command_is_registered_for_the_circulation_app(self):
        from django.core.management import get_commands

        self.assertEqual(get_commands()["run_daily_jobs"], "circulation")
