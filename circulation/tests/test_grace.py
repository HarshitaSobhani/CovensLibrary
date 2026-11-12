"""Phase 4b: grace-period changes and the owner's decision on every forfeiture they create."""

from django.db import DatabaseError, IntegrityError, connection, transaction
from django.test import TestCase

from audit.models import AuditEvent
from circulation import forfeiture, grace, services
from circulation.models import Loan, PendingForfeiture
from core.errors import RuleViolation
from core.models import LibrarySetting, SettingChangeEvent
from finance import tokens
from finance.models import FinancialTransaction

from .helpers import D, make_owner, overdue_loan

# loan due 31 Dec 2026; 5 days overdue on 5 Jan 2027: not eligible at grace 7 (day 8 = 8 Jan),
# eligible at grace 3 (day 4 = 4 Jan)
TODAY = D(2027, 1, 5)


class Base(TestCase):
    def setUp(self):
        self.owner = make_owner()
        self.member, self.copy, self.loan = overdue_loan(self.owner)

    def change(self, days, today=TODAY):
        return grace.change_grace_days(actor=self.owner, new_days=days, today=today)

    def token(self):
        return tokens.token_state(self.member)[1]

    def pending(self):
        return PendingForfeiture.objects.get(loan=self.loan)

    def refused(self, fn, code, **kw):
        with self.assertRaises(RuleViolation) as ctx:
            fn(**kw)
        self.assertEqual(ctx.exception.code, code)


class ChangeTests(Base):
    def test_a_shorter_grace_period_makes_the_loan_newly_eligible_and_posts_nothing(self):
        event = self.change(3)
        self.assertEqual(
            (event.setting_name, event.old_value, event.new_value), ("grace_days", "7", "3")
        )
        self.assertEqual(event.actor_id, self.owner.pk)
        self.assertEqual(event.affected_loans[0]["loan_id"], self.loan.pk)
        self.assertEqual(event.affected_loans[0]["forfeiture_day"], "2027-01-04")
        self.assertEqual(
            LibrarySetting.load().grace_days, 3
        )  # the change applies to existing loans
        pending = self.pending()
        self.assertEqual((pending.status, pending.setting_change_id), ("pending", event.pk))
        self.assertEqual(self.token(), tokens.HELD)  # nothing posted without the owner
        self.assertFalse(FinancialTransaction.objects.filter(kind="token_forfeiture").exists())
        audit = AuditEvent.objects.get(action="setting.grace_days_changed")
        self.assertEqual(
            (audit.summary["old"], audit.summary["new"], audit.summary["newly_eligible_loans"]),
            (7, 3, 1),
        )

    def test_the_daily_job_never_posts_a_loan_that_awaits_a_decision(self):
        self.change(3)
        result = forfeiture.process_overdue_forfeitures(today=D(2027, 2, 1))
        self.assertEqual((result["forfeited"], result["skipped_decision"]), (0, 1))
        self.assertEqual(self.token(), tokens.HELD)

    def test_a_longer_grace_period_or_a_not_yet_overdue_loan_creates_no_decision(self):
        self.assertEqual(self.change(14).affected_loans, [])
        self.assertEqual(self.change(7, today=TODAY).affected_loans, [])
        other_member, _, other_loan = overdue_loan(
            self.owner, issued_on=D(2026, 12, 20), name="Bo", number="9811100011"
        )
        event = self.change(1, today=D(2027, 1, 5))  # Bo's loan is due 19 Jan: not overdue
        ids = [row["loan_id"] for row in event.affected_loans]
        self.assertNotIn(other_loan.pk, ids)
        self.assertIn(self.loan.pk, ids)

    def test_loans_already_eligible_closed_or_forfeited_are_not_re_prompted(self):
        # eligible under both settings (12 Jan is past day 8 for grace 7 and 4): the job's business
        event = self.change(4, today=D(2027, 1, 12))
        self.assertEqual(event.affected_loans, [])
        self.assertEqual(PendingForfeiture.objects.count(), 0)
        services.return_loan(
            actor=self.owner, loan=self.loan, today=D(2027, 1, 3)
        )  # closed on day 3
        self.assertEqual(
            self.change(1, today=D(2027, 1, 12)).affected_loans, []
        )  # closed loans untouched

    def test_validation(self):
        for bad in (-1, 366, 2.5, "7", None, True):
            self.refused(
                grace.change_grace_days,
                "grace_invalid",
                actor=self.owner,
                new_days=bad,
                today=TODAY,
            )
        self.refused(
            grace.change_grace_days, "grace_unchanged", actor=self.owner, new_days=7, today=TODAY
        )
        self.assertEqual(SettingChangeEvent.objects.count(), 0)


