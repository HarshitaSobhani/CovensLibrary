from django.conf import settings
from django.db import models
from django.db.models.functions import Lower
from django.utils import timezone

from core.errors import RuleViolation
from core.money import MAX_PAISE


class Category(models.Model):
    name = models.CharField(max_length=80)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]
        verbose_name_plural = "categories"
        constraints = [models.UniqueConstraint(Lower("name"), name="category_name_ci_unique")]

    def __str__(self):
        return self.name


class BookEdition(models.Model):
    """One bibliographic edition. Each edition is its own record; copies hang off it."""

    title = models.CharField(max_length=300)
    # Required when a book is created or imported; legacy records may still hold a blank author.
    author = models.CharField(max_length=200, blank=True)
    publisher = models.CharField(max_length=200, blank=True)
    edition_label = models.CharField(max_length=100, blank=True)
    publication_year = models.PositiveSmallIntegerField(null=True, blank=True)
    category = models.ForeignKey(
        Category, on_delete=models.PROTECT, related_name="editions", null=True, blank=True
    )  # optional: incomplete records stay editable
    isbn = models.CharField(max_length=13, blank=True, db_index=False)
    description = models.TextField(blank=True)
    notes = models.TextField(blank=True)
    is_archived = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["title"]
        indexes = [
            models.Index(Lower("title"), name="edition_title_ci_idx"),
            models.Index(Lower("author"), name="edition_author_ci_idx"),
            models.Index(fields=["isbn"], condition=~models.Q(isbn=""), name="edition_isbn_idx"),
        ]

    def __str__(self):
        return self.title

    @property
    def missing_metadata(self):
        """Optional catalogue fields still empty, so the owner can complete them later."""
        checks = (
            ("Category", self.category_id is not None),
            ("ISBN", bool(self.isbn)),
            ("Publisher", bool(self.publisher)),
            ("Edition", bool(self.edition_label)),
            ("Publication year", self.publication_year is not None),
            ("Description", bool(self.description.strip())),
            ("Notes", bool(self.notes.strip())),
        )
        return [name for name, present in checks if not present]

    def delete(self, *args, **kwargs):
        raise RuleViolation("edition_delete_blocked", "Editions are archived, never deleted.")


class Acquisition(models.Model):
    class Source(models.TextChoices):
        PURCHASED = "purchased", "Purchased"
        DONATED = "donated", "Donated"

    edition = models.ForeignKey(BookEdition, on_delete=models.PROTECT, related_name="acquisitions")
    source = models.CharField(max_length=10, choices=Source.choices)
    donor_name = models.CharField(max_length=200, blank=True)
    donor_contact = models.CharField(max_length=200, blank=True)
    acquired_on = models.DateField(null=True, blank=True)
    unit_cost_paise = models.PositiveBigIntegerField(null=True, blank=True)
    notes = models.TextField(blank=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(unit_cost_paise__isnull=True)
                | models.Q(unit_cost_paise__lte=MAX_PAISE),
                name="acquisition_cost_within_limit",
            )
        ]

    def __str__(self):
        return f"{self.edition_id} {self.source}"


class BookCopy(models.Model):
    """A physical copy. Condition and circulation status are separate fields (B14)."""

    class Condition(models.TextChoices):
        GOOD = "good", "Good"
        POOR = "poor", "Poor"
        DAMAGED = "damaged", "Damaged"
        LOST = "lost", "Lost"

    class Status(models.TextChoices):
        AVAILABLE = "available", "Available"
        BORROWED = "borrowed", "Borrowed"
        DAMAGED = "damaged", "Damaged"
        LOST = "lost", "Lost"
        WITHDRAWN = "withdrawn", "Withdrawn"

    copy_code = models.CharField(max_length=40, unique=True)
    barcode = models.CharField(max_length=40, unique=True)
    edition = models.ForeignKey(BookEdition, on_delete=models.PROTECT, related_name="copies")
    acquisition = models.ForeignKey(
        Acquisition, null=True, blank=True, on_delete=models.PROTECT, related_name="copies"
    )
    condition = models.CharField(max_length=10, choices=Condition.choices, default="good")
    status = models.CharField(max_length=10, choices=Status.choices, default="available")
    shelf_location = models.CharField(max_length=80, blank=True)
    notes = models.TextField(blank=True)
    legacy_ref = models.CharField(max_length=80, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["copy_code"]
        verbose_name_plural = "book copies"
        constraints = [
            models.CheckConstraint(
                condition=~models.Q(status__in=["available", "borrowed"])
                | models.Q(condition="good"),
                name="copy_available_or_borrowed_requires_good",
            ),
            models.CheckConstraint(
                condition=~models.Q(condition="poor") | models.Q(status="withdrawn"),
                name="copy_poor_requires_withdrawn",
            ),
            models.CheckConstraint(
                condition=models.Q(condition="lost", status="lost")
                | (~models.Q(condition="lost") & ~models.Q(status="lost")),
                name="copy_lost_condition_matches_status",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(condition="damaged", status__in=["damaged", "withdrawn"])
                    | (~models.Q(condition="damaged") & ~models.Q(status="damaged"))
                ),
                name="copy_damaged_condition_matches_status",
            ),
        ]
        indexes = [
            models.Index(fields=["status"], name="copy_status_idx"),
            models.Index(
                fields=["legacy_ref"],
                condition=~models.Q(legacy_ref=""),
                name="copy_legacy_ref_idx",
            ),
        ]

    def __str__(self):
        return self.copy_code

    @property
    def is_issuable(self):
        return self.condition == self.Condition.GOOD and self.status == self.Status.AVAILABLE

    def delete(self, *args, **kwargs):
        raise RuleViolation(
            "copy_delete_blocked", "Copies keep their history and are never deleted."
        )


