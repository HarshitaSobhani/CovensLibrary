"""Phase 6 corrections: strict phone exemption, stronger formula escaping (D-28h) and masking of
member-linked free text for anonymised members (D-28i)."""

import csv
import io
import zipfile

from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from openpyxl import load_workbook

from catalogue import services as catalogue
from catalogue.models import Category, CopyEvent
from circulation import grace, services
from circulation.models import PendingForfeiture
from circulation.tests.helpers import D, Entry, make_copies, make_owner, member_at, overdue_loan
from finance import corrections as fc
from finance.models import FinancialTransaction, TokenLedgerEntry
from members import services as members
from members.models import Member
from memberships.tests.helpers import PW
from reports import export, masking, queries, statement
from reports import render as rr

PHONES = ["+919811100077", "+1 202 555 0147", "+44.20.7946.0958", "+91 98111 00077", "+1"]
NOT_PHONES = [
    "+", "+ ", "+.", "+1=2", "+cmd", "+91a", "+1\n", " +919811100077", "+(91)98111",
    "-919811100077", "++91", "+91-98111", "+91\t98", "+1+1",
]  # fmt: skip
DANGEROUS = [
    "=1+1", "+SUM(1)", "-2+3", "@cmd", "-5", "-12.50",
    " =1+1", "\n=1+1", "\x00=1", "\xa0=1+1", "\t=1", "\r=1", "  +1", "\t =1", " \n\t=SUM(1)",
    "\x00 \xa0@x", "​=1", "﻿=1", "　-2", "\x1f=1", "\x7f+1", "\n \r@A1", "\r\n=1",
    " =1", " +1", "\x0b=1", "\x0c@1", "\x85=1",
]  # fmt: skip
SAFE = ["ok", " hello", "a=b", "x +1", "1+1", "", " ", "\n", "Café", "(=)", "'=1", "#=1"]


class PhoneExemptionTests(SimpleTestCase):
    def test_strict_phone_numbers_are_unchanged(self):
        for value in PHONES:
            self.assertEqual(rr.safe_text(value), value, value)

    def test_nothing_else_beginning_with_a_plus_is_exempt(self):
        for value in NOT_PHONES:
            out = rr.safe_text(value)
            self.assertEqual(out, "'" + value, repr(value))


class FormulaEscapingTests(SimpleTestCase):
    def test_every_dangerous_prefix_is_escaped_and_the_text_is_kept_exactly(self):
        for value in DANGEROUS:
            self.assertEqual(rr.safe_text(value), "'" + value, repr(value))

    def test_safe_text_is_untouched(self):
        for value in SAFE:
            self.assertEqual(rr.safe_text(value), value, repr(value))
        self.assertEqual(rr.safe_text(None), "")
        self.assertEqual(rr.safe_text(0), "0")

    def test_escaping_is_not_applied_twice_in_a_row_by_accident(self):
        once = rr.safe_text("=1")
        self.assertEqual(once, "'=1")
        self.assertEqual(rr.safe_text(once), "'=1")  # starts with an apostrophe: not dangerous


