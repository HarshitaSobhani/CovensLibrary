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


def row(title="Book", author="", isbn="", category="Fiction", copies=1, ids="", source="", **kw):
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
            actor=self.owner, title="Existing", category=self.cat, isbn="9780261102217"
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
        e = services.create_edition(actor=self.owner, title="Old", category=self.cat)
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
            actor=self.owner, title="E", category=self.cat, isbn="9780261102217"
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
        e = services.create_edition(actor=self.owner, title="Z", category=self.cat)
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
        e = services.create_edition(actor=self.owner, title="Other", category=self.cat)
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
        r = self.client.post(
            reverse("catalogue:import_preview", args=[batch.pk]),
            {"copy_id_mode": "generate", "confirm": "1"},
            follow=True,
        )
        self.assertContains(r, "will be left out")  # needs explicit acknowledgement
        self.assertEqual(BookEdition.objects.count(), 0)
        r = self.client.post(
            reverse("catalogue:import_preview", args=[batch.pk]),
            {"copy_id_mode": "generate", "accept_excluded": "on", "confirm": "1"},
            follow=True,
        )
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
