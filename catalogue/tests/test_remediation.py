"""Regression tests for the Phase 1-2 review findings M1, L2-L5, L10-L12."""

import io
import threading
import zipfile
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import DataError, connections
from django.test import TestCase, TransactionTestCase
from django.urls import reverse
from openpyxl import Workbook

from catalogue import import_service as svc
from catalogue import services
from catalogue.models import BookCopy, BookEdition, Category, LabelLayout
from core.errors import RuleViolation

PW = "Correct-Horse-9!"


def workbook(rows, header=None):
    wb = Workbook()
    ws = wb.active
    ws.title = "Books"
    ws.append(header or svc.COLUMNS)
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def row(**kw):
    data = dict.fromkeys(svc.COLUMNS, "")
    data.update({"Title": "Book", "Category": "Fiction"})
    data.update(kw)
    return [data[c] for c in svc.COLUMNS]


class Base(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_user("owner", password=PW)
        self.cat = Category.objects.create(name="Fiction")

    def stage(self, rows, mode="generate", create_categories=False):
        batch = svc.stage_import(SimpleUploadedFile("b.xlsx", workbook(rows)), actor=self.owner)
        rows_ = svc.set_options(
            batch,
            create_missing_categories=create_categories,
            copy_id_mode=mode,
            isbn_warnings_acknowledged=True,
        )
        return batch, rows_


class NumericValidationTests(Base):
    def test_non_finite_and_huge_numbers_are_row_errors_not_crashes(self):
        cases = [
            ("Copies", "inf", "copies_invalid"),
            ("Copies", "-inf", "copies_invalid"),
            ("Copies", "nan", "copies_invalid"),
            ("Copies", "1e999", "copies_invalid"),
            ("Copies", "0", "copies_invalid"),
            ("Copies", "101", "copies_invalid"),
            ("Copies", "2.5", "copies_invalid"),
            ("Publication Year", "inf", "year_invalid"),
            ("Publication Year", "-Infinity", "year_invalid"),
            ("Publication Year", "1e999", "year_invalid"),
            ("Publication Year", "999", "year_invalid"),
            ("Publication Year", "2001.5", "year_invalid"),
            ("Publication Year", "99999999999", "year_invalid"),
            ("Unit Cost (INR)", "Infinity", "cost_invalid"),
            ("Unit Cost (INR)", "-Infinity", "cost_invalid"),
            ("Unit Cost (INR)", "NaN", "cost_invalid"),
            ("Unit Cost (INR)", "1e999", "cost_invalid"),
            ("Unit Cost (INR)", "-1", "cost_invalid"),
            ("Unit Cost (INR)", "10.999", "cost_invalid"),
            ("Unit Cost (INR)", "99999999999", "cost_invalid"),
            ("Unit Cost (INR)", "abc", "cost_invalid"),
        ]
        batch, rows = self.stage([row(**{col: value}) for col, value, _ in cases])
        for (col, value, code), r in zip(cases, rows, strict=True):
            self.assertFalse(r.is_valid, f"{col}={value} must be invalid")
            self.assertIn(code, [e["code"] for e in r.errors], f"{col}={value}")

    def test_valid_numbers_still_pass(self):
        batch, rows = self.stage(
            [row(**{"Publication Year": "2001", "Copies": "3", "Unit Cost (INR)": "250.50"})]
        )
        self.assertTrue(rows[0].is_valid)
        self.assertEqual((rows[0].clean["copies"], rows[0].clean["unit_cost_paise"]), (3, 25050))

    def test_cost_boundary(self):
        _, rows = self.stage(
            [row(**{"Unit Cost (INR)": "21474836.47"}), row(**{"Unit Cost (INR)": "21474836.48"})]
        )
        self.assertEqual([r.is_valid for r in rows], [True, False])


class LengthValidationTests(Base):
    LIMITS = [
        ("Title", 300),
        ("Author", 200),
        ("Publisher", 200),
        ("Edition", 100),
        ("Donor Name", 200),
        ("Shelf Location", 80),
    ]

    def test_each_text_field_over_its_database_limit_is_a_row_error(self):
        for column, limit in self.LIMITS:
            _, rows = self.stage([row(**{column: "x" * (limit + 1)})])
            self.assertFalse(rows[0].is_valid, column)
            self.assertIn("too_long", [e["code"] for e in rows[0].errors], column)

    def test_category_and_copy_id_limits(self):
        _, rows = self.stage([row(Category="C" * 81)], create_categories=True)
        self.assertIn("too_long", [e["code"] for e in rows[0].errors])
        _, rows = self.stage([row(**{"Legacy Copy IDs": "L" * 41})], mode="supplied")
        self.assertIn("too_long", [e["code"] for e in rows[0].errors])
        _, rows = self.stage([row(**{"Legacy Copy IDs": "L" * 81})], mode="generate")
        self.assertIn("too_long", [e["code"] for e in rows[0].errors])

    def test_description_is_capped_defensively(self):
        _, rows = self.stage([row(Description="d" * 5001)])
        self.assertIn("too_long", [e["code"] for e in rows[0].errors])

    def test_rows_exactly_at_every_limit_are_valid_and_really_import(self):
        """Preview 'valid' must mean execution succeeds: test the boundary end to end."""
        big = row(
            Title="T" * 300,
            Author="A" * 200,
            Publisher="P" * 200,
            Edition="E" * 100,
            Category="K" * 80,
            **{
                "Donor Name": "D" * 200,
                "Shelf Location": "S" * 80,
                "Legacy Copy IDs": "Z" * 40,
                "Copies": 1,
                "Acquisition Source": "donated",
            },
        )
        batch, rows = self.stage([big], mode="supplied", create_categories=True)
        self.assertTrue(rows[0].is_valid, rows[0].errors)
        svc.execute_import(batch, actor=self.owner)
        self.assertEqual(BookEdition.objects.get().title, "T" * 300)
        self.assertEqual(BookCopy.objects.get().copy_code, "Z" * 40)
        batch2, rows2 = self.stage(
            [row(Title="Gen", **{"Legacy Copy IDs": "Y" * 80})], mode="generate"
        )
        self.assertTrue(rows2[0].is_valid)
        svc.execute_import(batch2, actor=self.owner)
        self.assertEqual(BookCopy.objects.get(edition__title="Gen").legacy_ref, "Y" * 80)

    def test_every_reported_failure_now_blocks_confirmation_with_a_clear_error(self):
        for column, value in [("Title", "T" * 301), ("Author", "A" * 201)]:
            batch, rows = self.stage([row(**{column: value})])
            self.assertFalse(rows[0].is_valid)
            with self.assertRaises(RuleViolation) as ctx:
                svc.execute_import(batch, actor=self.owner)
            self.assertEqual(ctx.exception.code, "nothing_to_import")
            self.assertEqual(BookEdition.objects.count(), 0)


class CopyIdRuleTests(Base):
    def test_supplied_ids_must_be_printable_ascii_and_not_in_generated_format(self):
        _, rows = self.stage(
            [
                row(**{"Legacy Copy IDs": "ÉCOLE-1"}),
                row(**{"Legacy Copy IDs": "COV-C-000099"}),
                row(**{"Legacy Copy IDs": "OK-1"}),
            ],
            mode="supplied",
        )
        self.assertEqual([r.is_valid for r in rows], [False, False, True])
        self.assertEqual(rows[0].errors[0]["code"], "id_charset")
        self.assertEqual(rows[1].errors[0]["code"], "id_reserved")

    def test_service_also_refuses_reserved_codes(self):
        e = services.create_edition(actor=self.owner, title="X", category=self.cat)
        with self.assertRaises(RuleViolation) as ctx:
            services.add_copies(e, 1, actor=self.owner, source="purchased", codes=["COV-C-000123"])
        self.assertEqual(ctx.exception.code, "code_reserved")


class AtomicityTests(Base):
    def test_unforeseen_database_error_rolls_back_and_reports_cleanly(self):
        batch, rows = self.stage(
            [row(Title="A", Category="New1"), row(Title="B")], create_categories=True
        )
        real = services.add_copies
        calls = []

        def boom(*a, **k):
            calls.append(1)
            if len(calls) == 2:
                raise DataError("simulated value too long")
            return real(*a, **k)

        with mock.patch("catalogue.import_service.services.add_copies", side_effect=boom):
            with self.assertRaises(RuleViolation) as ctx:
                svc.execute_import(batch, actor=self.owner)
        self.assertEqual(ctx.exception.code, "import_failed")
        self.assertEqual((BookEdition.objects.count(), BookCopy.objects.count()), (0, 0))
        self.assertFalse(Category.objects.filter(name="New1").exists())
        batch.refresh_from_db()
        self.assertEqual(batch.status, "validated")

    def test_view_shows_a_message_not_a_server_error(self):
        client = self.client
        client.force_login(self.owner)
        r = client.post(
            reverse("catalogue:import_start"),
            {"file": SimpleUploadedFile("b.xlsx", workbook([row(Copies="inf")]))},
        )
        self.assertEqual(r.status_code, 302)  # staged; the row is simply reported invalid
        page = client.get(r["Location"])
        self.assertContains(page, "1 with errors")


class UploadHardeningTests(Base):
    def test_oversized_upload_is_rejected_before_reading(self):
        class Fake:
            name = "x.xlsx"
            size = svc.MAX_BYTES + 1
            read = mock.Mock()

        with self.assertRaises(RuleViolation) as ctx:
            svc.read_workbook(Fake())
        self.assertEqual(ctx.exception.code, "file_size")
        Fake.read.assert_not_called()

    def test_read_is_bounded(self):
        data = io.BytesIO(b"PK" + b"0" * (svc.MAX_BYTES * 2))

        class Fake:
            name = "x.xlsx"
            size = None
            read = lambda self, n=-1: data.read(n)  # noqa: E731

        with self.assertRaises(RuleViolation):
            svc.read_workbook(Fake())
        self.assertLessEqual(data.tell(), svc.MAX_BYTES + 1)

    def _zip_with(self, extra_name, payload):
        src = workbook([row()])
        out = io.BytesIO()
        with (
            zipfile.ZipFile(io.BytesIO(src)) as zin,
            zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zout,
        ):
            for item in zin.infolist():
                zout.writestr(item, zin.read(item))
            zout.writestr(extra_name, payload)
        return out.getvalue()

    def test_decompression_bomb_is_rejected(self):
        bomb = self._zip_with("xl/bomb.bin", b"\0" * (60 * 1024 * 1024))
        self.assertLess(len(bomb), svc.MAX_BYTES)
        with self.assertRaises(RuleViolation) as ctx:
            svc.stage_import(SimpleUploadedFile("b.xlsx", bomb), actor=self.owner)
        self.assertEqual(ctx.exception.code, "file_complex")

    def test_high_compression_ratio_is_rejected(self):
        data = self._zip_with("xl/ratio.bin", b"\0" * (3 * 1024 * 1024))
        with self.assertRaises(RuleViolation) as ctx:
            svc.stage_import(SimpleUploadedFile("b.xlsx", data), actor=self.owner)
        self.assertEqual(ctx.exception.code, "file_complex")

    def test_macro_payload_is_rejected(self):
        data = self._zip_with("xl/vbaProject.bin", b"macro")
        with self.assertRaises(RuleViolation) as ctx:
            svc.stage_import(SimpleUploadedFile("b.xlsx", data), actor=self.owner)
        self.assertEqual(ctx.exception.code, "file_type")

    def test_too_many_parts_is_rejected(self):
        src = workbook([row()])
        out = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(src)) as zin, zipfile.ZipFile(out, "w") as zout:
            for item in zin.infolist():
                zout.writestr(item, zin.read(item))
            for i in range(svc.MAX_ZIP_ENTRIES + 1):
                zout.writestr(f"xl/extra{i}.xml", b"x")
        with self.assertRaises(RuleViolation) as ctx:
            svc.stage_import(SimpleUploadedFile("b.xlsx", out.getvalue()), actor=self.owner)
        self.assertEqual(ctx.exception.code, "file_complex")

    def test_row_scan_is_capped_even_when_rows_are_empty(self):
        wb = Workbook()
        ws = wb.active
        ws.title = "Books"
        ws.append(svc.COLUMNS)
        ws.cell(row=svc.MAX_SCAN_ROWS + 50, column=1, value="Late")
        buf = io.BytesIO()
        wb.save(buf)
        with self.assertRaises(RuleViolation) as ctx:
            svc.stage_import(SimpleUploadedFile("b.xlsx", buf.getvalue()), actor=self.owner)
        self.assertEqual(ctx.exception.code, "too_many_rows")

    def test_data_row_limit(self):
        rows = [row(Title=f"B{i}") for i in range(svc.MAX_ROWS + 1)]
        with self.assertRaises(RuleViolation) as ctx:
            svc.stage_import(SimpleUploadedFile("b.xlsx", workbook(rows)), actor=self.owner)
        self.assertEqual(ctx.exception.code, "too_many_rows")

    def test_extra_columns_beyond_the_bound_are_ignored(self):
        wb = Workbook()
        ws = wb.active
        ws.title = "Books"
        ws.append(svc.COLUMNS + [f"X{i}" for i in range(200)])
        ws.append(row()[: len(svc.COLUMNS)] + ["y"] * 200)
        buf = io.BytesIO()
        wb.save(buf)
        batch = svc.stage_import(SimpleUploadedFile("b.xlsx", buf.getvalue()), actor=self.owner)
        self.assertLessEqual(len(batch.rows.get().data), svc.MAX_COLS)


