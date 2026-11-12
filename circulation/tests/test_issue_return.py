"""Phase 4a: loan issue and return, every block, backdating, copy events (PostgreSQL)."""

from datetime import timedelta

from django.test import TestCase

from audit.models import AuditEvent
from catalogue.models import BookCopy, CopyEvent
from circulation import services
from circulation.models import Loan
from core.errors import RuleViolation
from core.models import DomainEvent, LibrarySetting
from finance import corrections as fc
from finance import tokens
from members import services as member_services
from members.models import Member

from .helpers import D, Entry, make_copies, make_owner, member_at, ms

START = D(2026, 11, 11)  # subscription 11 Nov 2026 .. last valid 10 May 2027


class Base(TestCase):
    def setUp(self):
        self.owner = make_owner()
        self.member = member_at(self.owner)
        self.copies = make_copies(self.owner, 3)

    def issue(self, copy=None, member=None, **kw):
        kw.setdefault("today", START)
        return services.issue_loan(
            actor=self.owner, member=member or self.member, copy=copy or self.copies[0], **kw
        )

    def code(self, **kw):
        with self.assertRaises(RuleViolation) as ctx:
            self.issue(**kw)
        return ctx.exception.code


class IssueTests(Base):
    def test_a_normal_issue_is_30_days_and_updates_the_copy_and_history(self):
        loan = self.issue(today=D(2026, 12, 1))
        self.assertEqual((loan.issued_on, loan.due_on), (D(2026, 12, 1), D(2026, 12, 31)))
        self.assertEqual((loan.status, loan.issue_condition), ("active", "good"))
        self.assertEqual(loan.subscription_id, self.member.subscriptions.get().pk)
        copy = BookCopy.objects.get(pk=self.copies[0].pk)
        self.assertEqual((copy.status, copy.condition), ("borrowed", "good"))
        event = CopyEvent.objects.filter(event_type="issued").get()
        self.assertEqual(
            (event.loan_id, event.from_value, event.to_value), (loan.pk, "available", "borrowed")
        )
        self.assertEqual(event.actor_id, self.owner.pk)
        audit = AuditEvent.objects.get(action="loan.issued")
        self.assertEqual(audit.summary["due_on"], "2026-12-31")
        self.assertIs(audit.summary["backdated"], False)

    def test_loan_days_setting_drives_the_default(self):
        setting = LibrarySetting.load()
        setting.loan_days = 14
        setting.save()
        self.assertEqual(self.issue(today=D(2026, 12, 1)).due_on, D(2026, 12, 15))

    def test_one_active_loan_per_member(self):
        self.issue()
        self.assertEqual(self.code(copy=self.copies[1]), "loan_outstanding")

    def test_one_physical_copy_per_loan(self):
        other = member_at(self.owner, "Bo", "9876543211")
        self.issue()
        self.assertEqual(self.code(copy=self.copies[0], member=other), "copy_unavailable")

    def test_only_good_and_available_copies_are_issuable(self):
        states = [
            ("poor", "withdrawn"),
            ("damaged", "damaged"),
            ("damaged", "withdrawn"),
            ("lost", "lost"),
            ("good", "withdrawn"),
        ]
        for index, (condition, status) in enumerate(states):
            copy = make_copies(self.owner, 1, title=f"T{index}")[0]
            BookCopy.objects.filter(pk=copy.pk).update(condition=condition, status=status)
            with self.subTest(condition=condition, status=status):
                self.assertEqual(self.code(copy=copy), "copy_unavailable")
        self.assertEqual(Loan.objects.count(), 0)

    def test_member_must_be_active_and_not_anonymised(self):
        Member.objects.filter(pk=self.member.pk).update(status="suspended")
        self.member.refresh_from_db()
        self.assertEqual(self.code(), "member_not_active")
        Member.objects.filter(pk=self.member.pk).update(status="archived")
        self.member.refresh_from_db()
        self.assertEqual(self.code(), "member_not_active")
        Member.objects.filter(pk=self.member.pk).update(
            status="active", needs_new_subscription=True
        )
        self.member.refresh_from_db()
        self.assertEqual(self.code(), "new_subscription_required")

    def test_subscription_must_be_valid_on_the_issue_date(self):
        # before the subscription starts / on the first expired day / long after
        for day in (D(2026, 11, 10), D(2027, 5, 11), D(2028, 1, 1)):
            with self.subTest(day=day):
                self.assertEqual(self.code(issued_on=day, today=day), "no_valid_subscription")

    def test_a_future_dated_subscription_does_not_allow_borrowing_yet(self):
        scheduled = member_at(
            self.owner, "Cy", "9876543212", activation_date=START + timedelta(days=1)
        )
        self.assertEqual(self.code(member=scheduled), "no_valid_subscription")
        self.assertEqual(
            self.issue(member=scheduled, today=START + timedelta(days=1)).status, "active"
        )

    def test_token_must_be_held(self):
        fc.refund_token(
            actor=self.owner, member=self.member, entries=[Entry("cash", 50000)], today=START
        )
        self.assertEqual(self.code(), "token_missing")
        ms.pay_fresh_token(
            actor=self.owner, member=self.member, entries=[Entry("cash", 50000)], today=START
        )
        self.assertEqual(self.issue().status, "active")

    def test_a_forfeited_token_suspends_borrowing_but_not_the_subscription(self):
        fc.forfeit_token(
            actor=self.owner,
            member=self.member,
            trigger="lost",
            owner_confirmed=True,
            reason="r",
            today=START,
        )
        self.assertEqual(self.code(), "token_forfeited")
        self.assertIsNotNone(ms.current_subscription(self.member, START))

    def test_anonymised_members_cannot_borrow(self):
        member_services.anonymise_member(self.member, actor=self.owner, reason="r", confirm=True)
        self.assertEqual(self.code(), "anonymised")


