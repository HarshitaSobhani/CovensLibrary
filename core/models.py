import uuid
from datetime import time

from django.db import models
from django.utils import timezone

from .money import MAX_PAISE


class LibrarySetting(models.Model):
    """Singleton settings row (id is always 1). Changing it never rewrites history."""

    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    library_name = models.CharField(max_length=120, default="Coven's Library")
    library_address = models.CharField(max_length=300, blank=True)
    library_phone = models.CharField(max_length=40, blank=True)
    library_email = models.EmailField(blank=True)
    logo_file = models.CharField(max_length=200, blank=True)
    loan_days = models.PositiveSmallIntegerField(default=30)
    grace_days = models.PositiveSmallIntegerField(default=7)
    token_amount_paise = models.PositiveBigIntegerField(default=50000)
    issue_block_days = models.PositiveSmallIntegerField(default=15)
    receipt_footer = models.CharField(max_length=300, blank=True)
    backup_time = models.TimeField(default=time(19, 45))  # D-29b: 19:45 Asia/Kolkata
    drive_folder_id = models.CharField(max_length=200, blank=True)
    dashboard_config = models.JSONField(default=dict, blank=True)  # see core/dashboard_config.py

    class Meta:
        constraints = [
            models.CheckConstraint(condition=models.Q(id=1), name="library_setting_singleton"),
            models.CheckConstraint(condition=models.Q(loan_days__gt=0), name="loan_days_positive"),
            models.CheckConstraint(
                condition=models.Q(token_amount_paise__gt=0), name="token_amount_positive"
            ),
            models.CheckConstraint(
                condition=models.Q(token_amount_paise__lte=MAX_PAISE),
                name="token_amount_within_limit",
            ),
        ]

    def __str__(self):
        return self.library_name

    def save(self, *args, **kwargs):
        self.pk = 1
        # Singleton: always update the one row if it exists.
        self._state.adding = not type(self).objects.filter(pk=1).exists()
        super().save(*args, **kwargs)

    @classmethod
    def load(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


class IdSequence(models.Model):
    """Gap-tolerant, race-safe counters keyed by (name, year). year=0 for non-yearly sequences."""

    name = models.CharField(max_length=40)
    year = models.PositiveSmallIntegerField(default=0)
    last_value = models.PositiveBigIntegerField(default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["name", "year"], name="id_sequence_unique_name_year")
        ]

    def __str__(self):
        return f"{self.name}/{self.year}={self.last_value}"


class SettingChangeEvent(models.Model):
    """A recorded change of a business setting (append-only). For the grace period it lists the
    loans that became newly eligible for token forfeiture (they need the owner's decision)."""

    setting_name = models.CharField(max_length=60)
    old_value = models.CharField(max_length=60)
    new_value = models.CharField(max_length=60)
    actor = models.ForeignKey("accounts.User", on_delete=models.PROTECT, related_name="+")
    changed_at = models.DateTimeField(default=timezone.now)
    affected_loans = models.JSONField(default=list, blank=True)

    class Meta:
        ordering = ["id"]

    def __str__(self):
        return f"{self.setting_name}: {self.old_value} -> {self.new_value}"


class DomainEvent(models.Model):
    """Transactional outbox: one durable row per notifiable business fact (APPEND_ONLY).

    The row is inserted in the SAME database transaction as the business operation (see
    ``core.outbox.emit``), so a rolled-back operation leaves no event and a committed one can never
    lose it. ``dedupe_key`` is unique: the same fact can never produce two events. Phase 5b reads
    events that have no notification record yet (matched on ``dedupe_key``); this table is never
    updated - delivery, retry and skip state live in the notification table. The payload holds ids
    and codes only (no names, contact details or credentials); the delivery step reads live member
    data, so anonymisation is honoured.
    """

    event_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    dedupe_key = models.CharField(max_length=200, unique=True)
    event_type = models.CharField(max_length=40)
    member_id = models.PositiveBigIntegerField(
        null=True,
        blank=True,
        db_index=True,
        help_text="members.Member id (no foreign key: core is imported by every app)",
    )
    payload = models.JSONField(default=dict, blank=True)
    event_date = models.DateField(help_text="Business date of the fact (Asia/Kolkata).")
    occurred_at = models.DateTimeField(help_text="When the fact was processed.")
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["id"]
        constraints = [
            models.CheckConstraint(
                condition=~models.Q(dedupe_key=""), name="domain_event_dedupe_key_not_empty"
            ),
            models.CheckConstraint(
                condition=~models.Q(event_type=""), name="domain_event_type_not_empty"
            ),
        ]
        indexes = [models.Index(fields=["event_type", "id"], name="domain_event_type_idx")]

    def __str__(self):
        return f"{self.event_type} {self.dedupe_key}"
