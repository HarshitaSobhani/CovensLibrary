import io
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from openpyxl import Workbook, load_workbook

from audit.models import AuditEvent
from catalogue import import_service as svc
from catalogue import services
from catalogue.models import BookCopy, BookEdition, Category, ImportBatch
from core.errors import RuleViolation

PW = "Correct-Horse-9!"


def workbook(rows, header=None, sheet="Books"):
    wb = Workbook()
    ws = wb.active
    ws.title = sheet
    ws.append(header or svc.COLUMNS)
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def row(
    title="Book",
    author="Test Author",
    isbn="",
    category="Fiction",
    copies=1,
    ids="",
    source="",
    **kw,
):
    data = dict.fromkeys(svc.COLUMNS, "")
    data.update(
        {
            "Title": title,
            "Author": author,
            "ISBN": isbn,
            "Category": category,
            "Copies": copies,
            "Legacy Copy IDs": ids,
            "Acquisition Source": source,
        }
    )
    data.update(kw)
    return [data[c] for c in svc.COLUMNS]


def reviewed(client, url, **data):
    """Form data for Confirm carrying the token of the preview the owner is looking at."""
    token = client.get(url).context["preview_token"]
    return {"copy_id_mode": "generate", "confirm": "1", "preview_token": token, **data}


def upload(content, name="books.xlsx"):
    return SimpleUploadedFile(name, content)