class OutputsKeepPhonesAndEscapeTextTests(TestCase):
    def setUp(self):
        self.owner = make_owner()
        self.client.login(username="owner", password=PW)
        self.member = member_at(self.owner, name="Asha Rao", number="9811100077")  # +919811100077

    def test_a_phone_is_exact_in_the_members_report_csv_xlsx_statement_and_zip(self):
        report = queries.build("members", D(2026, 11, 1), D(2026, 11, 30), D(2026, 11, 30))
        self.assertIn("+919811100077", rr.to_csv(report).decode("utf-8-sig"))
        self.assertNotIn("'+919811100077", rr.to_csv(report).decode("utf-8-sig"))
        wb = load_workbook(io.BytesIO(rr.to_xlsx(report)))
        values = [c.value for ws in wb.worksheets for row in ws.iter_rows() for c in row]
        self.assertIn("+919811100077", values)
        self.assertNotIn("'+919811100077", values)
        data = statement.build(self.member, None, None, D(2026, 12, 2))
        self.assertIn("+919811100077", rr.to_csv(data).decode("utf-8-sig"))
        self.assertNotIn("'+919811100077", rr.to_csv(data).decode("utf-8-sig"))
        zbytes, _ = export.build_zip()
        text = zipfile.ZipFile(io.BytesIO(zbytes)).read("members.csv").decode("utf-8-sig")
        self.assertIn(",+919811100077,", text)
        self.assertNotIn("'+919811100077", text)
        xlsx = load_workbook(
            io.BytesIO(zipfile.ZipFile(io.BytesIO(zbytes)).read("coven_export.xlsx"))
        )
        self.assertEqual(xlsx["members"].cell(row=2, column=3).value, "+919811100077")

    def test_dangerous_text_with_leading_whitespace_is_escaped_in_every_renderer(self):
        from reports.core import TEXT, Column, Report, Section

        nasty = [" =HYPERLINK(1)", "\n=1+1", "\x00=1", "\xa0@x", "\t =SUM(1)"]
        report = Report("t", "T", "", [Section("S", [Column("Value", TEXT)], [[v] for v in nasty])])
        csv_rows = list(csv.reader(io.StringIO(rr.to_csv(report).decode("utf-8-sig"))))
        written = [r[0] for r in csv_rows if r and r[0] in {"'" + v for v in nasty}]
        self.assertEqual(sorted(written), sorted("'" + v for v in nasty))
        wb = load_workbook(io.BytesIO(rr.to_xlsx(report)))
        cells = [c.value for ws in wb.worksheets for row in ws.iter_rows() for c in row]
        for v in nasty:
            self.assertIn("'" + rr.xlsx_clean(v), cells)
        pdf = rr.to_pdf(report)
        self.assertTrue(pdf.startswith(b"%PDF"))

    def test_control_characters_never_crash_an_xlsx_export_and_stay_visible(self):
        from reports.core import TEXT, Column, Report, Section

        self.assertEqual(rr.xlsx_clean("a\x00b\x1fc\x0bd"), "a\u2400b\u241fc\u240bd")
        self.assertEqual(rr.xlsx_clean("tab\tnl\ncr\rok"), "tab\tnl\ncr\rok")  # allowed
        report = Report(
            "t", "T\x01", "", [Section("S\x02", [Column("V\x03", TEXT)], [["x\x04y"], ["=\x05"]])]
        )
        wb = load_workbook(io.BytesIO(rr.to_xlsx(report)))
        cells = [c.value for ws in wb.worksheets for row in ws.iter_rows() for c in row if c.value]
        self.assertIn("x\u2404y", cells)
        self.assertIn("'=\u2405", cells)
        self.assertIn("x\x04y", rr.to_csv(report).decode("utf-8-sig"))  # CSV keeps the original
        Member.objects.filter(pk=self.member.pk).update(notes="has\x07bell")
        fc.post_adjustment(
            actor=self.owner, member=self.member, direction="none", amount_paise=100,
            reason="bell\x07 reason", owner_approved=True, today=D(2026, 12, 4),
        )  # fmt: skip
        zbytes, _ = export.build_zip()  # must not raise
        z = zipfile.ZipFile(io.BytesIO(zbytes))
        sheet = load_workbook(io.BytesIO(z.read("coven_export.xlsx")))["financial_transactions"]
        self.assertTrue(
            any(c.value == "bell\u2407 reason" for row in sheet.iter_rows() for c in row)
        )
        self.assertIn("bell\x07 reason", z.read("financial_transactions.csv").decode("utf-8-sig"))

    def test_a_reason_starting_with_a_formula_is_escaped_in_report_and_export(self):
        fc.post_adjustment(
            actor=self.owner, member=self.member, direction="none", amount_paise=100,
            reason="=SUM(A1)", owner_approved=True, today=D(2026, 12, 3),
        )  # fmt: skip
        report = queries.build("adjustments", D(2026, 12, 1), D(2026, 12, 31), D(2026, 12, 31))
        self.assertIn("'=SUM(A1)", rr.to_csv(report).decode("utf-8-sig"))
        zbytes, _ = export.build_zip()
        z = zipfile.ZipFile(io.BytesIO(zbytes))
        rows = list(
            csv.reader(io.StringIO(z.read("financial_transactions.csv").decode("utf-8-sig")))
        )
        self.assertIn("'=SUM(A1)", [r[rows[0].index("reason")] for r in rows[1:]])


