"""Phase 6a: periods, the nine reports, both revenue bases, D-7 listing and the report screens."""

import io
from datetime import timedelta
from fractions import Fraction

from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from openpyxl import load_workbook

from audit.models import AuditEvent
from circulation import services
from circulation.tests.helpers import D, Entry, make_copies, make_owner, member_at, overdue_loan
from finance import corrections as fc
from finance.allocation import allocate, allocate_by_month
from members import services as members
from memberships import services as ms
from memberships.models import MemberSubscription
from memberships.tests.helpers import PW, register
from reports import periods, queries
from reports import render as rr
from reports.core import ROUNDING_NOTE


def build(key, start, end, today=None):
    return queries.build(key, start, end, today or end)


def section(report, title_part):
    return next(s for s in report.sections if title_part in s.title)


def measure(report, title_part, label):
    return next(r[1] for r in section(report, title_part).rows if r[0] == label)


class PeriodTests(SimpleTestCase):
    def test_calendar_presets_and_boundaries(self):
        r = periods.resolve
        self.assertEqual(r("today", D(2027, 3, 4))[:2], (D(2027, 3, 4), D(2027, 3, 4)))
        self.assertEqual(r("week", D(2027, 3, 4))[:2], (D(2027, 3, 1), D(2027, 3, 7)))  # Thu
        self.assertEqual(r("week", D(2027, 3, 1))[:2], (D(2027, 3, 1), D(2027, 3, 7)))  # Mon
        self.assertEqual(r("week", D(2027, 3, 7))[:2], (D(2027, 3, 1), D(2027, 3, 7)))  # Sun
        self.assertEqual(r("month", D(2028, 2, 10))[:2], (D(2028, 2, 1), D(2028, 2, 29)))  # leap
        self.assertEqual(r("month", D(2027, 2, 10))[:2], (D(2027, 2, 1), D(2027, 2, 28)))
        self.assertEqual(r("month", D(2027, 12, 31))[:2], (D(2027, 12, 1), D(2027, 12, 31)))
        for day, q in (
            (D(2027, 1, 1), (D(2027, 1, 1), D(2027, 3, 31))),
            (D(2027, 3, 31), (D(2027, 1, 1), D(2027, 3, 31))),
            (D(2027, 4, 1), (D(2027, 4, 1), D(2027, 6, 30))),
            (D(2027, 9, 30), (D(2027, 7, 1), D(2027, 9, 30))),
            (D(2027, 12, 31), (D(2027, 10, 1), D(2027, 12, 31))),
        ):
            self.assertEqual(r("quarter", day)[:2], q, day)
        self.assertEqual(r("year", D(2028, 6, 1))[:2], (D(2028, 1, 1), D(2028, 12, 31)))

    def test_custom_range_validation(self):
        r = periods.resolve
        self.assertEqual(
            r("custom", D(2027, 1, 1), "2027-02-01", "2027-02-01")[:2], (D(2027, 2, 1),) * 2
        )
        for bad in (
            ("2027-02-02", "2027-02-01"),
            ("x", "2027-02-01"),
            ("", ""),
            ("2020-01-01", "2030-01-01"),
        ):
            with self.assertRaises(periods.PeriodError):
                r("custom", D(2027, 1, 1), *bad)
        with self.assertRaises(periods.PeriodError):
            r("decade", D(2027, 1, 1))


