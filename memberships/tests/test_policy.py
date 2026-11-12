"""Phase 3a: every date rule, with the approved examples and boundaries (pure functions)."""

from datetime import date, timedelta

from django.test import SimpleTestCase

from core.errors import RuleViolation
from memberships import policy as p
from memberships.policy import Period

D = date


class CalendarMonthTests(SimpleTestCase):
    def test_approved_examples_first_expired_and_last_valid_day(self):
        cases = [
            (D(2026, 11, 11), 6, D(2027, 5, 11), D(2027, 5, 10)),
            (D(2026, 8, 31), 6, D(2027, 2, 28), D(2027, 2, 27)),
            (D(2027, 1, 31), 1, D(2027, 2, 28), D(2027, 2, 27)),
            (D(2028, 2, 29), 12, D(2029, 2, 28), D(2029, 2, 27)),
        ]
        for start, months, expiry, last in cases:
            with self.subTest(start=start, months=months):
                self.assertEqual(p.expires_on(start, months), expiry)
                self.assertEqual(p.last_valid_day(expiry), last)

    def test_month_end_clamping_and_leap_years(self):
        self.assertEqual(p.add_months(D(2027, 1, 31), 1), D(2027, 2, 28))
        self.assertEqual(p.add_months(D(2028, 1, 31), 1), D(2028, 2, 29))  # leap year
        self.assertEqual(p.add_months(D(2028, 2, 29), 12), D(2029, 2, 28))
        self.assertEqual(p.add_months(D(2028, 2, 29), 48), D(2032, 2, 29))
        self.assertEqual(p.add_months(D(2026, 8, 31), 1), D(2026, 9, 30))
        self.assertEqual(p.add_months(D(2026, 3, 31), 6), D(2026, 9, 30))
        self.assertEqual(p.add_months(D(2026, 12, 31), 2), D(2027, 2, 28))

    def test_it_is_calendar_months_not_fixed_days(self):
        # six months from 1 Mar is 184 days, from 1 Sep it is 181: neither is "180 days"
        self.assertEqual((p.expires_on(D(2026, 3, 1), 6) - D(2026, 3, 1)).days, 184)
        self.assertEqual((p.expires_on(D(2026, 9, 1), 6) - D(2026, 9, 1)).days, 181)

    def test_year_rollover_and_zero_and_negative(self):
        self.assertEqual(p.add_months(D(2026, 11, 30), 3), D(2027, 2, 28))
        self.assertEqual(p.add_months(D(2026, 12, 15), 12), D(2027, 12, 15))
        self.assertEqual(p.add_months(D(2026, 5, 5), 0), D(2026, 5, 5))
        with self.assertRaises(ValueError):
            p.add_months(D(2026, 5, 5), -1)

    def test_every_start_day_of_a_leap_cycle_is_clamped_not_overflowed(self):
        day = D(2027, 1, 1)
        while day < D(2029, 1, 1):
            expiry = p.expires_on(day, 6)
            self.assertGreater(expiry, day)
            self.assertLessEqual(expiry.day, day.day)
            self.assertGreaterEqual((expiry - day).days, 181)
            self.assertLessEqual((expiry - day).days, 184)
            day += timedelta(days=1)