class DateRuleTests(Base):
    """Last valid day 10 May 2027: new loans blocked from 25 April; due cap = last valid day."""

    def test_new_loans_are_blocked_from_25_april(self):
        issued = self.issue(issued_on=D(2027, 4, 24), today=D(2027, 4, 24), due_on=D(2027, 5, 10))
        self.assertEqual(issued.due_on, D(2027, 5, 10))  # exactly the last valid day
        for day in (D(2027, 4, 25), D(2027, 5, 1), D(2027, 5, 10)):
            other = member_at(self.owner, f"M{day.day}", f"98111000{day.day:02d}")
            with self.subTest(day=day):
                self.assertEqual(
                    self.code(
                        member=other,
                        copy=make_copies(self.owner, 1, f"B{day.day}")[0],
                        issued_on=day,
                        today=day,
                        due_on=day + timedelta(days=1),
                    ),
                    "issue_window_closed",
                )

    def test_the_due_date_cap_is_independent_of_the_15_day_block(self):
        # 20 April is outside the block but 20 April + 30 days passes the last valid day
        self.assertEqual(
            self.code(issued_on=D(2027, 4, 20), today=D(2027, 4, 20)), "due_after_last_valid_day"
        )
        self.assertEqual(
            self.code(issued_on=D(2027, 4, 20), today=D(2027, 4, 20), due_on=D(2027, 5, 11)),
            "due_after_last_valid_day",
        )  # the first expired day is never a valid due date
        self.assertEqual(
            self.issue(
                issued_on=D(2027, 4, 20), today=D(2027, 4, 20), due_on=D(2027, 5, 10)
            ).due_on,
            D(2027, 5, 10),
        )

    def test_the_default_due_date_is_never_silently_shortened_or_extended(self):
        self.assertEqual(
            self.code(issued_on=D(2027, 3, 20), today=D(2027, 3, 20), due_on=D(2027, 5, 1)),
            "due_date_invalid",
        )  # later than 30 days
        self.assertEqual(self.code(due_on=START - timedelta(days=1)), "due_date_invalid")
        # an issue that fits the cap with the full 30 days
        self.assertEqual(
            self.issue(issued_on=D(2027, 3, 1), today=D(2027, 3, 1)).due_on, D(2027, 3, 31)
        )

    def test_the_block_length_follows_the_setting(self):
        setting = LibrarySetting.load()
        setting.issue_block_days = 10
        setting.save()
        ok = self.issue(issued_on=D(2027, 4, 29), today=D(2027, 4, 29), due_on=D(2027, 5, 10))
        self.assertEqual(ok.issued_on, D(2027, 4, 29))
        other = member_at(self.owner, "Dee", "9876543299")
        self.assertEqual(
            self.code(
                member=other,
                copy=self.copies[1],
                issued_on=D(2027, 4, 30),
                today=D(2027, 4, 30),
                due_on=D(2027, 5, 10),
            ),
            "issue_window_closed",
        )