class DecisionTests(Base):
    def setUp(self):
        super().setUp()
        self.change(3)

    def test_confirming_posts_the_forfeiture_with_the_owner_the_reason_and_the_effective_date(self):
        pending = grace.confirm_pending(
            actor=self.owner, pending=self.pending(), reason="shorter grace applies", today=TODAY
        )
        self.assertEqual((pending.status, pending.resolved_by_id), ("confirmed", self.owner.pk))
        txn = FinancialTransaction.objects.get(kind="token_forfeiture")
        entry = txn.token_entries.get()
        self.assertEqual((txn.recorded_by_id, txn.recorded_by_system), (self.owner.pk, ""))
        self.assertEqual(
            (entry.forfeiture_trigger, entry.owner_confirmed, entry.reason),
            ("grace_change_confirmed", True, "shorter grace applies"),
        )
        self.assertEqual(txn.effective_date, D(2027, 1, 4))  # due + 3 + 1
        self.assertEqual(Loan.objects.get(pk=self.loan.pk).forfeiture_entry_id, entry.pk)
        self.assertEqual(pending.resulting_entry_id, entry.pk)
        self.assertEqual(self.token(), tokens.FORFEITED)
        self.assertTrue(
            AuditEvent.objects.filter(
                action="forfeiture.grace_change_confirmed", actor=self.owner
            ).exists()
        )

    def test_declining_needs_a_reason_posts_nothing_and_the_job_skips_the_loan_for_good(self):
        self.refused(
            grace.decline_pending,
            "reason_required",
            actor=self.owner,
            pending=self.pending(),
            reason="  ",
        )
        self.refused(
            grace.decline_pending,
            "reason_too_long",
            actor=self.owner,
            pending=self.pending(),
            reason="x" * 2001,
        )
        self.assertEqual(self.pending().status, "pending")
        pending = grace.decline_pending(
            actor=self.owner, pending=self.pending(), reason="member was ill"
        )
        self.assertEqual((pending.status, pending.decline_reason), ("declined", "member was ill"))
        self.assertEqual(self.token(), tokens.HELD)
        self.assertFalse(FinancialTransaction.objects.filter(kind="token_forfeiture").exists())
        for day in (D(2027, 1, 10), D(2027, 3, 1)):
            result = forfeiture.process_overdue_forfeitures(today=day)
            self.assertEqual((result["forfeited"], result["skipped_decision"]), (0, 1))
        audit = AuditEvent.objects.get(action="forfeiture.grace_change_declined")
        self.assertEqual(
            (audit.actor_id, audit.summary["reason"]), (self.owner.pk, "member was ill")
        )

    def test_the_owner_is_not_asked_again_for_the_same_loan(self):
        grace.decline_pending(actor=self.owner, pending=self.pending(), reason="member was ill")
        self.assertEqual(list(grace.pending_decisions()), [])
        event = self.change(1, today=D(2027, 1, 6))  # another change: still no new prompt
        self.assertEqual(event.affected_loans, [])
        self.assertEqual(PendingForfeiture.objects.filter(loan=self.loan).count(), 1)

    def test_a_decision_is_made_once(self):
        grace.confirm_pending(actor=self.owner, pending=self.pending(), reason="r", today=TODAY)
        self.refused(
            grace.confirm_pending,
            "already_resolved",
            actor=self.owner,
            pending=self.pending(),
            reason="r",
            today=TODAY,
        )
        self.refused(
            grace.decline_pending,
            "already_resolved",
            actor=self.owner,
            pending=self.pending(),
            reason="r",
        )

    def test_confirmation_needs_a_reason_and_current_eligibility(self):
        self.refused(
            grace.confirm_pending,
            "reason_required",
            actor=self.owner,
            pending=self.pending(),
            reason="",
            today=TODAY,
        )
        self.change(10, today=D(2027, 1, 6))  # grace raised again: no longer eligible
        self.refused(
            grace.confirm_pending,
            "not_eligible",
            actor=self.owner,
            pending=self.pending(),
            reason="r",
            today=D(2027, 1, 6),
        )
        self.assertEqual(self.pending().status, "pending")
        grace.decline_pending(
            actor=self.owner, pending=self.pending(), reason="no longer overdue enough"
        )

    def test_a_pending_decision_survives_a_return_and_is_judged_on_the_return_date(self):
        services.return_loan(
            actor=self.owner, loan=self.loan, today=D(2027, 1, 6)
        )  # return after day 4 (grace 3)
        self.assertEqual(self.token(), tokens.HELD)  # the owner has not decided yet
        grace.confirm_pending(
            actor=self.owner,
            pending=self.pending(),
            reason="returned after day 4",
            today=D(2027, 1, 6),
        )
        self.assertEqual(
            Loan.objects.get(pk=self.loan.pk).forfeiture_entry.effective_date, D(2027, 1, 4)
        )

    def test_confirming_without_a_held_token_changes_nothing(self):
        from finance import corrections

        corrections.forfeit_token(
            actor=self.owner,
            member=self.member,
            trigger="lost",
            owner_confirmed=True,
            reason="other",
            today=TODAY,
        )
        self.refused(
            grace.confirm_pending,
            "token_not_held",
            actor=self.owner,
            pending=self.pending(),
            reason="r",
            today=TODAY,
        )
        self.assertEqual(self.pending().status, "pending")
        self.assertEqual(FinancialTransaction.objects.filter(kind="token_forfeiture").count(), 1)