class ImportBase(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_user("owner", password=PW)
        self.cat = Category.objects.create(name="Fiction")

    def stage(self, rows, **kw):
        return svc.stage_import(upload(workbook(rows, **kw)), actor=self.owner)

    def options(self, batch, **kw):
        base = dict(
            create_missing_categories=False,
            copy_id_mode="generate",
            isbn_warnings_acknowledged=False,
        )
        base.update(kw)
        return svc.set_options(batch, **base)


class TemplateTests(ImportBase):
    def test_template_has_documented_columns_and_no_data_rows(self):
        wb = load_workbook(io.BytesIO(svc.build_template()))
        self.assertEqual([c.value for c in wb["Books"][1]], svc.COLUMNS)
        self.assertEqual(wb["Books"].max_row, 1)
        self.assertIn("Instructions", wb.sheetnames)
        with self.assertRaises(RuleViolation) as ctx:
            svc.stage_import(upload(svc.build_template()), actor=self.owner)
        self.assertEqual(ctx.exception.code, "no_rows")


class FileValidationTests(ImportBase):
    def test_rejects_wrong_type_size_and_structure(self):
        for name in ("books.xls", "books.xlsm", "books.csv", "books"):
            with self.assertRaises(RuleViolation):
                svc.stage_import(upload(workbook([row()]), name), actor=self.owner)
        with self.assertRaises(RuleViolation) as ctx:
            svc.stage_import(upload(b"not a zip at all", "x.xlsx"), actor=self.owner)
        self.assertEqual(ctx.exception.code, "file_type")
        with self.assertRaises(RuleViolation) as ctx:
            svc.stage_import(upload(b"PK" + b"0" * (6 * 1024 * 1024), "x.xlsx"), actor=self.owner)
        self.assertEqual(ctx.exception.code, "file_size")
        with self.assertRaises(RuleViolation) as ctx:
            svc.stage_import(upload(workbook([row()], sheet="Other")), actor=self.owner)
        self.assertEqual(ctx.exception.code, "sheet_missing")
        with self.assertRaises(RuleViolation) as ctx:
            svc.stage_import(upload(workbook([["x"]], header=["Foo", "Bar"])), actor=self.owner)
        self.assertEqual(ctx.exception.code, "header_missing")

    def test_nothing_written_to_catalogue_while_staging(self):
        self.stage([row("A"), row("B")])
        self.assertEqual(BookEdition.objects.count(), 0)
        self.assertEqual(BookCopy.objects.count(), 0)

    def test_original_rows_preserved(self):
        batch = self.stage([row("  Padded Title  ", isbn="978-0-261-10221-7", copies=2)])
        stored = batch.rows.get()
        self.assertEqual(stored.data["Title"], "Padded Title")
        self.assertEqual(stored.data["ISBN"], "978-0-261-10221-7")
        self.assertEqual(stored.clean["isbn"], "9780261102217")


class RowValidationTests(ImportBase):
    def test_field_errors(self):
        batch = self.stage(
            [
                row("", category="Fiction"),
                row("Bad ISBN", isbn="123"),
                row("Bad year", **{"Publication Year": "1492x"}),
                row("Bad copies", copies=0),
                row("Bad source", source="stolen"),
                row("Bad date", **{"Acquired On": "11/11/2026"}),
                row("Bad cost", **{"Unit Cost (INR)": "-5"}),
                row("Fine"),
            ]
        )
        valid = list(batch.rows.values_list("is_valid", flat=True))
        self.assertEqual(valid, [False] * 7 + [True])
        codes = [r.errors[0]["code"] for r in batch.rows.all()[:7]]
        self.assertEqual(
            codes,
            [
                "title_required",
                "isbn_invalid",
                "year_invalid",
                "copies_invalid",
                "source_invalid",
                "date_invalid",
                "cost_invalid",
            ],
        )

    def test_unknown_category_reject_vs_create(self):
        batch = self.stage([row("A", category="Mystery"), row("B")])
        rows = self.options(batch)
        self.assertEqual([r.is_valid for r in rows], [False, True])
        self.assertEqual(rows[0].errors[0]["code"], "category_unknown")
        rows = self.options(batch, create_missing_categories=True)
        self.assertEqual([r.is_valid for r in rows], [True, True])
        self.assertEqual(rows[0].warnings[0]["code"], "category_new")
        self.assertFalse(Category.objects.filter(name="Mystery").exists())  # not created yet

    def test_duplicate_isbn_warnings_database_and_file(self):
        services.create_edition(
            author="Test Author",
            actor=self.owner,
            title="Existing",
            category=self.cat,
            isbn="9780261102217",
        )
        batch = self.stage(
            [
                row("A", isbn="9780261102217"),
                row("B", isbn="0306406152"),
                row("C", isbn="0306406152"),
            ]
        )
        codes = [[w["code"] for w in r.warnings] for r in batch.rows.all()]
        self.assertEqual(codes, [["isbn_duplicate"], [], ["isbn_duplicate"]])
        self.assertTrue(all(r.is_valid for r in batch.rows.all()))

    def test_suspicious_duplicate_rows_warn(self):
        batch = self.stage([row("Same", author="A"), row("Same", author="A")])
        self.assertEqual([w["code"] for w in batch.rows.all()[1].warnings], ["row_duplicate"])

    def test_copy_id_modes_and_conflicts(self):
        e = services.create_edition(
            author="Test Author", actor=self.owner, title="Old", category=self.cat
        )
        services.add_copies(e, 1, actor=self.owner, source="purchased", codes=["EXIST-1"])
        batch = self.stage(
            [
                row("A", copies=2, ids="NEW-1, NEW-2"),
                row("B", copies=1, ids="EXIST-1"),
                row("C", copies=1, ids="NEW-1"),
                row("D", copies=2, ids="ONLY-ONE"),
            ]
        )
        rows = self.options(batch, copy_id_mode="supplied")
        self.assertEqual([r.is_valid for r in rows], [True, False, False, False])
        self.assertEqual(rows[1].errors[0]["code"], "id_exists")
        self.assertIn("id_repeated", [e["code"] for e in rows[2].errors])
        self.assertEqual(rows[3].errors[0]["code"], "ids_count")
        rows = self.options(batch, copy_id_mode="generate")
        # In generate mode, supplied references are only recorded; count mismatches still error.
        self.assertEqual(rows[3].errors[0]["code"], "ids_count")


class ExecutionTests(ImportBase):
    def test_generate_mode_creates_editions_copies_events_and_audit(self):
        batch = self.stage(
            [
                row(
                    "A",
                    copies=2,
                    isbn="9780261102217",
                    source="donated",
                    **{
                        "Donor Name": "Sam",
                        "Unit Cost (INR)": "250.5",
                        "Acquired On": "2026-01-15",
                        "Legacy Copy IDs": "L1, L2",
                    },
                ),
                row("B"),
            ]
        )
        self.options(batch)
        batch = svc.execute_import(batch, actor=self.owner)
        self.assertEqual(batch.status, "confirmed")
        self.assertEqual(batch.summary["editions"], 2)
        self.assertEqual(batch.summary["copies"], 3)
        self.assertEqual(BookEdition.objects.count(), 2)
        codes = list(BookCopy.objects.values_list("copy_code", flat=True))
        self.assertEqual(codes, ["COV-C-000001", "COV-C-000002", "COV-C-000003"])
        first = BookCopy.objects.get(copy_code="COV-C-000001")
        self.assertEqual(first.legacy_ref, "L1")
        self.assertEqual(first.acquisition.unit_cost_paise, 25050)
        self.assertEqual(first.acquisition.donor_name, "Sam")
        self.assertEqual(first.events.count(), 1)
        self.assertTrue(AuditEvent.objects.filter(action="import.confirm").exists())

    def test_supplied_mode_uses_legacy_ids_as_copy_ids(self):
        batch = self.stage([row("A", copies=2, ids="OLD-7, OLD-8")])
        self.options(batch, copy_id_mode="supplied")
        svc.execute_import(batch, actor=self.owner)
        self.assertEqual(
            sorted(BookCopy.objects.values_list("copy_code", "barcode")),
            [("OLD-7", "OLD-7"), ("OLD-8", "OLD-8")],
        )

    def test_create_missing_categories_on_confirm_only(self):
        batch = self.stage([row("A", category="Mystery")])
        self.options(batch, create_missing_categories=True)
        self.assertFalse(Category.objects.filter(name="Mystery").exists())
        svc.execute_import(batch, actor=self.owner)
        self.assertTrue(Category.objects.filter(name="Mystery").exists())

    def test_excluded_rows_need_explicit_acknowledgement(self):
        batch = self.stage([row("Good"), row("Bad", isbn="123")])
        self.options(batch)
        with self.assertRaises(RuleViolation) as ctx:
            svc.execute_import(batch, actor=self.owner)
        self.assertEqual(ctx.exception.code, "excluded_ack_required")
        self.assertEqual(BookEdition.objects.count(), 0)
        batch = svc.execute_import(batch, actor=self.owner, accept_excluded=True)
        self.assertEqual((BookEdition.objects.count(), batch.summary["rows_skipped"]), (1, 1))

    def test_isbn_warnings_need_acknowledgement(self):
        services.create_edition(
            author="Test Author",
            actor=self.owner,
            title="E",
            category=self.cat,
            isbn="9780261102217",
        )
        batch = self.stage([row("A", isbn="9780261102217")])
        self.options(batch)
        with self.assertRaises(RuleViolation) as ctx:
            svc.execute_import(batch, actor=self.owner)
        self.assertEqual(ctx.exception.code, "isbn_ack_required")
        self.options(batch, isbn_warnings_acknowledged=True)
        svc.execute_import(batch, actor=self.owner)
        self.assertEqual(BookEdition.objects.filter(isbn="9780261102217").count(), 2)

    def test_failure_rolls_back_everything(self):
        batch = self.stage([row("A", category="NewCat"), row("B"), row("C")])
        self.options(batch, create_missing_categories=True)
        real = services.add_copies
        calls = {"n": 0}

        def flaky(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("boom")
            return real(*args, **kwargs)

        with mock.patch("catalogue.import_service.services.add_copies", side_effect=flaky):
            with self.assertRaises(RuntimeError):
                svc.execute_import(batch, actor=self.owner)
        self.assertEqual(BookEdition.objects.count(), 0)
        self.assertEqual(BookCopy.objects.count(), 0)
        self.assertFalse(Category.objects.filter(name="NewCat").exists())
        batch.refresh_from_db()
        self.assertEqual(batch.status, "validated")
        # copy-id counter did not skip numbers
        e = services.create_edition(
            author="Test Author", actor=self.owner, title="Z", category=self.cat
        )
        self.assertEqual(
            services.add_copies(e, 1, actor=self.owner, source="purchased")[0].copy_code,
            "COV-C-000001",
        )

    def test_cannot_confirm_twice(self):
        batch = self.stage([row("A")])
        self.options(batch)
        svc.execute_import(batch, actor=self.owner)
        with self.assertRaises(RuleViolation) as ctx:
            svc.execute_import(batch, actor=self.owner)
        self.assertEqual(ctx.exception.code, "batch_closed")
        self.assertEqual(BookEdition.objects.count(), 1)

    def test_stale_preview_detected(self):
        batch = self.stage([row("A", copies=1, ids="NEW-1")])
        self.options(batch, copy_id_mode="supplied")
        e = services.create_edition(
            author="Test Author", actor=self.owner, title="Other", category=self.cat
        )
        services.add_copies(e, 1, actor=self.owner, source="purchased", codes=["NEW-1"])
        with self.assertRaises(RuleViolation) as ctx:
            svc.execute_import(batch, actor=self.owner)
        self.assertEqual(ctx.exception.code, "stale_preview")

    def test_error_report_csv_is_escaped(self):
        batch = self.stage([row("=cmd", isbn="123"), row("ok")])
        text = svc.error_report_csv(batch)
        self.assertTrue(text.startswith("Row,Severity,Field,Message,Code"))
        self.assertIn("isbn_invalid", text)
        self.assertNotIn(",=", text)


class ImportViewTests(ImportBase):
    def test_login_required(self):
        for name, args in [
            ("import_start", []),
            ("import_template", []),
            ("import_preview", [1]),
            ("import_errors", [1]),
        ]:
            self.assertEqual(
                self.client.get(reverse(f"catalogue:{name}", args=args)).status_code, 302, name
            )

    def test_upload_preview_confirm_flow(self):
        self.client.force_login(self.owner)
        r = self.client.post(
            reverse("catalogue:import_start"),
            {"file": upload(workbook([row("Flow", copies=2), row("Bad", isbn="1")]))},
        )
        batch = ImportBatch.objects.get()
        self.assertRedirects(r, reverse("catalogue:import_preview", args=[batch.pk]))
        page = self.client.get(reverse("catalogue:import_preview", args=[batch.pk]))
        self.assertContains(page, "1 valid")
        self.assertContains(page, "1 with errors")
        self.assertEqual(BookEdition.objects.count(), 0)
        url = reverse("catalogue:import_preview", args=[batch.pk])
        r = self.client.post(url, reviewed(self.client, url), follow=True)
        self.assertContains(r, "will be left out")  # needs explicit acknowledgement
        self.assertEqual(BookEdition.objects.count(), 0)
        r = self.client.post(url, reviewed(self.client, url, accept_excluded="on"), follow=True)
        self.assertContains(r, "Imported 1 edition")
        self.assertEqual(BookCopy.objects.count(), 2)
        report = self.client.get(reverse("catalogue:import_errors", args=[batch.pk]))
        self.assertEqual(report["Content-Type"], "text/csv")

    def test_invalid_upload_shows_error_and_creates_no_batch(self):
        self.client.force_login(self.owner)
        r = self.client.post(reverse("catalogue:import_start"), {"file": upload(b"junk", "x.xlsx")})
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "not a valid .xlsx")
        self.assertEqual(ImportBatch.objects.count(), 0)

    def test_template_download(self):
        self.client.force_login(self.owner)
        r = self.client.get(reverse("catalogue:import_template"))
        self.assertEqual(r.status_code, 200)
        self.assertIn("spreadsheetml", r["Content-Type"])


class HeaderNormalisationTests(ImportBase):
    def test_duplicate_column_is_rejected_not_guessed(self):
        header = [*svc.COLUMNS, "Category"]  # a second Category column
        with self.assertRaises(RuleViolation) as ctx:
            self.stage([[*row(), "Poetry"]], header=header)
        self.assertEqual(ctx.exception.code, "header_duplicate")
        self.assertIn("'Category'", ctx.exception.message)
        self.assertIn("columns G ('Category'), Q ('Category')", ctx.exception.message)
        self.assertEqual(ImportBatch.objects.count(), 0)

    def test_book_title_heading_is_accepted(self):
        for heading in ("Book Title", "book  title", "BOOK TITLE"):
            header = [heading if c == "Title" else c for c in svc.COLUMNS]
            batch = self.stage([row("Named")], header=header)
            r = batch.rows.get()
            self.assertTrue(r.is_valid, heading)
            self.assertEqual(r.clean["title"], "Named")

    def test_title_and_book_title_together_are_ambiguous(self):
        header = ["Title", "Author", "Book Title"]
        with self.assertRaises(RuleViolation) as ctx:
            self.stage([["One", "Someone", "Two"]], header=header)
        self.assertEqual(ctx.exception.code, "header_duplicate")
        self.assertIn("A ('Title'), C ('Book Title')", ctx.exception.message)

    def test_unrecognised_duplicate_columns_are_ignored(self):
        header = [*svc.COLUMNS, "Remarks", "Remarks"]
        self.assertTrue(self.stage([[*row(), "x", "y"]], header=header).rows.get().is_valid)

    def test_headers_match_ignoring_case_and_spacing(self):
        header = [c.upper() + " " for c in svc.COLUMNS]
        batch = self.stage([row()], header=header)
        self.assertTrue(batch.rows.get().is_valid)

    def test_blank_legacy_ids_valid_in_generate_mode(self):
        batch = self.stage([row(copies=3)])
        r = batch.rows.get()
        self.assertTrue(r.is_valid)
        self.assertEqual(r.errors, [])


class ModeAndCategoryRegressionTests(ImportBase):
    def codes(self):
        return sorted(BookCopy.objects.values_list("copy_code", flat=True))

    def test_generate_mode_multiple_copies_unique_ids(self):
        batch = self.stage([row("A", copies=3), row("B", copies=2)])
        self.options(batch)
        svc.execute_import(batch, actor=self.owner)
        codes = self.codes()
        self.assertEqual(len(codes), 5)
        self.assertEqual(len(set(codes)), 5)

    def test_supplied_mode_missing_and_wrong_count_rejected(self):
        batch = self.stage([row("A", copies=2), row("B", copies=2, ids="X1")])
        rows = self.options(batch, copy_id_mode="supplied")
        for r in rows:
            self.assertFalse(r.is_valid)
            self.assertEqual(r.errors[0]["code"], "ids_count")

    def test_supplied_mode_exact_ids_and_duplicates(self):
        batch = self.stage([row("A", copies=2, ids="X1,X2"), row("B", copies=2, ids="X3,X3")])
        rows = self.options(batch, copy_id_mode="supplied")
        self.assertEqual([r.is_valid for r in rows], [True, False])
        self.assertIn("ids_duplicate", [e["code"] for e in rows[1].errors])

    def test_switching_modes_revalidates_both_ways(self):
        batch = self.stage([row("A", copies=2)])
        self.assertTrue(batch.rows.get().is_valid)
        self.assertFalse(self.options(batch, copy_id_mode="supplied")[0].is_valid)
        batch.refresh_from_db()
        self.assertEqual(batch.copy_id_mode, "supplied")
        self.assertTrue(self.options(batch, copy_id_mode="generate")[0].is_valid)
        batch.refresh_from_db()
        self.assertEqual(batch.copy_id_mode, "generate")

    def test_invalid_mode_fails_explicitly(self):
        batch = self.stage([row("A")])
        for bad in ("", None, "auto"):
            with self.assertRaises(RuleViolation) as ctx:
                self.options(batch, copy_id_mode=bad)
            self.assertEqual(ctx.exception.code, "mode_invalid")

    def test_blank_category_accepted_whatever_the_create_setting(self):
        batch = self.stage([row("A", category="")])
        for create in (True, False):
            r = self.options(batch, create_missing_categories=create)[0]
            self.assertTrue(r.is_valid)
            self.assertEqual(r.errors, [])

    def test_failed_rows_create_no_categories(self):
        batch = self.stage([row("Good", category="New"), row("Bad", category="Ghost", isbn="1")])
        self.options(batch, create_missing_categories=True)
        svc.execute_import(batch, actor=self.owner, accept_excluded=True)
        self.assertTrue(Category.objects.filter(name="New").exists())
        self.assertFalse(Category.objects.filter(name="Ghost").exists())

    def test_view_persists_mode_on_update_preview(self):
        self.client.force_login(self.owner)
        batch = self.stage([row("A", copies=2)])
        url = reverse("catalogue:import_preview", args=[batch.pk])
        self.client.post(url, {"copy_id_mode": "supplied", "refresh": "1"})
        batch.refresh_from_db()
        self.assertEqual(batch.copy_id_mode, "supplied")
        page = self.client.get(url)
        self.assertContains(page, "0 valid")
        self.assertRegex(page.content.decode(), r'value="supplied"[^>]*\schecked')


MINIMAL = {"Title": "Only Title", "Author": "Only Author"}


def minimal_row(**kw):
    """Title + Author with every other field blank."""
    data = dict.fromkeys(svc.COLUMNS, "")
    data.update(MINIMAL)
    data.update(kw)
    return [data[c] for c in svc.COLUMNS]


class MandatoryFieldRuleTests(ImportBase):
    """Business rule: only Title and Author are mandatory; everything else is optional."""

    def confirm(self, batch, **kw):
        self.options(batch, **kw)
        return svc.execute_import(batch, actor=self.owner)

    def test_title_and_author_alone_import_successfully(self):
        batch = self.stage([minimal_row()])
        r = batch.rows.get()
        self.assertTrue(r.is_valid)
        self.assertEqual(r.errors, [])
        self.confirm(batch)
        e = BookEdition.objects.get()
        self.assertEqual((e.title, e.author), ("Only Title", "Only Author"))
        self.assertIsNone(e.category)
        self.assertEqual((e.isbn, e.publisher, e.edition_label, e.description, e.notes), ("",) * 5)
        self.assertIsNone(e.publication_year)
        self.assertEqual(BookCopy.objects.filter(edition=e).count(), 1)  # blank Copies = 1
        self.assertEqual(Category.objects.filter(name="").count(), 0)

    def test_missing_title_rejected_with_field_error(self):
        r = self.stage([minimal_row(Title="")]).rows.get()
        self.assertFalse(r.is_valid)
        self.assertEqual([(e["field"], e["code"]) for e in r.errors], [("Title", "title_required")])

    def test_missing_author_rejected_with_field_error(self):
        r = self.stage([minimal_row(Author="")]).rows.get()
        self.assertFalse(r.is_valid)
        self.assertEqual(
            [(e["field"], e["code"]) for e in r.errors], [("Author", "author_required")]
        )

    def test_whitespace_only_title_rejected(self):
        r = self.stage([minimal_row(Title="   \t ")]).rows.get()
        self.assertFalse(r.is_valid)
        self.assertEqual([e["code"] for e in r.errors], ["title_required"])

    def test_whitespace_only_author_rejected(self):
        r = self.stage([minimal_row(Author="  \t  ")]).rows.get()
        self.assertFalse(r.is_valid)
        self.assertEqual([e["code"] for e in r.errors], ["author_required"])

    def test_author_is_never_substituted(self):
        self.confirm(self.stage([minimal_row(Author="Unknown author")]))
        self.assertEqual(BookEdition.objects.get().author, "Unknown author")  # verbatim, user-given
        rejected = self.stage([minimal_row(Author="")]).rows.get()
        self.assertFalse(rejected.is_valid)  # never turned into "Unknown author"

    def test_blank_category_accepted(self):
        batch = self.stage([minimal_row(), minimal_row(Title="Two", Category="")])
        self.assertTrue(all(r.is_valid for r in batch.rows.all()))
        self.assertFalse(any(e["field"] == "Category" for r in batch.rows.all() for e in r.errors))
        self.assertEqual(Category.objects.count(), 1)  # only the fixture's "Fiction"; none invented

    def test_unknown_nonempty_category_follows_create_setting(self):
        batch = self.stage([minimal_row(Category="Poetry")])
        r = self.options(batch, create_missing_categories=False)[0]
        self.assertFalse(r.is_valid)
        self.assertEqual([e["code"] for e in r.errors], ["category_unknown"])
        r = self.options(batch, create_missing_categories=True)[0]
        self.assertTrue(r.is_valid)
        svc.execute_import(batch, actor=self.owner)
        self.assertEqual(BookEdition.objects.get().category.name, "Poetry")

    def test_generated_ids_work_with_incomplete_metadata(self):
        batch = self.stage([minimal_row(Copies="3"), minimal_row(Title="Two")])
        self.confirm(batch, copy_id_mode="generate")
        codes = list(BookCopy.objects.values_list("copy_code", flat=True))
        self.assertEqual(len(codes), 4)
        self.assertEqual(len(set(codes)), 4)
        self.assertTrue(all(c.startswith("COV-C-") for c in codes))

    def test_malformed_optional_values_still_rejected(self):
        for field, bad in (("ISBN", "123"), ("Publication Year", "abc"), ("Copies", "101")):
            r = self.stage([minimal_row(**{field: bad})]).rows.get()
            self.assertFalse(r.is_valid, field)
            self.assertEqual({e["field"] for e in r.errors}, {field})

    def test_missing_title_or_author_column_is_a_clear_error(self):
        for header in (["Title", "Category"], ["Author", "Category"]):
            with self.assertRaises(RuleViolation) as ctx:
                self.stage([["x", "y"]], header=header)
            self.assertEqual(ctx.exception.code, "header_missing")

    def test_preview_lists_missing_optional_metadata(self):
        self.client.force_login(self.owner)
        batch = self.stage([minimal_row(), minimal_row(Title="Two", Category="Fiction")])
        page = self.client.get(reverse("catalogue:import_preview", args=[batch.pk]))
        self.assertContains(page, "2 valid")
        self.assertContains(page, "missing optional details")
        self.assertContains(page, "Category 1")
        self.assertContains(page, "ISBN 2")

    def test_missing_optional_fields_remain_editable_after_import(self):
        self.confirm(self.stage([minimal_row()]))
        e = BookEdition.objects.get()
        self.assertEqual(
            e.missing_metadata,
            [
                "Category",
                "ISBN",
                "Publisher",
                "Edition",
                "Publication year",
                "Description",
                "Notes",
            ],
        )
        self.client.force_login(self.owner)
        url = reverse("catalogue:edition_edit", args=[e.pk])
        self.assertContains(self.client.get(url), "Only Title")
        r = self.client.post(
            url,
            {
                "title": "Only Title",
                "author": "Only Author",
                "category": self.cat.pk,
                "publisher": "Penguin",
                "publication_year": "2001",
                "isbn": "9780261102217",
                "edition_label": "",
                "description": "",
                "notes": "",
            },
        )
        self.assertEqual(r.status_code, 302)
        e.refresh_from_db()
        self.assertEqual((e.category, e.publisher, e.publication_year), (self.cat, "Penguin", 2001))
        self.assertEqual(e.missing_metadata, ["Edition", "Description", "Notes"])
        detail = self.client.get(reverse("catalogue:edition_detail", args=[e.pk]))
        self.assertContains(detail, "Incomplete record")
        listing = self.client.get(reverse("catalogue:edition_list") + "?incomplete=1")
        self.assertContains(listing, "Only Title")

    def test_incomplete_edition_without_author_stays_editable_but_new_needs_author(self):
        self.client.force_login(self.owner)
        legacy = BookEdition.objects.create(title="Legacy", author="", category=None)
        url = reverse("catalogue:edition_edit", args=[legacy.pk])
        r = self.client.post(url, {"title": "Legacy 2", "author": "", "category": ""})
        self.assertEqual(r.status_code, 302)  # legacy blank author does not block edits
        r = self.client.post(
            reverse("catalogue:edition_create"), {"title": "New", "author": "", "category": ""}
        )
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Author is required.")


class ModePersistenceWithIncompleteRowsTests(ImportBase):
    def test_generate_mode_survives_update_and_reload(self):
        self.client.force_login(self.owner)
        batch = self.stage([minimal_row(Title=f"B{i}") for i in range(3)])
        url = reverse("catalogue:import_preview", args=[batch.pk])
        self.client.post(url, {"copy_id_mode": "supplied", "refresh": "1"})
        codes = {e["code"] for r in batch.rows.all() for e in r.errors}
        self.assertEqual(codes, {"ids_count"})  # supplied mode needs one ID per copy
        self.client.post(url, {"copy_id_mode": "generate", "refresh": "1"})
        batch.refresh_from_db()
        self.assertEqual(batch.copy_id_mode, "generate")
        self.assertTrue(all(r.is_valid for r in batch.rows.all()))
        html = self.client.get(url).content.decode()
        self.assertRegex(html, r'value="generate"[^>]*\schecked')
        self.assertNotRegex(html, r'value="supplied"[^>]*\schecked')


class AuditRemediationTests(ImportBase):
    """Engineering audit findings A-01 to A-03."""

    def test_edition_in_inactive_category_stays_editable(self):  # A-01
        inactive = Category.objects.create(name="Retired", is_active=False)
        e = services.create_edition(
            actor=self.owner, title="Old", author="Someone", category=inactive
        )
        self.client.force_login(self.owner)
        r = self.client.post(
            reverse("catalogue:edition_edit", args=[e.pk]),
            {"title": "Old", "author": "Someone", "category": inactive.pk, "publisher": "P"},
        )
        self.assertEqual(r.status_code, 302)
        e.refresh_from_db()
        self.assertEqual((e.category, e.publisher), (inactive, "P"))
        # an inactive category still cannot be chosen for another edition
        other = services.create_edition(actor=self.owner, title="New", author="Someone")
        r = self.client.post(
            reverse("catalogue:edition_edit", args=[other.pk]),
            {"title": "New", "author": "Someone", "category": inactive.pk},
        )
        self.assertEqual(r.status_code, 200)

    def test_set_options_is_atomic(self):  # A-02
        batch = self.stage([minimal_row()])
        with mock.patch.object(svc, "validate_batch", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                self.options(batch, copy_id_mode="supplied")
        batch.refresh_from_db()
        self.assertEqual(batch.copy_id_mode, "generate")  # option change rolled back

    def test_stale_preview_detected_when_valid_rows_swap(self):  # A-03
        # Same number of valid rows before and after, but a different set of rows.
        batch = self.stage(
            [minimal_row(Title="A", Category="Later"), minimal_row(Title="B", Category="Fiction")]
        )
        self.assertEqual([r.is_valid for r in batch.rows.all()], [False, True])
        Category.objects.create(name="Later")
        Category.objects.filter(name="Fiction").update(name="Renamed")
        with self.assertRaises(RuleViolation) as ctx:
            svc.execute_import(batch, actor=self.owner, accept_excluded=True)
        self.assertEqual(ctx.exception.code, "stale_preview")
        self.assertEqual(BookEdition.objects.count(), 0)

    def test_confirm_never_imports_an_unreviewed_preview(self):  # A-04 / O-02
        self.client.force_login(self.owner)
        url_of = lambda b: reverse("catalogue:import_preview", args=[b.pk])  # noqa: E731
        stale = "has been regenerated"

        # 1. catalogue changed after the preview was shown
        batch = self.stage([minimal_row(Title="A", Category="Later"), minimal_row(Title="B")])
        url = url_of(batch)
        data = reviewed(self.client, url)
        Category.objects.create(name="Later")
        r = self.client.post(url, data, follow=True)
        self.assertContains(r, stale)
        self.assertContains(r, "2 valid")  # regenerated preview displayed
        self.assertEqual(BookEdition.objects.count(), 0)
        r = self.client.post(url, data, follow=True)  # the OLD token again: still refused
        self.assertContains(r, stale)
        self.assertEqual(BookEdition.objects.count(), 0)
        r = self.client.post(url, reviewed(self.client, url), follow=True)  # separate confirm
        self.assertContains(r, "Import completed.")
        self.assertEqual(BookEdition.objects.count(), 2)

        # 2. an option changed in the same click as Confirm
        batch = self.stage([minimal_row(Title="C", Category="Brand New")])
        url = url_of(batch)
        data = reviewed(self.client, url, create_missing_categories="on")
        r = self.client.post(url, data, follow=True)
        self.assertContains(r, stale)
        self.assertEqual(BookEdition.objects.filter(title="C").count(), 0)
        batch.refresh_from_db()
        self.assertTrue(batch.create_missing_categories)  # option kept, preview regenerated
        data = reviewed(self.client, url, create_missing_categories="on")
        self.assertContains(self.client.post(url, data, follow=True), "Import completed.")
        self.assertEqual(BookEdition.objects.filter(title="C").count(), 1)

        # 3. copy-ID mode changed in the same click
        batch = self.stage([minimal_row(Title="D", **{"Legacy Copy IDs": "L-1"})])
        url = url_of(batch)
        data = reviewed(self.client, url, copy_id_mode="supplied")
        self.assertContains(self.client.post(url, data, follow=True), stale)
        self.assertEqual(BookEdition.objects.filter(title="D").count(), 0)

        # 4. no token, or a forged one
        batch = self.stage([minimal_row(Title="E")])
        url = url_of(batch)
        for token in (None, "", "0" * 64):
            data = {"copy_id_mode": "generate", "confirm": "1"}
            if token is not None:
                data["preview_token"] = token
            self.assertContains(self.client.post(url, data, follow=True), stale)
            self.assertEqual(BookEdition.objects.filter(title="E").count(), 0)

        # 5. the acknowledgement boxes are confirmations, not preview changes
        batch = self.stage([minimal_row(Title="F"), minimal_row(Title="")])
        url = url_of(batch)
        data = reviewed(self.client, url, accept_excluded="on")
        self.assertContains(self.client.post(url, data, follow=True), "Import completed.")
        self.assertEqual(BookEdition.objects.filter(title="F").count(), 1)

    def test_service_refuses_a_reviewed_state_that_no_longer_matches(self):  # O-02
        batch = self.stage([minimal_row()])
        seen = svc.preview_state(batch)
        self.options(batch, copy_id_mode="supplied")
        self.options(batch, copy_id_mode="generate")
        svc.execute_import(batch, actor=self.owner, reviewed_state=seen)  # same state again: fine
        batch2 = self.stage([minimal_row(Title="Z", Category="Nope")])
        seen = svc.preview_state(batch2)
        self.options(batch2, create_missing_categories=True)
        with self.assertRaises(RuleViolation) as ctx:
            svc.execute_import(batch2, actor=self.owner, reviewed_state=seen)
        self.assertEqual(ctx.exception.code, "stale_preview")

    def test_preview_shows_author_and_labelled_controls(self):
        self.client.force_login(self.owner)
        batch = self.stage([minimal_row()])
        page = self.client.get(reverse("catalogue:import_preview", args=[batch.pk]))
        self.assertContains(page, "<th>Author</th>")
        self.assertContains(page, "Only Author")
        self.assertContains(page, "Copy IDs")
        html = page.content.decode()
        for value in ("generate", "supplied"):
            self.assertRegex(html, rf'<input[^>]*type="radio"[^>]*value="{value}"[^>]*id="(\w+)"')
        self.assertRegex(html, r'<label class="form-check-label" for="id_copy_id_mode_0">')

    def test_stale_preview_detected_when_a_valid_row_changes_outcome(self):  # A-03
        # The same rows stay valid and every total is identical, but what row A would do
        # changes: its category was "to be created" in the preview and now already exists.
        batch = self.stage([minimal_row(Title="A", Category="Later")])
        self.options(batch, create_missing_categories=True)
        before = svc.summarise(batch)
        Category.objects.create(name="Later")
        with self.assertRaises(RuleViolation) as ctx:
            svc.execute_import(batch, actor=self.owner)
        self.assertEqual(ctx.exception.code, "stale_preview")
        self.assertEqual(BookEdition.objects.count(), 0)
        after = svc.summarise(batch)
        self.assertEqual((before["valid"], before["invalid"]), (after["valid"], after["invalid"]))

    def test_unchanged_preview_still_confirms(self):
        batch = self.stage([minimal_row(), minimal_row(Title="Two", Category="Fiction")])
        self.options(batch)
        svc.execute_import(batch, actor=self.owner)
        self.assertEqual(BookEdition.objects.count(), 2)

    def test_failed_set_options_rolls_back_row_validation_too(self):  # A-02
        batch = self.stage([minimal_row()])
        state = svc.preview_state(batch)
        with mock.patch.object(
            svc.ImportRow.objects, "bulk_update", side_effect=RuntimeError("boom")
        ):
            with self.assertRaises(RuntimeError):
                self.options(batch, copy_id_mode="supplied", create_missing_categories=True)
        batch.refresh_from_db()
        self.assertEqual((batch.copy_id_mode, batch.create_missing_categories), ("generate", False))
        self.assertEqual(svc.preview_state(batch), state)