class MaskingTests(TestCase):
    """Reasons typed about a member are masked once the member is anonymised; the records
    themselves are never changed."""

    LOST = "R-LOST Zainab said she lost it"
    DECLINE = "R-DECLINE Zainab was ill"
    ADJ = "R-ADJ Zainab duplicate token"
    LIVE = "R-LIVE Asha kept this"
    LIVE_ADJ = "R-LIVEADJ Asha adjustment"

    def setUp(self):
        self.owner = make_owner()
        self.client.login(username="owner", password=PW)
        # member A: lost book, an adjustment
        self.a, self.copy_a, self.loan_a = overdue_loan(
            self.owner, name="Zainab Qureshi", number="9811100071"
        )
        services.mark_lost(
            actor=self.owner,
            loan=self.loan_a,
            owner_confirmed=True,
            reason=self.LOST,
            today=D(2027, 1, 5),
        )
        fc.post_adjustment(
            actor=self.owner, member=self.a, direction="none", amount_paise=100,
            reason=self.ADJ, owner_approved=True, today=D(2027, 1, 6),
        )  # fmt: skip
        # member B: a declined forfeiture decision
        self.b, self.copy_b, self.loan_b = overdue_loan(
            self.owner, name="Bilal Khan", number="9811100072"
        )
        grace.change_grace_days(actor=self.owner, new_days=3, today=D(2027, 1, 5))
        grace.decline_pending(
            actor=self.owner,
            pending=PendingForfeiture.objects.get(loan=self.loan_b),
            reason=self.DECLINE,
        )
        # member C stays live
        self.c, self.copy_c, self.loan_c = overdue_loan(
            self.owner, name="Asha Rao", number="9811100073"
        )
        services.mark_lost(
            actor=self.owner,
            loan=self.loan_c,
            owner_confirmed=True,
            reason=self.LIVE,
            today=D(2027, 1, 5),
        )
        fc.post_adjustment(
            actor=self.owner, member=self.c, direction="none", amount_paise=100,
            reason=self.LIVE_ADJ, owner_approved=True, today=D(2027, 1, 6),
        )  # fmt: skip
        # donor
        edition = catalogue.create_edition(
            actor=self.owner, title="Donated Book", category=Category.objects.first()
        )
        catalogue.add_copies(
            edition,
            1,
            actor=self.owner,
            source="donated",
            donor_name="Donor Dan",
            donor_contact="dan@example.com",
        )

    def anonymise(self, *members_):
        for m in members_:
            services.return_loan(
                actor=self.owner, loan=m.loans.first(), today=D(2027, 1, 6)
            ) if m.loans.filter(status="active").exists() else None
            members.anonymise_member(m, actor=self.owner, reason="request", confirm=True)

    def zip_text(self):
        z = zipfile.ZipFile(io.BytesIO(export.build_zip()[0]))
        return {n: z.read(n).decode("utf-8", "ignore") for n in z.namelist() if n.endswith(".csv")}

    def test_before_anonymisation_everything_is_visible(self):
        text = "\n".join(self.zip_text().values())
        for reason in (self.LOST, self.DECLINE, self.ADJ, self.LIVE):
            self.assertIn(reason, text)

    def test_the_masking_helper(self):
        self.assertEqual(masking.mask(None, "x"), "x")
        self.assertEqual(masking.mask(D(2027, 1, 1), "x"), masking.REDACTED)
        self.assertEqual(masking.mask(D(2027, 1, 1), ""), "")
        self.assertEqual(masking.REDACTED, "Redacted — member anonymised")

    def test_statement_adjustments_and_zip_mask_anonymised_members_only(self):
        self.anonymise(self.a, self.b)
        member_a = type(self.a).objects.get(pk=self.a.pk)
        # statement
        sheet = statement.build(member_a, None, None, D(2027, 2, 1))
        money = next(s for s in sheet.sections if s.title.startswith("Payments"))
        self.assertIn(masking.REDACTED, [r[6] for r in money.rows])
        blob = rr.to_csv(sheet).decode("utf-8-sig")
        for reason in (self.LOST, self.ADJ):
            self.assertNotIn(reason, blob)
        # adjustments report in every format
        adj = queries.build("adjustments", D(2027, 1, 1), D(2027, 1, 31), D(2027, 2, 1))
        for rendered in (rr.to_csv(adj).decode("utf-8-sig"), str([s.rows for s in adj.sections])):
            self.assertNotIn(self.ADJ, rendered)
            self.assertIn(masking.REDACTED, rendered)
        # the full export
        files = self.zip_text()
        everything = "\n".join(files.values())
        for reason in (self.LOST, self.DECLINE, self.ADJ):
            self.assertEqual([n for n, t in files.items() if reason in t], [], reason)
        self.assertIn(self.LIVE, everything)  # the live member's text stays
        self.assertIn(masking.REDACTED, files["financial_transactions.csv"])
        self.assertIn(masking.REDACTED, files["pending_forfeitures.csv"])
        self.assertIn(masking.REDACTED, files["token_ledger.csv"])

    def test_nothing_leaks_through_the_xlsx_or_pdf_routes(self):
        self.anonymise(self.a, self.b)
        zbytes = export.build_zip()[0]
        xlsx = load_workbook(
            io.BytesIO(zipfile.ZipFile(io.BytesIO(zbytes)).read("coven_export.xlsx"))
        )
        values = [
            str(c.value) for ws in xlsx.worksheets for row in ws.iter_rows() for c in row if c.value
        ]
        for reason in (self.LOST, self.DECLINE, self.ADJ):
            self.assertFalse([v for v in values if reason in v], reason)
        member_a = type(self.a).objects.get(pk=self.a.pk)
        for fmt in ("pdf", "xlsx", "csv"):
            response = self.client.get(
                reverse("reports:statement", args=[member_a.pk]) + f"?format={fmt}"
            )
            self.assertEqual(response.status_code, 200)
            self.assertNotIn(b"R-ADJ", response.content)
            self.assertNotIn(b"R-LOST", response.content)
        page = self.client.get(reverse("reports:statement", args=[member_a.pk])).content.decode()
        self.assertNotIn("R-ADJ", page)
        self.assertIn("Redacted", page)

    def test_the_underlying_records_are_unchanged(self):
        self.anonymise(self.a, self.b)
        self.assertTrue(FinancialTransaction.objects.filter(reason=self.ADJ).exists())
        self.assertTrue(FinancialTransaction.objects.filter(reason=self.LOST).exists())
        self.assertTrue(TokenLedgerEntry.objects.filter(reason=self.LOST).exists())
        self.assertTrue(PendingForfeiture.objects.filter(decline_reason=self.DECLINE).exists())

    def test_copy_event_notes_are_masked_only_for_anonymised_members(self):
        for loan in (self.loan_a, self.loan_c):
            self.assertTrue(
                CopyEvent.objects.filter(loan=loan).exclude(note="").exists(),
                "fixture needs a note",
            )
        before = self.zip_text()["copy_events.csv"]
        notes_a = list(
            CopyEvent.objects.filter(loan=self.loan_a)
            .exclude(note="")
            .values_list("note", flat=True)
        )
        notes_c = list(
            CopyEvent.objects.filter(loan=self.loan_c)
            .exclude(note="")
            .values_list("note", flat=True)
        )
        self.assertTrue(all(n in before for n in notes_a + notes_c))
        self.anonymise(self.a)
        after = self.zip_text()["copy_events.csv"]
        self.assertTrue(all(n not in after for n in notes_a))
        self.assertIn(masking.REDACTED, after)
        self.assertTrue(all(n in after for n in notes_c))
        self.assertTrue(CopyEvent.objects.filter(loan=self.loan_a, note=notes_a[0]).exists())

    def test_donor_names_are_not_exported_but_stay_stored(self):
        files = self.zip_text()
        self.assertNotIn("Donor Dan", "\n".join(files.values()))
        self.assertNotIn("dan@example.com", "\n".join(files.values()))
        self.assertNotIn("donor_name", files["acquisitions.csv"].splitlines()[0])
        from catalogue.models import Acquisition

        self.assertTrue(Acquisition.objects.filter(donor_name="Donor Dan").exists())
        self.assertTrue(any("donor" in line.lower() for line in export.EXCLUDED))

    def test_live_members_statement_keeps_its_reasons(self):
        sheet = statement.build(self.c, None, None, D(2027, 2, 1))
        money = next(s for s in sheet.sections if s.title.startswith("Payments"))
        self.assertIn(self.LIVE_ADJ, [r[6] for r in money.rows])