class StateTests(SimpleTestCase):
    period = Period(D(2026, 11, 11), D(2027, 5, 11))  # last valid 10 May 2027

    def test_inclusive_start_and_exclusive_expiry(self):
        s = p.state
        self.assertEqual(s(self.period, D(2026, 11, 10)), p.SCHEDULED)
        self.assertEqual(s(self.period, D(2026, 11, 11)), p.ACTIVE)
        self.assertEqual(s(self.period, D(2027, 5, 10)), p.ACTIVE)  # last valid day
        self.assertEqual(s(self.period, D(2027, 5, 11)), p.EXPIRED)  # first expired day
        self.assertTrue(p.is_valid_on(self.period, D(2027, 5, 10)))
        self.assertFalse(p.is_valid_on(self.period, D(2027, 5, 11)))
        self.assertFalse(p.is_valid_on(self.period, D(2026, 11, 10)))

    def test_cancelled_is_never_valid(self):
        gone = self.period._replace(cancelled=True)
        self.assertEqual(p.state(gone, D(2027, 1, 1)), p.CANCELLED)
        self.assertFalse(p.is_valid_on(gone, D(2027, 1, 1)))

    def test_current_and_scheduled_selection(self):
        a = Period(D(2026, 1, 1), D(2026, 7, 1))
        b = Period(D(2026, 7, 1), D(2027, 1, 1))
        self.assertEqual(p.current_period([a, b], D(2026, 6, 30)), a)
        self.assertEqual(p.current_period([a, b], D(2026, 7, 1)), b)  # no overlap at the seam
        self.assertEqual(p.scheduled_periods([a, b], D(2026, 6, 30)), [b])
        self.assertIsNone(p.current_period([a, b], D(2027, 1, 1)))
        self.assertTrue(p.periods_overlap(a, Period(D(2026, 6, 30), D(2026, 8, 1))))
        self.assertFalse(p.periods_overlap(a, b))


class NextStartTests(SimpleTestCase):
    def code(self, *args, **kw):
        with self.assertRaises(RuleViolation) as ctx:
            p.next_start(*args, **kw)
        return ctx.exception.code

    def test_first_subscription_starts_on_the_entered_date(self):
        self.assertEqual(p.next_start([], D(2026, 11, 11), D(2026, 11, 11)), D(2026, 11, 11))
        self.assertEqual(p.next_start([], D(2026, 11, 11), D(2026, 12, 1)), D(2026, 12, 1))
        self.assertEqual(self.code([], D(2026, 11, 11)), "activation_date_required")

    def test_early_renewal_starts_on_the_first_expired_day(self):
        cur = Period(D(2026, 11, 11), D(2027, 5, 11))
        self.assertEqual(p.next_start([cur], D(2027, 1, 1)), D(2027, 5, 11))
        self.assertEqual(p.next_start([cur], D(2027, 5, 10)), D(2027, 5, 11))  # last valid day
        self.assertEqual(p.next_start([cur], D(2027, 1, 1), D(2027, 5, 11)), D(2027, 5, 11))
        self.assertEqual(self.code([cur], D(2027, 1, 1), D(2027, 6, 1)), "activation_date_fixed")

    def test_late_renewal_starts_on_the_owner_entered_date(self):
        old = Period(D(2026, 1, 1), D(2026, 7, 1))
        today = D(2026, 9, 15)
        self.assertEqual(p.next_start([old], today, D(2026, 9, 20)), D(2026, 9, 20))
        self.assertEqual(p.next_start([old], today, D(2026, 7, 1)), D(2026, 7, 1))  # backdated seam
        self.assertEqual(self.code([old], today), "activation_date_required")
        self.assertEqual(self.code([old], today, D(2026, 6, 30)), "activation_overlap")

    def test_on_the_first_expired_day_the_renewal_is_late_not_early(self):
        cur = Period(D(2026, 11, 11), D(2027, 5, 11))
        self.assertEqual(self.code([cur], D(2027, 5, 11)), "activation_date_required")
        self.assertEqual(p.next_start([cur], D(2027, 5, 11), D(2027, 5, 11)), D(2027, 5, 11))

    def test_at_most_one_scheduled_subscription(self):
        cur = Period(D(2026, 11, 11), D(2027, 5, 11))
        nxt = Period(D(2027, 5, 11), D(2027, 11, 11))
        self.assertEqual(self.code([cur, nxt], D(2027, 1, 1)), "scheduled_exists")
        # once the scheduled period starts it is the current one and may itself be renewed
        self.assertEqual(p.next_start([cur, nxt], D(2027, 6, 1)), D(2027, 11, 11))

    def test_cancelled_periods_do_not_count(self):
        gone = Period(D(2026, 11, 11), D(2027, 5, 11), cancelled=True)
        self.assertEqual(p.next_start([gone], D(2027, 1, 1), D(2027, 1, 1)), D(2027, 1, 1))
        sched = Period(D(2027, 6, 1), D(2027, 12, 1), cancelled=True)
        self.assertEqual(p.next_start([sched], D(2027, 1, 1), D(2027, 1, 1)), D(2027, 1, 1))