class CollectionsTests(TestCase):
    def setUp(self):
        self.owner = make_owner()

    def test_each_split_component_counts_on_its_own_payment_date(self):
        register(
            self.owner,
            today=D(2026, 11, 11),
            backdate_reason="part paid earlier",
            backdate_confirmed=True,
            subscription_entries=[
                Entry("cash", 30000, paid_on=D(2026, 11, 5)),
                Entry("upi", 50000, paid_on=D(2026, 11, 11), reference="U1"),
            ],
        )
        early = build("collections", D(2026, 11, 1), D(2026, 11, 8))
        self.assertEqual(measure(early, "Totals", "Subscription fees collected"), 30000)
        late = build("collections", D(2026, 11, 9), D(2026, 11, 30))
        self.assertEqual(measure(late, "Totals", "Subscription fees collected"), 50000)
        whole = build("collections", D(2026, 11, 1), D(2026, 11, 30))
        self.assertEqual(measure(whole, "Totals", "Subscription fees collected"), 80000)

    def test_tokens_are_separate_from_subscription_fees(self):
        register(self.owner, today=D(2026, 11, 11))
        report = build("collections", D(2026, 11, 1), D(2026, 11, 30))
        self.assertEqual(measure(report, "Totals", "Subscription fees collected"), 80000)
        self.assertEqual(measure(report, "Totals", "Tokens collected"), 50000)
        self.assertEqual(section(report, "Payment entries").total_row[6], 130000)  # cash moved

    def test_refund_forfeiture_and_adjustments_are_kept_apart(self):
        member = register(self.owner, today=D(2026, 11, 11)).member
        fc.forfeit_token(
            actor=self.owner, member=member, trigger="lost", owner_confirmed=True, reason="r",
            today=D(2026, 12, 1),
        )  # fmt: skip
        report = build("collections", D(2026, 12, 1), D(2026, 12, 31))
        self.assertEqual(
            measure(report, "Totals", "Tokens forfeited (no cash moves; by effective date)"), 50000
        )
        self.assertEqual(measure(report, "Totals", "Subscription fees collected"), 0)
        self.assertEqual(section(report, "Payment entries").rows, [])  # a forfeiture moves no cash
        ms.pay_fresh_token(
            actor=self.owner, member=member, entries=[Entry("cash", 50000)], today=D(2026, 12, 2)
        )
        fc.refund_token(
            actor=self.owner, member=member, entries=[Entry("cash", 50000)], today=D(2026, 12, 3)
        )
        report = build("collections", D(2026, 12, 1), D(2026, 12, 31))
        self.assertEqual(measure(report, "Totals", "Tokens refunded"), -50000)
        self.assertEqual(measure(report, "Totals", "Tokens collected"), 50000)

    def test_an_empty_period_has_zero_totals_and_no_rows(self):
        report = build("collections", D(2030, 1, 1), D(2030, 1, 31))
        self.assertTrue(all(r[1] == 0 for r in section(report, "Totals").rows))
        self.assertIsNone(section(report, "Payment entries").total_row)