class ReferenceBackdateConfirmMaskingTests(TestCase):
    """D-28i, second pass: payment-entry references, backdate reasons and forfeiture
    confirmation reasons are masked for anonymised members and kept for live ones."""

    REF_A, REF_L = "UPI-ZAINAB-77", "UPI-LIVE-99"
    BACK_A, BACK_L = "BACKDATE-A Zainab paid early", "BACKDATE-L Asha paid early"
    CONF_A, CONF_L = "CONFIRM-A Zainab explained", "CONFIRM-L Asha explained"

    def setUp(self):
        from memberships.tests.helpers import register

        self.owner = make_owner()
        self.client.login(username="owner", password=PW)

        def reg(name, number, ref, back):
            return register(
                self.owner,
                name=name,
                number=number,
                today=D(2026, 11, 11),
                payment_date=D(2026, 11, 5),
                backdate_reason=back,
                backdate_confirmed=True,
                subscription_entries=[
                    Entry("cash", 30000, paid_on=D(2026, 11, 5)),
                    Entry("upi", 50000, paid_on=D(2026, 11, 5), reference=ref),
                ],
            ).member

        self.a = reg("Zainab Qureshi", "9811100081", self.REF_A, self.BACK_A)
        self.l = reg("Asha Rao", "9811100082", self.REF_L, self.BACK_L)
        from catalogue.models import BookCopy
        from circulation import services as loans

        copies = make_copies(self.owner, 2, title="Shared")
        self.loan_a = loans.issue_loan(
            actor=self.owner, member=self.a, copy=copies[0], today=D(2026, 12, 1)
        )
        self.loan_l = loans.issue_loan(
            actor=self.owner, member=self.l, copy=copies[1], today=D(2026, 12, 1)
        )
        grace.change_grace_days(actor=self.owner, new_days=3, today=D(2027, 1, 5))
        for loan, reason in ((self.loan_a, self.CONF_A), (self.loan_l, self.CONF_L)):
            grace.confirm_pending(
                actor=self.owner,
                pending=PendingForfeiture.objects.get(loan=loan),
                reason=reason,
                today=D(2027, 1, 5),
            )
        for loan in (self.loan_a,):
            services.return_loan(actor=self.owner, loan=loan, today=D(2027, 1, 6))
        members.anonymise_member(self.a, actor=self.owner, reason="request", confirm=True)
        self.assertTrue(BookCopy.objects.exists())

    def zip_files(self):
        z = zipfile.ZipFile(io.BytesIO(export.build_zip()[0]))
        return z, {n: z.read(n).decode("utf-8-sig") for n in z.namelist() if n.endswith(".csv")}

    def collections(self):
        return queries.build("collections", D(2026, 11, 1), D(2026, 11, 30), D(2026, 12, 1))

    def test_the_collections_report_masks_the_anonymised_reference_on_screen_and_in_files(self):
        report = self.collections()
        rows = report.sections[1].rows
        refs = [r[5] for r in rows]
        self.assertIn(self.REF_L, refs)
        self.assertNotIn(self.REF_A, refs)
        self.assertIn(masking.REDACTED, refs)
        page = self.client.get(
            reverse("reports:report", args=["collections"])
            + "?preset=custom&start=2026-11-01&end=2026-11-30"
        )
        text = page.content.decode()
        self.assertNotIn(self.REF_A, text)
        self.assertIn(masking.REDACTED, text)
        self.assertIn(self.REF_L, text)
        csv_text = rr.to_csv(report).decode("utf-8-sig")
        self.assertNotIn(self.REF_A, csv_text)
        self.assertIn(self.REF_L, csv_text)
        wb = load_workbook(io.BytesIO(rr.to_xlsx(report)))
        cells = [
            str(c.value) for ws in wb.worksheets for row in ws.iter_rows() for c in row if c.value
        ]
        self.assertFalse([c for c in cells if self.REF_A in c])
        self.assertIn(self.REF_L, cells)
        for fmt in ("csv", "xlsx", "pdf"):
            response = self.client.get(
                reverse("reports:report", args=["collections"])
                + f"?preset=custom&start=2026-11-01&end=2026-11-30&format={fmt}"
            )
            self.assertEqual(response.status_code, 200)
            self.assertNotIn(self.REF_A.encode(), response.content)

    def test_the_full_export_masks_references_backdate_and_confirmation_reasons(self):
        z, files = self.zip_files()
        everything = "\n".join(files.values())
        for original in (self.REF_A, self.BACK_A, self.CONF_A):
            self.assertEqual([n for n, t in files.items() if original in t], [], original)
        for kept in (self.REF_L, self.BACK_L, self.CONF_L):
            self.assertIn(kept, everything)
        self.assertIn(masking.REDACTED, files["payment_entries.csv"])
        self.assertIn(masking.REDACTED, files["financial_transactions.csv"])
        self.assertIn(masking.REDACTED, files["pending_forfeitures.csv"])
        wb = load_workbook(io.BytesIO(z.read("coven_export.xlsx")))
        values = [
            str(c.value) for ws in wb.worksheets for row in ws.iter_rows() for c in row if c.value
        ]
        for original in (self.REF_A, self.BACK_A, self.CONF_A):
            self.assertFalse([v for v in values if original in v], original)
        for kept in (self.REF_L, self.BACK_L, self.CONF_L):
            self.assertTrue([v for v in values if kept in v], kept)

    def test_masking_is_per_row_and_the_originals_stay_in_the_database(self):
        from finance.models import PaymentEntry

        self.assertTrue(PaymentEntry.objects.filter(reference=self.REF_A).exists())
        self.assertTrue(FinancialTransaction.objects.filter(backdate_reason=self.BACK_A).exists())
        self.assertTrue(PendingForfeiture.objects.filter(confirm_reason=self.CONF_A).exists())
        rows = list(csv.reader(io.StringIO(self.zip_files()[1]["payment_entries.csv"])))
        ref = rows[0].index("reference")
        values = [r[ref] for r in rows[1:]]
        # A has two referenced entries (the subscription UPI part and the token's "U1"): masked.
        # L's two referenced entries stay readable. Entries without a reference stay empty.
        self.assertEqual(values.count(masking.REDACTED), 2)
        self.assertEqual(
            sorted(v for v in values if v),
            sorted([self.REF_L, "U1", masking.REDACTED, masking.REDACTED]),
        )

    def test_the_statement_and_other_reports_do_not_expose_the_original_values(self):
        member_a = type(self.a).objects.get(pk=self.a.pk)
        sheet = statement.build(member_a, None, None, D(2027, 2, 1))
        blob = rr.to_csv(sheet).decode("utf-8-sig")
        for original in (self.REF_A, self.BACK_A, self.CONF_A):
            self.assertNotIn(original, blob)
        for key in queries.REPORTS:
            report = queries.build(key, D(2026, 1, 1), D(2027, 12, 31), D(2027, 2, 1))
            text = rr.to_csv(report).decode("utf-8-sig")
            for original in (self.REF_A, self.BACK_A, self.CONF_A):
                self.assertNotIn(original, text, key)


