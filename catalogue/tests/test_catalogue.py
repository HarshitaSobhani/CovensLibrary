from django.contrib.auth import get_user_model
from django.db import DatabaseError, IntegrityError, transaction
from django.db.models import ProtectedError
from django.test import TestCase
from django.urls import reverse

from audit.models import AuditEvent
from catalogue import services
from catalogue.labels import default_layout, page_count, render_labels_pdf, validate_layout
from catalogue.models import BookCopy, BookEdition, Category, CopyEvent, LabelLayout
from core.errors import RuleViolation

PW = "Correct-Horse-9!"


class Base(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_user("owner", password=PW)
        self.cat = Category.objects.create(name="Fiction")

    def edition(self, title="The Hobbit", **kw):
        return services.create_edition(actor=self.owner, title=title, category=self.cat, **kw)


class CategoryTests(Base):
    def test_name_unique_case_insensitive_at_database(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            Category.objects.create(name="fiction")

    def test_get_or_create_is_case_insensitive(self):
        cat, created = services.get_or_create_category("FICTION")
        self.assertEqual((cat, created), (self.cat, False))


class IsbnTests(Base):
    def test_valid_and_invalid(self):
        self.assertEqual(services.clean_isbn("978-0-261-10221-7"), "9780261102217")
        self.assertEqual(services.clean_isbn("0-306-40615-2"), "0306406152")
        self.assertEqual(services.clean_isbn("080442957x"), "080442957X")
        self.assertEqual(services.clean_isbn(""), "")
        for bad in ("9780261102218", "123", "ABCDEFGHIJ"):
            with self.assertRaises(RuleViolation):
                services.clean_isbn(bad)

    def test_duplicate_isbn_warns_and_needs_acknowledgement(self):
        self.edition(isbn="9780261102217")
        with self.assertRaises(RuleViolation) as ctx:
            self.edition("Other", isbn="9780261102217")
        self.assertEqual(ctx.exception.code, "isbn_duplicate")
        e = self.edition("Other", isbn="9780261102217", isbn_acknowledged=True)
        self.assertEqual(BookEdition.objects.filter(isbn=e.isbn).count(), 2)  # no merge, no discard

    def test_isbn_optional_and_not_unique(self):
        self.edition("A")
        self.edition("B")
        self.assertEqual(BookEdition.objects.filter(isbn="").count(), 2)


class EditionTests(Base):
    def test_author_blank_or_unknown(self):
        self.assertEqual(self.edition("A").author, "")
        self.assertEqual(self.edition("B", author=services.UNKNOWN_AUTHOR).author, "Unknown author")

    def test_title_required_and_year_bounds(self):
        with self.assertRaises(RuleViolation):
            self.edition(" ")
        with self.assertRaises(RuleViolation):
            self.edition("X", publication_year=500)

    def test_each_edition_is_separate_record_and_never_deleted(self):
        a, b = self.edition("Same"), self.edition("Same")
        self.assertNotEqual(a.pk, b.pk)
        with self.assertRaises(RuleViolation):
            a.delete()

    def test_archive_preserves_history(self):
        e = self.edition()
        services.set_edition_archived(e, True, actor=self.owner)
        e.refresh_from_db()
        self.assertTrue(e.is_archived)
        self.assertEqual(services.search_editions("Hobbit").count(), 0)
        self.assertEqual(services.search_editions("Hobbit", archived=True).count(), 1)

    def test_one_category_per_edition_is_protected(self):
        e = self.edition()
        with self.assertRaises(ProtectedError):
            self.cat.delete()
        self.assertEqual(e.category, self.cat)


class CopyTests(Base):
    def test_copy_ids_generated_unique_and_barcode_matches(self):
        e = self.edition()
        copies = services.add_copies(e, 3, actor=self.owner, source="purchased")
        self.assertEqual(
            [c.copy_code for c in copies], ["COV-C-000001", "COV-C-000002", "COV-C-000003"]
        )
        self.assertTrue(all(c.barcode == c.copy_code for c in copies))
        more = services.add_copies(
            self.edition("Other"), 1, actor=self.owner, source="donated", donor_name="Sam"
        )
        self.assertEqual(more[0].copy_code, "COV-C-000004")
        self.assertEqual(more[0].acquisition.source, "donated")

    def test_database_unique_code_and_barcode(self):
        e = self.edition()
        c = services.add_copies(e, 1, actor=self.owner, source="purchased")[0]
        with self.assertRaises(IntegrityError), transaction.atomic():
            BookCopy.objects.create(copy_code=c.copy_code, barcode="OTHER", edition=e)
        with self.assertRaises(IntegrityError), transaction.atomic():
            BookCopy.objects.create(copy_code="OTHER", barcode=c.barcode, edition=e)

    def test_supplied_codes_validated(self):
        e = self.edition()
        with self.assertRaises(RuleViolation):
            services.add_copies(e, 2, actor=self.owner, source="purchased", codes=["A"])
        with self.assertRaises(RuleViolation):
            services.add_copies(e, 2, actor=self.owner, source="purchased", codes=["A", "A"])
        services.add_copies(e, 1, actor=self.owner, source="purchased", codes=["OLD-1"])
        with self.assertRaises(RuleViolation):
            services.add_copies(e, 1, actor=self.owner, source="purchased", codes=["OLD-1"])

    def test_new_copy_is_good_available_and_issuable(self):
        c = services.add_copies(self.edition(), 1, actor=self.owner, source="purchased")[0]
        self.assertEqual((c.condition, c.status, c.is_issuable), ("good", "available", True))
        self.assertEqual(c.events.get().event_type, "acquired")

    def test_poor_copy_must_be_withdrawn_and_not_issuable(self):
        c = services.add_copies(self.edition(), 1, actor=self.owner, source="purchased")[0]
        with self.assertRaises(RuleViolation):
            services.set_copy_state(c, condition="poor", status="available", actor=self.owner)
        c = services.set_copy_state(c, condition="poor", status="withdrawn", actor=self.owner)
        self.assertEqual((c.condition, c.status, c.is_issuable), ("poor", "withdrawn", False))

    def test_only_good_available_is_issuable(self):
        e = self.edition()
        states = [
            ("good", "available", True),
            ("good", "withdrawn", False),
            ("poor", "withdrawn", False),
            ("damaged", "damaged", False),
            ("damaged", "withdrawn", False),
            ("lost", "lost", False),
        ]
        for cond, status, issuable in states:
            c = services.add_copies(e, 1, actor=self.owner, source="purchased")[0]
            c = services.set_copy_state(c, condition=cond, status=status, actor=self.owner)
            self.assertEqual(c.is_issuable, issuable, (cond, status))

    def test_invalid_combinations_rejected_by_service_and_database(self):
        c = services.add_copies(self.edition(), 1, actor=self.owner, source="purchased")[0]
        for cond, status in [
            ("lost", "available"),
            ("damaged", "available"),
            ("good", "lost"),
            ("good", "damaged"),
            ("poor", "damaged"),
        ]:
            with self.assertRaises(RuleViolation):
                services.set_copy_state(c, condition=cond, status=status, actor=self.owner)
        with self.assertRaises(RuleViolation):
            services.set_copy_state(c, condition="good", status="borrowed", actor=self.owner)
        for cond, status in [
            ("poor", "available"),
            ("lost", "available"),
            ("good", "lost"),
            ("damaged", "available"),
            ("poor", "borrowed"),
        ]:
            with self.assertRaises(IntegrityError), transaction.atomic():
                BookCopy.objects.filter(pk=c.pk).update(condition=cond, status=status)

    def test_borrowed_copy_cannot_be_changed_manually(self):
        c = services.add_copies(self.edition(), 1, actor=self.owner, source="purchased")[0]
        BookCopy.objects.filter(pk=c.pk).update(status="borrowed")
        with self.assertRaises(RuleViolation) as ctx:
            services.set_copy_state(c, condition="good", status="withdrawn", actor=self.owner)
        self.assertEqual(ctx.exception.code, "copy_borrowed")

    def test_history_records_changes_and_is_append_only(self):
        c = services.add_copies(self.edition(), 1, actor=self.owner, source="purchased")[0]
        services.set_copy_state(
            c, condition="damaged", status="damaged", actor=self.owner, note="torn"
        )
        types = list(c.events.values_list("event_type", flat=True))
        self.assertEqual(types, ["acquired", "condition_changed", "status_changed"])
        ev = c.events.last()
        with self.assertRaises(DatabaseError), transaction.atomic():
            CopyEvent.objects.filter(pk=ev.pk).update(note="x")
        with self.assertRaises(DatabaseError), transaction.atomic():
            ev.delete()
        self.assertTrue(AuditEvent.objects.filter(action="copy.state").exists())

    def test_copy_never_deleted(self):
        c = services.add_copies(self.edition(), 1, actor=self.owner, source="purchased")[0]
        with self.assertRaises(RuleViolation):
            c.delete()


class LabelTests(Base):
    def test_default_layout_is_configurable_and_validated(self):
        layout = default_layout()
        self.assertTrue(layout.is_default)
        validate_layout(layout)
        bad = LabelLayout(
            page_width_mm=100,
            page_height_mm=100,
            label_width_mm=60,
            label_height_mm=30,
            columns=3,
            rows=8,
            margin_mm=10,
        )
        with self.assertRaises(RuleViolation):
            validate_layout(bad)

    def test_only_one_default_layout_allowed(self):
        LabelLayout.objects.create(name="A", is_default=True)
        with self.assertRaises(IntegrityError), transaction.atomic():
            LabelLayout.objects.create(name="B", is_default=True)

    def test_pdf_pages_follow_layout(self):
        e = self.edition()
        services.add_copies(e, 7, actor=self.owner, source="purchased")
        layout = LabelLayout.objects.create(
            name="Small",
            columns=2,
            rows=2,
            page_width_mm=210,
            page_height_mm=297,
            label_width_mm=60,
            label_height_mm=30,
            margin_mm=10,
        )
        pdf = render_labels_pdf(e.copies.select_related("edition"), layout)
        self.assertTrue(pdf.startswith(b"%PDF"))
        self.assertEqual(page_count(7, layout), 2)
        self.assertEqual(pdf.count(b"/Type /Page\n") + pdf.count(b"/Type /Page "), 2)


class ViewTests(Base):
    def test_all_catalogue_pages_require_login(self):
        e = self.edition()
        c = services.add_copies(e, 1, actor=self.owner, source="purchased")[0]
        for name, args in [
            ("edition_list", []),
            ("edition_create", []),
            ("edition_detail", [e.pk]),
            ("edition_edit", [e.pk]),
            ("copies_add", [e.pk]),
            ("copy_detail", [c.pk]),
            ("labels_pdf", [e.pk]),
            ("category_list", []),
            ("layout_list", []),
            ("import_start", []),
            ("import_template", []),
        ]:
            self.assertEqual(
                self.client.get(reverse(f"catalogue:{name}", args=args)).status_code, 302, name
            )

    def test_create_edition_flow_with_isbn_warning(self):
        self.client.force_login(self.owner)
        data = {
            "title": "Dune",
            "author": "",
            "publisher": "",
            "edition_label": "",
            "publication_year": "",
            "category": self.cat.pk,
            "isbn": "9780261102217",
            "description": "",
            "notes": "",
        }
        self.assertEqual(
            self.client.post(reverse("catalogue:edition_create"), data).status_code, 302
        )
        r = self.client.post(reverse("catalogue:edition_create"), {**data, "title": "Dune 2"})
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "another edition has this ISBN")
        self.assertContains(r, 'value="Dune 2"')
        r = self.client.post(
            reverse("catalogue:edition_create"),
            {**data, "title": "Dune 2", "isbn_acknowledged": "on"},
        )
        self.assertEqual(r.status_code, 302)
        self.assertEqual(BookEdition.objects.count(), 2)

    def test_add_copies_and_state_change_views(self):
        self.client.force_login(self.owner)
        e = self.edition()
        r = self.client.post(
            reverse("catalogue:copies_add", args=[e.pk]),
            {"count": 2, "source": "donated", "donor_name": "Sam", "unit_cost_rupees": "250.50"},
        )
        self.assertEqual(r.status_code, 302)
        copy = e.copies.first()
        self.assertEqual(copy.acquisition.unit_cost_paise, 25050)
        r = self.client.post(
            reverse("catalogue:copy_detail", args=[copy.pk]),
            {"condition": "poor", "status": "available"},
        )
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "cannot have status")
        r = self.client.post(
            reverse("catalogue:copy_detail", args=[copy.pk]),
            {"condition": "poor", "status": "withdrawn"},
        )
        self.assertEqual(r.status_code, 302)
        r = self.client.get(reverse("catalogue:labels_pdf", args=[e.pk]))
        self.assertEqual(r["Content-Type"], "application/pdf")

    def test_empty_state_and_search(self):
        self.client.force_login(self.owner)
        self.assertContains(self.client.get(reverse("catalogue:edition_list")), "No books found")
        e = self.edition("Searchable", author="Ann")
        services.add_copies(e, 1, actor=self.owner, source="purchased")
        r = self.client.get(reverse("catalogue:edition_list"), {"q": "COV-C-000001"})
        self.assertContains(r, "Searchable")
        r = self.client.get(reverse("catalogue:edition_list"), {"q": "zzz"})
        self.assertContains(r, "No books found")