class CopyEvent(models.Model):
    """Append-only copy history (acquisition, condition and status changes; loans later)."""

    class Type(models.TextChoices):
        ACQUIRED = "acquired", "Acquired"
        ISSUED = "issued", "Issued"
        RETURNED = "returned", "Returned"
        CONDITION_CHANGED = "condition_changed", "Condition changed"
        STATUS_CHANGED = "status_changed", "Status changed"

    copy = models.ForeignKey(BookCopy, on_delete=models.PROTECT, related_name="events")
    event_type = models.CharField(max_length=20, choices=Type.choices)
    from_value = models.CharField(max_length=40, blank=True)
    to_value = models.CharField(max_length=40, blank=True)
    loan = models.ForeignKey(
        "circulation.Loan", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    note = models.TextField(blank=True)
    event_date = models.DateField(default=timezone.localdate)
    processed_at = models.DateTimeField(default=timezone.now)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )

    class Meta:
        ordering = ["id"]

    def __str__(self):
        return f"{self.copy_id} {self.event_type}"


class LabelLayout(models.Model):
    """Owner-configurable barcode label sheet (no fixed printer or label stock)."""

    name = models.CharField(max_length=80, unique=True)
    page_width_mm = models.DecimalField(max_digits=6, decimal_places=2, default=210)
    page_height_mm = models.DecimalField(max_digits=6, decimal_places=2, default=297)
    label_width_mm = models.DecimalField(max_digits=6, decimal_places=2, default=60)
    label_height_mm = models.DecimalField(max_digits=6, decimal_places=2, default=30)
    columns = models.PositiveSmallIntegerField(default=3)
    rows = models.PositiveSmallIntegerField(default=8)
    margin_mm = models.DecimalField(max_digits=6, decimal_places=2, default=10)
    is_default = models.BooleanField(default=False)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(columns__gt=0, rows__gt=0), name="label_grid_positive"
            ),
            models.UniqueConstraint(
                fields=["is_default"],
                condition=models.Q(is_default=True),
                name="label_single_default",
            ),
        ]

    def __str__(self):
        return self.name


class ImportBatch(models.Model):
    """One staged Excel import. Nothing touches the catalogue until the owner confirms."""

    class Status(models.TextChoices):
        VALIDATED = "validated", "Validated"
        CONFIRMED = "confirmed", "Confirmed"
        REJECTED = "rejected", "Rejected"

    class CopyIdMode(models.TextChoices):
        GENERATE = "generate", "Generate new copy IDs"
        SUPPLIED = "supplied", "Use supplied legacy copy IDs"

    filename = models.CharField(max_length=255)
    sha256 = models.CharField(max_length=64)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.VALIDATED)
    create_missing_categories = models.BooleanField(default=False)
    copy_id_mode = models.CharField(max_length=10, choices=CopyIdMode.choices, default="generate")
    isbn_warnings_acknowledged = models.BooleanField(default=False)
    summary = models.JSONField(default=dict, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField(default=timezone.now)

    def __str__(self):
        return f"import {self.pk} {self.filename}"


class ImportRow(models.Model):
    batch = models.ForeignKey(ImportBatch, on_delete=models.CASCADE, related_name="rows")
    row_number = models.PositiveIntegerField()
    data = models.JSONField(default=dict)  # original row, preserved as read
    clean = models.JSONField(default=dict, blank=True)
    is_valid = models.BooleanField(default=False)
    errors = models.JSONField(default=list, blank=True)
    warnings = models.JSONField(default=list, blank=True)

    class Meta:
        ordering = ["row_number"]
        constraints = [
            models.UniqueConstraint(fields=["batch", "row_number"], name="import_row_unique")
        ]

    def __str__(self):
        return f"{self.batch_id}#{self.row_number}"