class DatabaseTests(Base):
    def setUp(self):
        super().setUp()
        self.change(3)
        self.pk = self.pending().pk

    def execute(self, sql, params=None):
        with transaction.atomic(), connection.cursor() as cur:
            cur.execute(sql, params or [])

    def test_pending_forfeitures_are_never_deleted_and_resolve_only_once(self):
        with self.assertRaisesMessage(DatabaseError, "never deleted"):
            self.execute("DELETE FROM circulation_pendingforfeiture")
        with self.assertRaises(IntegrityError):  # declined needs a reason
            self.execute(
                "UPDATE circulation_pendingforfeiture SET status='declined', resolved_at=now(), "
                "resolved_by_id=%s WHERE id=%s",
                [self.owner.pk, self.pk],
            )
        with self.assertRaises(IntegrityError):  # confirmed needs reason and entry
            self.execute(
                "UPDATE circulation_pendingforfeiture SET status='confirmed', resolved_at=now(), "
                "resolved_by_id=%s WHERE id=%s",
                [self.owner.pk, self.pk],
            )
        grace.decline_pending(actor=self.owner, pending=self.pending(), reason="r")
        with self.assertRaisesMessage(DatabaseError, "resolved pending forfeiture cannot change"):
            self.execute(
                "UPDATE circulation_pendingforfeiture SET decline_reason='edited' WHERE id=%s",
                [self.pk],
            )

    def test_one_pending_row_per_loan_and_event(self):
        with self.assertRaises(IntegrityError):
            self.execute(
                "INSERT INTO circulation_pendingforfeiture (loan_id, setting_change_id, status, "
                "decline_reason, confirm_reason, created_at) SELECT loan_id, setting_change_id, "
                "'pending', '', '', now() FROM circulation_pendingforfeiture"
            )

    def test_setting_change_events_are_append_only(self):
        for sql in (
            "UPDATE core_settingchangeevent SET new_value = '99'",
            "DELETE FROM core_settingchangeevent",
        ):
            with self.subTest(sql), self.assertRaisesMessage(DatabaseError, "append-only"):
                self.execute(sql)