class PdfMaskingTests(TestCase):
    """PDF text is only readable with stream compression off, so these tests inspect UNCOMPRESSED
    output: a check on compressed bytes could never fail."""

    REF_A, REF_L = "UPI-ZAINAB-77", "UPI-LIVE-99"
    REASON_A, REASON_L = "R-ANON-PDF Zainab note", "R-LIVE-PDF Asha note"

    def setUp(self):
        from memberships.tests.helpers import register

        self.owner = make_owner()
        self.client.login(username="owner", password=PW)

        def reg(name, number, ref, reason):
            member = register(
                self.owner,
                name=name,
                number=number,
                today=D(2026, 11, 11),
                subscription_entries=[Entry("cash", 30000), Entry("upi", 50000, reference=ref)],
            ).member
            fc.post_adjustment(
                actor=self.owner, member=member, direction="none", amount_paise=100,
                reason=reason, owner_approved=True, today=D(2026, 11, 12),
            )  # fmt: skip
            return member

        self.a = reg("Zainab Qureshi", "9811100091", self.REF_A, self.REASON_A)
        self.l = reg("Asha Rao", "9811100092", self.REF_L, self.REASON_L)
        members.anonymise_member(self.a, actor=self.owner, reason="request", confirm=True)
        self.a = type(self.a).objects.get(pk=self.a.pk)

    def pdf(self, url):
        from unittest import mock

        from reportlab import rl_config

        with mock.patch.object(rl_config, "pageCompression", 0):
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200, url)
        self.assertTrue(response.content.startswith(b"%PDF"))
        return response.content

    def statement(self, member):
        return reverse("reports:statement", args=[member.pk]) + "?format=pdf"

    def collections(self):
        return reverse("reports:report", args=["collections"]) + (
            "?preset=custom&start=2026-11-01&end=2026-11-30&format=pdf"
        )

    def test_the_helper_really_exposes_pdf_text(self):
        live = self.pdf(self.statement(self.l))
        self.assertIn(b"Asha Rao", live)  # guard: absence checks below are meaningful
        self.assertIn(b"R-LIVE-PDF", live)
        compressed = self.client.get(self.statement(self.l)).content
        self.assertNotIn(b"R-LIVE-PDF", compressed)  # which is why compressed bytes prove nothing

    def test_the_anonymised_statement_pdf_has_no_name_reference_or_reason(self):
        text = self.pdf(self.statement(self.a))
        for original in (b"Zainab", b"Qureshi", self.REF_A.encode(), b"R-ANON-PDF", b"9811100091"):
            self.assertNotIn(original, text, original)
        self.assertIn(b"Anonymised member", text)
        self.assertIn(b"Redacted", text)
        self.assertIn(self.a.membership_no.encode(), text)

    def test_the_live_statement_pdf_keeps_its_permitted_information(self):
        text = self.pdf(self.statement(self.l))
        for kept in (b"Asha Rao", b"+919811100092", b"R-LIVE-PDF", self.l.membership_no.encode()):
            self.assertIn(kept, text, kept)
        self.assertNotIn(b"Redacted", text)

    def test_the_collections_pdf_masks_the_anonymised_reference_and_keeps_the_live_one(self):
        text = self.pdf(self.collections())
        self.assertNotIn(self.REF_A.encode(), text)
        self.assertNotIn(b"Zainab", text)
        self.assertIn(b"Redacted", text)
        self.assertIn(self.REF_L.encode(), text)
        self.assertIn(self.l.membership_no.encode(), text)

    def test_the_allocation_pdf_has_no_anonymised_name_or_reference(self):
        url = reverse("reports:report", args=["allocation"]) + "?preset=year&format=pdf"
        text = self.pdf(url)
        for original in (b"Zainab", self.REF_A.encode(), b"R-ANON-PDF"):
            self.assertNotIn(original, text, original)
        self.assertIn(self.l.membership_no.encode(), text)

    def test_pdf_built_directly_from_the_report_objects_is_masked_too(self):
        from unittest import mock

        from reportlab import rl_config

        sheet = statement.build(self.a, None, None, D(2027, 1, 1))
        with mock.patch.object(rl_config, "pageCompression", 0):
            raw = rr.to_pdf(sheet)
        self.assertNotIn(b"Zainab", raw)
        self.assertNotIn(b"R-ANON-PDF", raw)