class CsvInjectionTests(Base):
    def test_formula_prefixes_are_neutralised_including_whitespace_variants(self):
        for danger in [
            "=1+1",
            "+1",
            "-1",
            "@SUM(A1)",
            "\t=1+1",
            "\r=1",
            "\n=1",
            " =1+1",
            "  @x",
            " =1",
            "﻿=1",
            "\t",
            "\r",
        ]:
            self.assertTrue(svc._csv_safe(danger).startswith("'"), repr(danger))
        for safe in ["hello", "Row 5", "ok = fine", "12", "", "e-mail"]:
            self.assertEqual(svc._csv_safe(safe), safe, repr(safe))

    def test_error_report_applies_it_to_every_cell(self):
        batch, _ = self.stage([row(Title="+evil()", Category="@Cat", Author="-x")])
        text = svc.error_report_csv(batch)
        for line in text.splitlines()[1:]:
            for cell in line.split(","):
                self.assertFalse(cell[:1] in ("=", "+", "@"), line)


class CellSafetyTests(Base):
    def test_nul_bytes_are_stripped_from_cells_and_search_terms(self):
        self.assertEqual(svc._text("a\x00b"), "ab")
        from core.http import clean_query

        self.assertEqual(clean_query("ab\x00c\x07d"), "abcd")
        self.assertEqual(len(clean_query("x" * 500)), 200)

    def test_formula_cells_are_never_evaluated_or_imported(self):
        wb = Workbook()
        ws = wb.active
        ws.title = "Books"
        ws.append(["Title", "Category"])
        ws.append(["=1+1", "Fiction"])
        ws.append(["Real", "Fiction"])
        buf = io.BytesIO()
        wb.save(buf)
        batch = svc.stage_import(SimpleUploadedFile("b.xlsx", buf.getvalue()), actor=self.owner)
        titles = [r.data.get("Title") for r in batch.rows.all()]
        self.assertNotIn("=1+1", titles)
        self.assertNotIn("2", titles)