class AllocationTests(TestCase):
    def setUp(self):
        self.owner = make_owner()
        self.member = register(
            self.owner, today=D(2026, 11, 11)
        ).member  # 11 Nov 2026 - 10 May 2027

    def test_the_lifetime_allocation_equals_the_price_exactly(self):
        report = build("allocation", D(2026, 1, 1), D(2027, 12, 31))
        total = section(report, "allocated").total_row
        self.assertEqual(total[5], 80000)
        self.assertEqual(total[6], Fraction(80000))  # exact, no rounding drift

    def test_months_sum_exactly_and_no_per_day_rounding(self):
        months = allocate_by_month(80000, D(2026, 11, 11), D(2027, 5, 11))
        self.assertEqual(sum(months.values()), 80000)
        parts = []
        cursor = D(2026, 11, 1)
        for _ in range(7):
            last = (cursor.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
            parts.append(build("allocation", cursor, last).sections[0].total_row)
            cursor = last + timedelta(days=1)
        exact = sum(Fraction(p[6]) for p in parts if p)
        self.assertEqual(exact, 80000)

    def test_a_period_inside_the_subscription_gets_its_pro_rata_share(self):
        report = build("allocation", D(2026, 12, 1), D(2026, 12, 31))
        days = (D(2027, 5, 11) - D(2026, 11, 11)).days
        expected = Fraction(80000 * 31, days)
        self.assertEqual(section(report, "allocated").rows[0][6], expected)
        self.assertEqual(rr.display(expected, "money"), f"{rr.money_decimal(expected):,.2f}")

    def test_tokens_are_never_revenue_and_cancelled_fees_stay(self):
        sub = MemberSubscription.objects.get(member=self.member)
        ms.cancel_subscription(
            actor=self.owner, subscription=sub, reason="moved away", today=D(2026, 12, 1)
        )
        report = build("allocation", D(2026, 11, 1), D(2027, 6, 30))
        self.assertEqual(section(report, "allocated").total_row[6], Fraction(80000))
        self.assertIn("Cancelled (fee kept)", section(report, "allocated").rows[0][7])
        self.assertTrue(any("not revenue" in n for n in report.notes))

    def test_period_outside_the_subscription_lists_nothing(self):
        self.assertEqual(build("allocation", D(2027, 5, 11), D(2027, 6, 1)).sections[0].rows, [])
        self.assertEqual(build("allocation", D(2026, 10, 1), D(2026, 11, 10)).sections[0].rows, [])
        first = build("allocation", D(2026, 11, 11), D(2026, 11, 11)).sections[0].rows
        self.assertEqual(len(first), 1)  # start day is inclusive
        last = build("allocation", D(2027, 5, 10), D(2027, 5, 10)).sections[0].rows
        self.assertEqual(len(last), 1)  # last valid day included, expiry day excluded

    def test_the_rounding_note_is_present_and_totals_round_once(self):
        report = build("allocation", D(2026, 12, 1), D(2026, 12, 31))
        self.assertIn(ROUNDING_NOTE, report.notes)
        body, total = rr.rows_for_screen(report.sections[0])
        self.assertTrue(total[6])

    def test_raw_helper_matches_policy_example(self):
        self.assertEqual(
            allocate(80000, D(2026, 11, 11), D(2027, 5, 11), D(2026, 11, 11), D(2027, 5, 10)), 80000
        )


class AdjustmentReportTests(TestCase):
    def test_unlinked_cash_out_is_listed_apart_and_never_as_a_fee_refund(self):
        owner = make_owner()
        member = register(owner, today=D(2026, 11, 11)).member
        fc.post_adjustment(
            actor=owner,
            member=member,
            direction="out",
            amount_paise=10000,
            reason="duplicate token",
            owner_approved=True,
            confirm_not_fee_refund=True,
            entries=[Entry("cash", 10000)],
            today=D(2026, 12, 1),
        )
        fc.post_adjustment(
            actor=owner, member=member, direction="none", amount_paise=100, reason="note",
            owner_approved=True, today=D(2026, 12, 2),
        )  # fmt: skip
        report = build("adjustments", D(2026, 12, 1), D(2026, 12, 31))
        unlinked = section(report, "Unlinked cash-out")
        other = section(report, "All other")
        self.assertEqual([r[4] for r in unlinked.rows], [10000])
        self.assertIn("NOT subscription-fee refunds", unlinked.title)
        self.assertEqual(unlinked.rows[0][6], "duplicate token")
        self.assertEqual(unlinked.rows[0][7], "owner")
        self.assertEqual([r[4] for r in other.rows], [100])
        self.assertEqual(unlinked.total_row[4], 10000)


class LoanReportTests(TestCase):
    def setUp(self):
        self.owner = make_owner()
        self.member, self.copy, self.loan = overdue_loan(self.owner)  # issued 1 Dec, due 31 Dec

    def test_outstanding_and_overdue_as_at_a_date(self):
        on_time = build("loans", D(2026, 12, 1), D(2026, 12, 20), today=D(2027, 6, 1))
        self.assertEqual(on_time.sections[0].rows[0][-1], "On loan")
        late = build("loans", D(2026, 12, 1), D(2027, 1, 5), today=D(2027, 6, 1))
        self.assertEqual(
            (late.sections[0].rows[0][-1], late.sections[0].rows[0][5]), ("Overdue", 5)
        )
        self.assertEqual(
            build("loans", D(2026, 11, 1), D(2026, 11, 30), today=D(2027, 6, 1)).sections[0].rows,
            [],
        )

    def test_a_returned_loan_drops_out_after_its_return_date(self):
        services.return_loan(actor=self.owner, loan=self.loan, today=D(2027, 1, 2))
        before = build("loans", D(2026, 12, 1), D(2027, 1, 1), today=D(2027, 6, 1))
        after = build("loans", D(2026, 12, 1), D(2027, 1, 2), today=D(2027, 6, 1))
        self.assertEqual(len(before.sections[0].rows), 1)
        self.assertEqual(after.sections[0].rows, [])

    def test_the_report_date_never_runs_past_today(self):
        report = build("loans", D(2026, 12, 1), D(2030, 1, 1), today=D(2026, 12, 20))
        self.assertIn("20 Dec 2026", report.subtitle)


class OtherReportTests(TestCase):
    def setUp(self):
        self.owner = make_owner()

    def test_lost_and_damaged(self):
        member, copy, loan = overdue_loan(self.owner)
        services.mark_lost(
            actor=self.owner, loan=loan, owner_confirmed=True, reason="gone", today=D(2027, 1, 5)
        )
        report = build("lost_damaged", D(2027, 1, 1), D(2027, 1, 31))
        self.assertEqual(section(report, "currently").rows[0][0], copy.copy_code)
        self.assertEqual(
            section(report, "closed as lost")[0]
            if False
            else section(report, "closed as lost").rows[0][4],
            "Lost",
        )
        self.assertEqual(build("lost_damaged", D(2027, 2, 1), D(2027, 2, 28)).sections[1].rows, [])

    def test_inventory_counts_editions_and_copies_apart(self):
        make_copies(self.owner, 3, title="Three Copies")
        report = build("inventory", D(2026, 12, 1), D(2026, 12, 31))
        inv = section(report, "Inventory")
        self.assertEqual([r[1] for r in inv.rows[:2]], [1, 3])
        self.assertEqual(dict(inv.rows)["Copies available"], 3)

    def test_circulation_counts_in_the_period(self):
        member, copy, loan = overdue_loan(self.owner)
        services.renew_loan(actor=self.owner, loan=loan, today=D(2026, 12, 20))
        services.return_loan(actor=self.owner, loan=loan, today=D(2026, 12, 22))
        circ = section(build("inventory", D(2026, 12, 1), D(2026, 12, 31)), "Circulation")
        self.assertEqual(dict(circ.rows)["Loans issued"], 1)
        self.assertEqual(dict(circ.rows)["Renewals"], 1)
        self.assertEqual(dict(circ.rows)["Books returned"], 1)
        self.assertEqual(
            dict(section(build("inventory", D(2027, 2, 1), D(2027, 2, 28)), "Circulation").rows)[
                "Loans issued"
            ],
            0,
        )

    def test_borrowing_statistics(self):
        member, copy, loan = overdue_loan(self.owner)
        services.return_loan(actor=self.owner, loan=loan, today=D(2027, 1, 4))  # 34 days, late
        report = build("borrowing", D(2026, 12, 1), D(2027, 1, 31))
        self.assertEqual(
            section(report, "Loans per month").rows, [["2026-12", 1, 0], ["2027-01", 0, 1]]
        )
        self.assertEqual(section(report, "Average days").rows[0][1], Fraction(34))
        self.assertEqual(section(report, "Overdue rate").rows[0][1], Fraction(100))
        self.assertEqual(section(report, "Top 10 titles").rows, [["B-9876543210", 1]])
        self.assertEqual(rr.display(Fraction(34), "number"), "34.0")
        self.assertEqual(rr.display(Fraction(100, 3), "percent"), "33.3%")

    def test_borrowing_with_no_loans_is_safe(self):
        report = build("borrowing", D(2026, 12, 1), D(2026, 12, 31))
        self.assertEqual(section(report, "Overdue rate").rows[0][1], 0)

    def test_members_report_shows_anonymised_members_without_their_name(self):
        member = member_at(self.owner, name="Zainab Qureshi", number="9811100077")
        members.anonymise_member(member, actor=self.owner, reason="r", confirm=True)
        report = build("members", D(2026, 11, 1), D(2026, 11, 30))
        text = " ".join(str(c) for s in report.sections for r in s.rows for c in r)
        self.assertNotIn("Zainab", text)
        self.assertNotIn("9811100077", text)
        self.assertIn("Anonymised member", text)
        self.assertIn("COV-M-000001", text)


class ViewTests(TestCase):
    def setUp(self):
        self.owner = make_owner()
        self.client.login(username="owner", password=PW)
        register(self.owner, today=D(2026, 11, 11))

    def url(self, key, **q):
        q.setdefault("preset", "custom")
        q.setdefault("start", "2026-11-01")
        q.setdefault("end", "2026-11-30")
        return (
            reverse("reports:report", args=[key]) + "?" + "&".join(f"{k}={v}" for k, v in q.items())
        )

    def test_every_report_renders(self):
        self.assertEqual(self.client.get(reverse("reports:index")).status_code, 200)
        for key in queries.REPORTS:
            response = self.client.get(self.url(key))
            self.assertEqual(response.status_code, 200, key)
            self.assertContains(
                response, queries.build(key, D(2026, 11, 1), D(2026, 11, 30), D(2026, 11, 30)).title
            )

    def test_login_is_required_everywhere(self):
        self.client.logout()
        for url in (
            reverse("reports:index"),
            self.url("collections"),
            self.url("collections", format="csv"),
            self.url("collections", format="pdf"),
        ):
            self.assertEqual(self.client.get(url).status_code, 302, url)

    def test_downloads_have_the_right_types_and_are_audited_without_personal_data(self):
        for fmt, ctype, magic in (
            ("csv", "text/csv", b"\xef\xbb\xbf"),
            ("xlsx", "spreadsheetml.sheet", b"PK"),
            ("pdf", "application/pdf", b"%PDF"),
        ):
            response = self.client.get(self.url("collections", format=fmt))
            self.assertEqual(response.status_code, 200, fmt)
            self.assertIn(ctype, response["Content-Type"])
            self.assertTrue(response.content.startswith(magic), fmt)
            self.assertIn("attachment", response["Content-Disposition"])
            self.assertEqual(response["Cache-Control"], "no-store")
        events = AuditEvent.objects.filter(action="report.export")
        self.assertEqual(events.count(), 3)
        event = events.first()
        self.assertEqual(event.summary["from"], "2026-11-01")
        self.assertNotIn("Asha", str(event.summary))

    def test_pdf_exists_only_for_the_two_revenue_reports(self):
        for key in ("collections", "allocation"):
            self.assertEqual(self.client.get(self.url(key, format="pdf")).status_code, 200, key)
        for key in set(queries.REPORTS) - {"collections", "allocation"}:
            self.assertEqual(self.client.get(self.url(key, format="pdf")).status_code, 404, key)
            self.assertEqual(self.client.get(self.url(key, format="csv")).status_code, 200, key)
            self.assertEqual(self.client.get(self.url(key, format="xlsx")).status_code, 200, key)

    def test_bad_input(self):
        self.assertEqual(self.client.get(reverse("reports:report", args=["nope"])).status_code, 404)
        self.assertEqual(self.client.get(self.url("collections", format="exe")).status_code, 404)
        page = self.client.get(self.url("collections", start="2026-12-01", end="2026-11-01"))
        self.assertContains(page, "must not be after")
        bad = self.client.get(self.url("collections", start="junk", format="csv"))
        self.assertEqual(bad.status_code, 200)  # an invalid range never downloads a wrong file
        self.assertNotIn("attachment", bad.get("Content-Disposition", ""))
        self.assertEqual(AuditEvent.objects.filter(action="report.export").count(), 0)

    def test_the_nav_links_reports(self):
        self.assertContains(self.client.get(reverse("core:dashboard")), reverse("reports:index"))


class FormulaEscapingTests(TestCase):
    def setUp(self):
        self.owner = make_owner()
        self.client.login(username="owner", password=PW)

    def test_dangerous_text_is_neutralised_in_csv_and_xlsx(self):
        nasty = '=HYPERLINK("http://evil","x")'
        register(self.owner, name=nasty, today=D(2026, 11, 11))
        for value in ("+SUM(1)", "-2+3", "@cmd", "\tTAB", "\rCR"):
            self.assertTrue(rr.safe_text(value).startswith("'"), value)
        self.assertEqual(rr.safe_text("safe"), "safe")
        self.assertEqual(rr.safe_text(None), "")
        report = build("members", D(2026, 11, 1), D(2026, 11, 30))
        csv_text = rr.to_csv(report).decode("utf-8-sig")
        self.assertIn("'=HYPERLINK", csv_text)
        self.assertNotIn("\r\n=HYPERLINK", csv_text)
        self.assertNotIn(",=HYPERLINK", csv_text)
        wb = load_workbook(io.BytesIO(rr.to_xlsx(report)))
        found = False
        for ws in wb.worksheets:
            for row in ws.iter_rows():
                for cell in row:
                    if isinstance(cell.value, str) and "HYPERLINK" in cell.value:
                        found = True
                        self.assertTrue(cell.value.startswith("'"))
                        self.assertEqual(cell.data_type, "s")
        self.assertTrue(found)

    def test_negative_money_is_a_number_not_escaped_text(self):
        member = register(self.owner, today=D(2026, 11, 11)).member
        fc.forfeit_token(
            actor=self.owner,
            member=member,
            trigger="lost",
            owner_confirmed=True,
            reason="r",
            today=D(2026, 12, 1),
        )
        ms.pay_fresh_token(
            actor=self.owner, member=member, entries=[Entry("cash", 50000)], today=D(2026, 12, 2)
        )
        fc.refund_token(
            actor=self.owner, member=member, entries=[Entry("cash", 50000)], today=D(2026, 12, 3)
        )
        report = build("collections", D(2026, 12, 1), D(2026, 12, 31))
        csv_text = rr.to_csv(report).decode("utf-8-sig")
        self.assertIn("-500.00", csv_text)
        self.assertNotIn("'-500.00", csv_text)
        wb = load_workbook(io.BytesIO(rr.to_xlsx(report)))
        values = [c.value for ws in wb.worksheets for row in ws.iter_rows() for c in row]
        self.assertIn(-500.0, values)

    def test_xlsx_values_and_formats(self):
        register(self.owner, today=D(2026, 11, 11))
        report = build("allocation", D(2026, 12, 1), D(2026, 12, 31))
        wb = load_workbook(io.BytesIO(rr.to_xlsx(report)))
        ws = wb.worksheets[0]
        self.assertEqual(ws.cell(row=5, column=7).value, "Allocated to period")
        self.assertEqual(ws.cell(row=6, column=7).number_format, "#,##0.00")