class BackdatingTests(Base):
    def test_future_issue_dates_are_rejected(self):
        self.assertEqual(self.code(issued_on=START + timedelta(days=1)), "future_date")

    def test_a_past_issue_date_needs_a_reason_and_extra_confirmation(self):
        past = D(2026, 12, 1)
        today = D(2026, 12, 5)
        self.assertEqual(self.code(issued_on=past, today=today), "backdate_reason_required")
        self.assertEqual(
            self.code(issued_on=past, today=today, backdate_reason="  "), "backdate_reason_required"
        )
        self.assertEqual(
            self.code(issued_on=past, today=today, backdate_reason="slip"),
            "backdate_confirmation_required",
        )
        loan = self.issue(
            issued_on=past, today=today, backdate_reason="entered late", backdate_confirmed=True
        )
        self.assertEqual(
            (loan.backdated, loan.backdate_reason, loan.backdate_confirmed),
            (True, "entered late", True),
        )
        self.assertEqual(loan.due_on, D(2026, 12, 31))  # based on the event date
        self.assertGreater(loan.issued_processed_at.date(), D(2020, 1, 1))
        audit = AuditEvent.objects.get(action="loan.issued")
        self.assertIs(audit.summary["backdated"], True)
        self.assertEqual(audit.summary["backdate_reason"], "entered late")
        self.assertIs(audit.summary["backdate_owner_confirmed"], True)
        self.assertEqual(CopyEvent.objects.get(event_type="issued").event_date, past)

    def test_an_overlong_reason_is_rejected(self):
        self.assertEqual(
            self.code(
                issued_on=D(2026, 12, 1),
                today=D(2026, 12, 5),
                backdate_reason="x" * 2001,
                backdate_confirmed=True,
            ),
            "reason_too_long",
        )

    def test_a_backdated_issue_must_fall_inside_the_subscription(self):
        self.assertEqual(
            self.code(
                issued_on=D(2026, 11, 1),
                today=D(2026, 11, 5),
                backdate_reason="r",
                backdate_confirmed=True,
            ),
            "no_valid_subscription",
        )