class BorrowingTests(SimpleTestCase):
    period = Period(D(2026, 11, 11), D(2027, 5, 11))  # last valid 10 May 2027

    def code(self, fn, *args):
        with self.assertRaises(RuleViolation) as ctx:
            fn(*args)
        return ctx.exception.code

    def test_new_loans_blocked_from_25_april_when_last_valid_day_is_10_may(self):
        self.assertEqual(p.issue_blocked_from(self.period), D(2027, 4, 25))
        self.assertFalse(p.is_issue_blocked(self.period, D(2027, 4, 24)))
        self.assertTrue(p.is_issue_blocked(self.period, D(2027, 4, 25)))
        self.assertTrue(p.is_issue_blocked(self.period, D(2027, 5, 10)))

    def test_block_days_come_from_the_setting(self):
        self.assertEqual(p.issue_blocked_from(self.period, 10), D(2027, 4, 30))

    def test_issue_on_24_april_with_a_capped_due_date(self):
        # 24 April + 30 days = 24 May: after the last valid day, so the due-date cap rejects it
        due = D(2027, 4, 24) + timedelta(days=30)
        self.assertEqual(
            self.code(p.check_issue, self.period, D(2027, 4, 24), due), "due_after_last_valid_day"
        )
        p.check_issue(self.period, D(2027, 4, 24), D(2027, 5, 10))  # exactly the last valid day

    def test_due_date_may_equal_the_last_valid_day_but_never_the_first_expired_day(self):
        p.check_due_date(self.period, D(2027, 5, 10))
        self.assertEqual(
            self.code(p.check_due_date, self.period, D(2027, 5, 11)), "due_after_last_valid_day"
        )
        self.assertEqual(p.max_due_date(self.period), D(2027, 5, 10))

    def test_block_and_cap_are_independent(self):
        early = D(2026, 12, 1)
        p.check_issue(self.period, early, early + timedelta(days=30))  # both pass
        # block fails although the due date itself is fine
        self.assertEqual(
            self.code(p.check_issue, self.period, D(2027, 4, 25), D(2027, 5, 1)),
            "issue_window_closed",
        )
        # cap fails although the block does not apply
        self.assertEqual(
            self.code(p.check_issue, self.period, D(2027, 4, 20), D(2027, 5, 20)),
            "due_after_last_valid_day",
        )

    def test_no_loan_outside_the_validity_window(self):
        self.assertEqual(
            self.code(p.check_issue, self.period, D(2026, 11, 10), D(2026, 12, 10)),
            "no_valid_subscription",
        )
        self.assertEqual(
            self.code(p.check_issue, self.period, D(2027, 5, 11), D(2027, 6, 10)),
            "no_valid_subscription",
        )
        self.assertEqual(
            self.code(p.check_issue, None, D(2026, 12, 1), D(2026, 12, 31)), "no_valid_subscription"
        )

    def test_loan_renewal_rules(self):
        due = D(2027, 3, 1)
        p.check_loan_renewal(self.period, D(2027, 2, 1), due, D(2027, 3, 31))
        p.check_loan_renewal(self.period, due, due, D(2027, 3, 31))  # on the due date itself
        self.assertEqual(
            self.code(p.check_loan_renewal, self.period, D(2027, 3, 2), due, D(2027, 4, 1)),
            "renewal_after_due_date",
        )  # one day late: no renewal, even inside grace
        self.assertEqual(
            self.code(
                p.check_loan_renewal, self.period, D(2027, 4, 20), D(2027, 4, 25), D(2027, 5, 25)
            ),
            "due_after_last_valid_day",
        )  # never silently extended
        p.check_loan_renewal(self.period, D(2027, 4, 20), D(2027, 4, 25), D(2027, 5, 10))