class IsbnVariantTests(Base):
    def test_isbn10_and_isbn13_of_the_same_book_are_duplicates(self):
        services.create_edition(actor=self.owner, title="A", category=self.cat, isbn="0306406152")
        self.assertTrue(services.find_isbn_duplicates("9780306406157").exists())
        e = services.create_edition(
            actor=self.owner, title="B", category=self.cat, isbn="9780261102217"
        )
        self.assertTrue(services.find_isbn_duplicates("0261102214").exists())
        self.assertFalse(services.find_isbn_duplicates("0261102214", exclude_pk=e.pk).exists())
        self.assertFalse(services.find_isbn_duplicates("9791234567896").exists())

    def test_variants_function(self):
        self.assertEqual(services.isbn_variants("0306406152"), {"0306406152", "9780306406157"})
        self.assertEqual(services.isbn_variants("9780306406157"), {"0306406152", "9780306406157"})
        self.assertEqual(services.isbn_variants("9791234567896"), {"9791234567896"})
        self.assertEqual(services.isbn_variants("080442957X"), {"080442957X", "9780804429573"})


class LabelDefaultTests(Base):
    def test_creating_a_new_default_replaces_the_old_one(self):
        self.client.force_login(self.owner)
        first = LabelLayout.objects.create(name="First", is_default=True)
        data = {
            "name": "Second",
            "page_width_mm": "210",
            "page_height_mm": "297",
            "label_width_mm": "60",
            "label_height_mm": "30",
            "columns": "3",
            "rows": "8",
            "margin_mm": "10",
            "is_default": "on",
        }
        r = self.client.post(reverse("catalogue:layout_list"), data)
        self.assertEqual(r.status_code, 302)
        first.refresh_from_db()
        self.assertFalse(first.is_default)
        self.assertEqual(LabelLayout.objects.get(is_default=True).name, "Second")

    def test_set_default_action_and_login_requirement(self):
        a = LabelLayout.objects.create(name="A", is_default=True)
        b = LabelLayout.objects.create(name="B")
        url = reverse("catalogue:layout_set_default", args=[b.pk])
        self.assertEqual(self.client.post(url).status_code, 302)  # anonymous -> login
        b.refresh_from_db()
        self.assertFalse(b.is_default)
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(url).status_code, 405)
        self.client.post(url)
        a.refresh_from_db()
        b.refresh_from_db()
        self.assertEqual((a.is_default, b.is_default), (False, True))

    def test_invalid_layout_still_gets_a_form_error_not_a_crash(self):
        self.client.force_login(self.owner)
        bad = {
            "name": "Big",
            "page_width_mm": "100",
            "page_height_mm": "100",
            "label_width_mm": "60",
            "label_height_mm": "30",
            "columns": "3",
            "rows": "8",
            "margin_mm": "10",
        }
        r = self.client.post(reverse("catalogue:layout_list"), bad)
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "does not fit")


