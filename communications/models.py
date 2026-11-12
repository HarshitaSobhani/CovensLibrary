from django.db import models
from django.utils import timezone


class Notification(models.Model):
    """One email, from creation to its final state. It is the ONLY place delivery state lives:
    the outbox events (core.DomainEvent) stay immutable business facts. ``dedupe_key`` is unique,
    so the same event or reminder can never produce a second record (or a second email); retries
    reuse this row. ``recipient_email`` is the address actually used (resolved from the member at
    processing time) and is kept."""

    class Kind(models.TextChoices):
        SUBSCRIPTION_ACTIVATION = "subscription_activation", "Subscription activation"
        EXPIRY_REMINDER = "expiry_reminder", "Upcoming subscription expiry"
        SUBSCRIPTION_EXPIRED = "subscription_expired", "Subscription expiry notice"
        OVERDUE_REMINDER = "overdue_reminder", "Overdue / due-date reminder"
        PAYMENT_RECEIPT = "payment_receipt", "Payment receipt"
        LOAN_ISSUED = "loan_issued", "Loan issue"
        LOAN_RENEWED = "loan_renewed", "Loan renewal"
        LOAN_RETURNED = "loan_returned", "Book return"
        TOKEN_FORFEITED = "token_forfeited", "Token forfeiture"
        NONE = "none", "No email defined for this event"

    class Status(models.TextChoices):
        PREPARED = "prepared", "Prepared"  # not attempted yet
        SMTP_ACCEPTED = "smtp_accepted", "SMTP accepted"  # NOT proof of delivery or reading
        SMTP_FAILED = "smtp_failed", "SMTP failed (will retry)"
        FAILED = "failed", "Failed (retries used up)"
        SKIPPED = "skipped", "Skipped"

    class Source(models.TextChoices):
        EVENT = "event", "Outbox event"
        SCHEDULE = "schedule", "Scheduled reminder"

    class SkipCode(models.TextChoices):
        NO_EMAIL = "no_email", "No email address"
        ANONYMISED = "anonymised", "Member anonymised"
        NOT_NOTIFIED = "not_notified", "No email defined for this event"

    dedupe_key = models.CharField(max_length=200, unique=True)
    source = models.CharField(max_length=10, choices=Source.choices)
    event_id = models.UUIDField(null=True, blank=True, unique=True)  # the outbox event, if any
    kind = models.CharField(max_length=30, choices=Kind.choices)
    # The date a scheduled reminder was DUE (kept even when it is created later, after an
    # offline day); created_at / last_attempt_at record when it was really processed.
    scheduled_for = models.DateField(null=True, blank=True)
    member = models.ForeignKey(
        "members.Member", on_delete=models.PROTECT, related_name="notifications"
    )
    recipient_email = models.CharField(max_length=254, blank=True)
    subject = models.CharField(max_length=300, blank=True)
    body = models.TextField(blank=True)
    status = models.CharField(max_length=14, choices=Status.choices, default=Status.PREPARED)
    skip_code = models.CharField(max_length=14, blank=True, choices=SkipCode.choices)
    skip_reason = models.CharField(max_length=300, blank=True)
    attempt_count = models.PositiveSmallIntegerField(default=0)
    next_attempt_at = models.DateTimeField(null=True, blank=True)
    last_attempt_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True)
    accepted_at = models.DateTimeField(null=True, blank=True)
    claim_token = models.UUIDField(null=True, blank=True)
    claimed_until = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-id"]
        indexes = [
            models.Index(fields=["status", "next_attempt_at"], name="notification_due_idx"),
            models.Index(fields=["member"], name="notification_member_idx"),
        ]
        constraints = [
            models.CheckConstraint(
                condition=~models.Q(dedupe_key=""), name="notification_key_not_empty"
            ),
            models.CheckConstraint(
                condition=models.Q(
                    status__in=["prepared", "smtp_accepted", "smtp_failed", "failed", "skipped"]
                ),
                name="notification_status_valid",
            ),
            models.CheckConstraint(
                condition=models.Q(attempt_count__lte=4), name="notification_attempts_max_4"
            ),
            models.CheckConstraint(
                condition=~models.Q(status="skipped") | ~models.Q(skip_code=""),
                name="notification_skip_has_code",
            ),
            models.CheckConstraint(
                condition=~models.Q(status="smtp_accepted") | models.Q(accepted_at__isnull=False),
                name="notification_accepted_has_time",
            ),
        ]

    def __str__(self):
        return f"{self.kind} to member {self.member_id} ({self.status})"

    @property
    def scheduled_date(self):
        """The date a scheduled reminder was due. Records created before back-fill support have
        ``scheduled_for = NULL``; for those the creation date (local) is the best available."""
        if self.source != self.Source.SCHEDULE:
            return None
        return self.scheduled_for or timezone.localtime(self.created_at).date()

    @property
    def is_open(self):
        return self.status in (self.Status.PREPARED, self.Status.SMTP_FAILED)


class NotificationAttempt(models.Model):
    """Every SMTP attempt (append-only). It holds no address or message text, so anonymisation
    never needs to rewrite it; the error is stored with the recipient address removed."""

    notification = models.ForeignKey(
        Notification, on_delete=models.PROTECT, related_name="attempts"
    )
    attempt_no = models.PositiveSmallIntegerField()
    attempted_at = models.DateTimeField(default=timezone.now)
    accepted = models.BooleanField()
    error = models.TextField(blank=True)

    class Meta:
        ordering = ["attempt_no"]
        constraints = [
            models.UniqueConstraint(
                fields=["notification", "attempt_no"], name="notification_attempt_unique"
            ),
            models.CheckConstraint(
                condition=models.Q(accepted=True) | ~models.Q(error=""),
                name="notification_failed_attempt_has_error",
            ),
        ]

    def __str__(self):
        return f"attempt {self.attempt_no} of notification {self.notification_id}"


class MessageTemplate(models.Model):
    """Owner-editable WhatsApp message text (placeholders are a fixed whitelist)."""

    key = models.CharField(max_length=20, unique=True)
    body = models.TextField()
    updated_at = models.DateTimeField(default=timezone.now)

    def __str__(self):
        return self.key
