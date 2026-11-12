"""Daily pro-rata allocation (full precision) and the separate collections view."""

from datetime import date, timedelta
from decimal import Decimal
from fractions import Fraction

from django.db import transaction
from django.test import SimpleTestCase, TestCase

from finance import allocation as al
from finance import reporting
from finance import services as fs
from finance.services import Entry, Item

from .helpers import FLOW, K, make_member, make_owner, sub_item, tok_item

D = date


class AllocationTests(SimpleTestCase):
    def test_a_subscription_allocates_exactly_its_price_across_months(self):
        for price, start, months_end in (
            (80000, D(2026, 11, 11), D(2027, 5, 11)),
            (150000, D(2028, 2, 29), D(2029, 2, 28)),  # leap start
            (80000, D(2026, 8, 31), D(2027, 2, 28)),  # clamped expiry
            (150000, D(2027, 3, 1), D(2028, 3, 1)),  # spans a leap February
        ):
            with self.subTest(start=start):
                parts = al.allocate_by_month(price, start, months_end)
                self.assertEqual(sum(parts.values()), Fraction(price))

    def test_no_per_day_rounding_so_no_cumulative_drift(self):
        # 80000 / 181 days is not representable in paise; the exact sum is still 80000
        start, end = D(2026, 11, 11), D(2027, 5, 11)
        days = (end - start).days
        total = sum(
            al.allocate(80000, start, end, d, d)
            for d in (start + timedelta(i) for i in range(days))
        )
        self.assertEqual(total, Fraction(80000))
        self.assertNotEqual(al.daily_amount(80000, start, end).denominator, 1)

    def test_window_bounds_are_inclusive_and_expiry_day_is_excluded(self):
        start, end = D(2026, 11, 11), D(2027, 5, 11)  # 181 valid days, last valid 10 May
        per_day = al.daily_amount(80000, start, end)
        self.assertEqual(al.allocate(80000, start, end, start, start), per_day)
        self.assertEqual(al.allocate(80000, start, end, D(2027, 5, 10), D(2027, 5, 10)), per_day)
        self.assertEqual(al.allocate(80000, start, end, D(2027, 5, 11), D(2027, 5, 11)), 0)
        self.assertEqual(al.allocate(80000, start, end, D(2026, 11, 10), D(2026, 11, 10)), 0)
        self.assertEqual(al.allocate(80000, start, end, D(2020, 1, 1), D(2030, 1, 1)), 80000)
        self.assertEqual(al.allocate(80000, start, end, D(2027, 1, 1), D(2026, 1, 1)), 0)

    def test_month_split_of_the_approved_example(self):
        start, end = D(2026, 11, 11), D(2027, 5, 11)
        nov = al.allocate(80000, start, end, *al.month_range(2026, 11))
        self.assertEqual(nov, Fraction(80000 * 20, 181))  # 11..30 Nov = 20 days

    def test_rounding_happens_only_when_displaying(self):
        value = Fraction(80000 * 20, 181)
        self.assertEqual(al.display_rupees(value), Decimal("88.40"))
        self.assertEqual(
            al.display_rupees(Fraction(5, 2)), Decimal("0.03")
        )  # 2.5 paise rounds half up
        self.assertEqual(al.display_rupees(Fraction(150000)), Decimal("1500.00"))
        self.assertEqual(value, Fraction(1600000, 181))  # the value itself is untouched

    def test_invalid_periods_are_rejected(self):
        with self.assertRaises(ValueError):
            al.daily_amount(100, D(2026, 1, 2), D(2026, 1, 2))


class CollectionsTests(TestCase):
    def setUp(self):
        self.owner = make_owner()
        self.member = make_member(self.owner)

    def post(self, items, today, **kw):
        with transaction.atomic():
            return fs.post_items(
                member=self.member, items=items, actor=self.owner, today=today, **kw
            )

    def test_tokens_are_never_counted_as_subscription_fees(self):
        today = D(2026, 11, 11)
        self.post([sub_item(), tok_item()], today)
        out = reporting.collections(today, today)
        self.assertEqual(out["subscription_fees"], 80000)
        self.assertEqual(out["token_collections"], 50000)
        self.assertEqual(out["by_method"], {"cash": 80000, "upi": 50000})

    def test_each_split_component_counts_on_its_own_payment_date(self):
        today = D(2026, 11, 11)
        entries = [Entry("cash", 30000, paid_on=D(2026, 11, 10)), Entry("upi", 50000)]
        self.post(
            [sub_item(entries=entries)],
            today,
            backdate_reason="part paid yesterday",
            backdate_confirmed=True,
        )
        self.assertEqual(
            reporting.collections(D(2026, 11, 10), D(2026, 11, 10))["subscription_fees"], 30000
        )
        self.assertEqual(
            reporting.collections(D(2026, 11, 11), D(2026, 11, 11))["subscription_fees"], 50000
        )
        self.assertEqual(
            reporting.collections(D(2026, 11, 1), D(2026, 11, 30))["subscription_fees"], 80000
        )

    def test_refunds_adjustments_and_forfeitures_are_reported_apart(self):
        today = D(2026, 11, 11)
        self.post([tok_item()], today)
        self.post(
            [Item(K.TOKEN_REFUND, FLOW.OUT, 50000, today, [Entry("cash", 50000)], label="R")], today
        )
        self.post(
            [Item(K.ADJUSTMENT, FLOW.IN, 700, today, [Entry("upi", 700)], reason="x", label="A")],
            today,
        )
        self.post(
            [Item(K.ADJUSTMENT, FLOW.OUT, 200, today, [Entry("cash", 200)], reason="y", label="A")],
            today,
        )
        self.post([Item(K.TOKEN_FORFEITURE, FLOW.NONE, 50000, today, label="F")], today)
        out = reporting.collections(today, today)
        self.assertEqual(
            (
                out["token_collections"],
                out["token_refunds"],
                out["adjustments_in"],
                out["adjustments_out"],
                out["tokens_forfeited"],
                out["subscription_fees"],
            ),
            (50000, 50000, 700, 200, 50000, 0),
        )
        self.assertEqual(out["by_method"], {"cash": -50200, "upi": 50700})