class QueryParameterTests(Base):
    def test_bad_filters_give_controlled_responses(self):
        self.client.force_login(self.owner)
        e = services.create_edition(actor=self.owner, title="T", category=self.cat)
        services.add_copies(e, 1, actor=self.owner, source="purchased")
        for params in [
            "category=abc",
            "category=-1",
            "category=999999999999999999999",
            "category=%00",
            "archived=maybe",
            "page=zzz",
            "q=%27%3B--",
        ]:
            r = self.client.get("/books/?" + params)
            self.assertEqual(r.status_code, 200, params)
        for params in ["layout=abc", "layout=-5", "layout=99999", "layout="]:
            r = self.client.get(reverse("catalogue:labels_pdf", args=[e.pk]) + "?" + params)
            self.assertIn(r.status_code, (200, 302), params)
        r = self.client.get(
            reverse("catalogue:labels_pdf", args=[e.pk]) + "?layout=abc", follow=True
        )
        self.assertContains(r, "not valid")
        for params in ["status=zzz", "page=abc", "q=%00"]:
            self.assertEqual(self.client.get("/members/?" + params).status_code, 200, params)

    def test_invalid_category_filter_warns_and_is_ignored(self):
        self.client.force_login(self.owner)
        services.create_edition(actor=self.owner, title="Visible", category=self.cat)
        r = self.client.get("/books/?category=abc")
        self.assertContains(r, "Visible")
        self.assertContains(r, "not valid")