class ReturnTests(Base):
    def setUp(self):
        super().setUp()
        self.loan = self.issue(today=D(2026, 12, 1))

    def ret(self, **kw):
        kw.setdefault("today", D(2026, 12, 10))
        return services.return_loan(actor=self.owner, loan=self.loan, **kw)

    def test_a_good_return_closes_the_loan_and_frees_the_copy(self):
        loan = self.ret()
        copy = BookCopy.objects.get(pk=self.copies[0].pk)
        self.assertEqual(
            (loan.status, loan.return_condition, loan.returned_on),
            ("returned", "good", D(2026, 12, 10)),
        )
        self.assertEqual((copy.status, copy.condition), ("available", "good"))
        event = CopyEvent.objects.get(event_type="returned")
        self.assertEqual(
            (event.loan_id, event.from_value, event.to_value), (loan.pk, "borrowed", "available")
        )
        self.assertTrue(AuditEvent.objects.filter(action="loan.returned").exists())

    def test_the_member_and_the_copy_can_borrow_again_afterwards(self):
        self.ret()
        again = self.issue(copy=self.copies[0], today=D(2026, 12, 11))
        self.assertEqual(again.status, "active")
        self.assertEqual(Loan.objects.filter(member=self.member).count(), 2)  # history kept

    def test_a_loan_cannot_be_returned_twice_or_before_it_was_issued(self):
        with self.assertRaises(RuleViolation) as ctx:
            self.ret(returned_on=D(2026, 11, 30), backdate_reason="r", backdate_confirmed=True)
        self.assertEqual(ctx.exception.code, "return_before_issue")
        self.ret()
        with self.assertRaises(RuleViolation) as ctx:
            self.ret()
        self.assertEqual(ctx.exception.code, "loan_closed")

    def test_a_past_return_date_needs_a_reason_and_confirmation_and_is_audited(self):
        past = D(2026, 12, 5)
        for kwargs, code in (
            ({}, "backdate_reason_required"),
            ({"backdate_reason": "late entry"}, "backdate_confirmation_required"),
        ):
            with self.assertRaises(RuleViolation) as ctx:
                self.ret(returned_on=past, **kwargs)
            self.assertEqual(ctx.exception.code, code)
        loan = self.ret(returned_on=past, backdate_reason="late entry", backdate_confirmed=True)
        self.assertEqual((loan.return_backdated, loan.return_backdate_reason), (True, "late entry"))
        audit = AuditEvent.objects.get(action="loan.returned")
        self.assertEqual(
            (audit.summary["returned_on"], audit.summary["backdated"]), ("2026-12-05", True)
        )

    def test_future_return_dates_are_rejected(self):
        with self.assertRaises(RuleViolation) as ctx:
            self.ret(returned_on=D(2026, 12, 11))
        self.assertEqual(ctx.exception.code, "future_date")

    def test_non_good_outcomes_need_the_owners_confirmation_and_a_reason(self):
        for condition in ("poor", "damaged"):
            with self.assertRaises(RuleViolation) as ctx:
                self.ret(condition=condition)
            self.assertEqual(ctx.exception.code, "confirmation_required")
            with self.assertRaises(RuleViolation) as ctx:
                self.ret(condition=condition, owner_confirmed=True)
            self.assertEqual(ctx.exception.code, "reason_required")
        with self.assertRaises(RuleViolation) as ctx:
            self.ret(condition="lost")
        self.assertEqual(ctx.exception.code, "outcome_invalid")
        self.assertEqual(Loan.objects.get(pk=self.loan.pk).status, "active")


class OutstandingLoanAndEventsTests(Base):
    def test_the_hook_is_registered_and_blocks_a_token_refund_until_the_book_is_back(self):
        self.assertIn(services.member_has_active_loan, fc.OUTSTANDING_LOAN_CHECKS)
        loan = self.issue(today=D(2026, 12, 1))
        with self.assertRaises(RuleViolation) as ctx:
            fc.refund_token(
                actor=self.owner,
                member=self.member,
                entries=[Entry("cash", 50000)],
                today=D(2026, 12, 2),
            )
        self.assertEqual(ctx.exception.code, "loans_outstanding")
        services.return_loan(actor=self.owner, loan=loan, today=D(2026, 12, 3))
        posted = fc.refund_token(
            actor=self.owner,
            member=self.member,
            entries=[Entry("cash", 50000)],
            today=D(2026, 12, 3),
        )
        self.assertEqual(tokens.token_state(self.member)[1], tokens.REFUNDED)
        self.assertEqual(posted.transactions[0].amount_paise, 50000)

    def test_events_are_stored_in_the_transaction_with_stable_keys(self):
        loan = self.issue(today=D(2026, 12, 1))
        services.return_loan(actor=self.owner, loan=loan, today=D(2026, 12, 3))
        self.assertEqual(
            list(
                DomainEvent.objects.filter(event_type__startswith="loan.").values_list(
                    "event_type", "dedupe_key"
                )
            ),
            [
                ("loan.issued", f"loan:{loan.pk}:issued"),
                ("loan.returned", f"loan:{loan.pk}:returned"),
            ],
        )

    def test_a_failed_issue_stores_no_event_and_changes_nothing(self):
        self.assertEqual(
            self.code(
                copy=self.copies[0],
                issued_on=D(2026, 11, 1),
                today=D(2026, 11, 5),
                backdate_reason="r",
                backdate_confirmed=True,
            ),
            "no_valid_subscription",
        )
        self.assertFalse(DomainEvent.objects.filter(event_type__startswith="loan.").exists())
        self.assertEqual(BookCopy.objects.get(pk=self.copies[0].pk).status, "available")
        self.assertEqual(Loan.objects.count(), 0)
