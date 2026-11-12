from django.db import models

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
    backup_time = models.TimeField(null=True, blank=True)
    drive_folder_id = models.CharField(max_length=200, blank=True)

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