class PaginationEncodingTests(Base):
    def test_search_with_ampersand_survives_pagination(self):
        self.client.force_login(self.owner)
        for i in range(30):
            services.create_edition(actor=self.owner, title=f"Jerry & Tom {i}", category=self.cat)
        services.create_edition(actor=self.owner, title="Jerry alone", category=self.cat)
        first = self.client.get("/books/", {"q": "Jerry & Tom"})
        self.assertContains(first, "q=Jerry+%26+Tom&amp;page=2")
        second = self.client.get("/books/", {"q": "Jerry & Tom", "page": 2})
        self.assertEqual(second.context["q"], "Jerry & Tom")
        self.assertEqual(len(second.context["page"]), 5)  # 30 matches -> 25 + 5, not 31 total

    def test_member_filters_are_encoded(self):
        from members import services as ms

        self.client.force_login(self.owner)
        for i in range(30):
            ms.create_member(
                actor=self.owner,
                full_name=f"A&B {i}",
                national_number=f"98765{40000 + i}",
                region="IN",
            )
        r = self.client.get("/members/", {"q": "A&B", "status": "active"})
        self.assertContains(r, "q=A%26B&amp;status=active&amp;page=2")
        self.assertEqual(
            len(
                self.client.get("/members/", {"q": "A&B", "status": "active", "page": 2}).context[
                    "page"
                ]
            ),
            5,
        )

    def test_import_preview_pages_through_all_rows(self):
        self.client.force_login(self.owner)
        rows = [row(Title=f"B{i}") for i in range(130)]
        r = self.client.post(
            reverse("catalogue:import_start"),
            {"file": SimpleUploadedFile("b.xlsx", workbook(rows))},
        )
        page2 = self.client.get(r["Location"] + "?page=2")
        self.assertContains(page2, "B100")
        self.assertContains(page2, "B129")


class DuplicateMobileRaceTests(TransactionTestCase):
    def test_simultaneous_creation_gives_one_member_and_a_clean_error(self):
        from members import services as ms
        from members.models import Member

        owner = get_user_model().objects.create_user("owner", password=PW)
        barrier = threading.Barrier(2)
        outcomes = []

        def create():
            barrier.wait()
            try:
                ms.create_member(
                    actor=owner, full_name="Race", national_number="9000000001", region="IN"
                )
                outcomes.append("created")
            except RuleViolation as exc:
                outcomes.append(exc.code)
            except Exception as exc:  # noqa: BLE001
                outcomes.append(type(exc).__name__)
            finally:
                connections.close_all()

        threads = [threading.Thread(target=create) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sorted(outcomes), ["created", "mobile_duplicate"])
        self.assertEqual(Member.objects.filter(mobile_e164="+919000000001").count(), 1)
        # the sequence stayed gap-free for the next member
        nxt = ms.create_member(
            actor=owner, full_name="Next", national_number="9000000002", region="IN"
        )
        self.assertEqual(nxt.membership_no, "COV-M-000002")
