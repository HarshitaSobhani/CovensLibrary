"""Loans. A loan is history: it is never deleted and, once closed, never changes (a database
guard trigger allows only the documented transitions). Dates are business dates (Asia/Kolkata);
``*_processed_at`` is when the owner actually recorded the event (backdating keeps them apart)."""

from django.conf import settings
from django.db import models
from django.utils import timezone


class Loan(models.Model):
    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        RETURNED = "returned", "Returned"
        LOST = "lost", "Lost"

    member = models.ForeignKey("members.Member", on_delete=models.PROTECT, related_name="loans")
    copy = models.ForeignKey("catalogue.BookCopy", on_delete=models.PROTECT, related_name="loans")
    subscription = models.ForeignKey(
        "memberships.MemberSubscription",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="loans",
    )
    issued_on = models.DateField(help_text="Event date (may be backdated).")
    due_on = models.DateField()
    returned_on = models.DateField(null=True, blank=True, help_text="Return or loss date.")
    issued_processed_at = models.DateTimeField(default=timezone.now)
    returned_processed_at = models.DateTimeField(null=True, blank=True)
    issue_condition = models.CharField(max_length=10, default="good")
    return_condition = models.CharField(max_length=10, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.ACTIVE)
    backdated = models.BooleanField(default=False)
    backdate_reason = models.TextField(blank=True)
    backdate_confirmed = models.BooleanField(default=False)
    return_backdated = models.BooleanField(default=False)
    return_backdate_reason = models.TextField(blank=True)
    forfeiture_entry = models.OneToOneField(
        "finance.TokenLedgerEntry",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="loan",
    )
    notes = models.TextField(blank=True)
    issued_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    returned_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )

    class Meta:
        ordering = ["-issued_on", "-id"]
        constraints = [
            # One active loan per member and one per copy (decisions: B-rules, PRD 6.7).
            models.UniqueConstraint(
                fields=["member"],
                condition=models.Q(status="active"),
                name="loan_one_active_per_member",
            ),
            models.UniqueConstraint(
                fields=["copy"],
                condition=models.Q(status="active"),
                name="loan_one_active_per_copy",
            ),
            models.CheckConstraint(
                condition=models.Q(status__in=["active", "returned", "lost"]),
                name="loan_status_valid",
            ),
            models.CheckConstraint(
                condition=models.Q(due_on__gte=models.F("issued_on")),
                name="loan_due_not_before_issue",
            ),
            models.CheckConstraint(
                condition=models.Q(issue_condition="good"),
                name="loan_issued_only_in_good_condition",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        status="active",
                        returned_on__isnull=True,
                        returned_processed_at__isnull=True,
                        return_condition="",
                    )
                    | (
                        ~models.Q(status="active")
                        & models.Q(returned_on__isnull=False, returned_processed_at__isnull=False)
                        & ~models.Q(return_condition="")
                    )
                ),
                name="loan_closed_fields_match_status",
            ),
            models.CheckConstraint(
                condition=models.Q(returned_on__isnull=True)
                | models.Q(returned_on__gte=models.F("issued_on")),
                name="loan_returned_not_before_issue",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(status="returned", return_condition__in=["good", "poor", "damaged"])
                    | models.Q(status="lost", return_condition="lost")
                    | models.Q(status="active")
                ),
                name="loan_return_condition_matches_status",
            ),
            models.CheckConstraint(
                condition=models.Q(backdated=False)
                | (~models.Q(backdate_reason="") & models.Q(backdate_confirmed=True)),
                name="loan_backdating_needs_reason_and_confirmation",
            ),
            models.CheckConstraint(
                condition=models.Q(return_backdated=False)
                | (~models.Q(return_backdate_reason="") & models.Q(returned_on__isnull=False)),
                name="loan_return_backdating_needs_reason",
            ),
        ]
        indexes = [
            models.Index(
                fields=["due_on"], condition=models.Q(status="active"), name="loan_active_due_idx"
            ),
            models.Index(fields=["member", "-issued_on"], name="loan_member_issued_idx"),
        ]

    def __str__(self):
        return f"loan {self.pk} {self.member_id}/{self.copy_id}"


class LoanRenewal(models.Model):
    """One renewal: old and new due date, the date it was recorded and notes (append-only)."""

    loan = models.ForeignKey(Loan, on_delete=models.PROTECT, related_name="renewals")
    renewed_on = models.DateField()
    renewed_at = models.DateTimeField(default=timezone.now)
    old_due_on = models.DateField()
    new_due_on = models.DateField()
    notes = models.TextField(blank=True)
    renewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )

    class Meta:
        ordering = ["id"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(new_due_on__gt=models.F("old_due_on")),
                name="renewal_extends_the_due_date",
            ),
            models.UniqueConstraint(fields=["loan", "new_due_on"], name="renewal_unique_new_due"),
            models.CheckConstraint(
                condition=models.Q(renewed_on__lte=models.F("old_due_on")),
                name="renewal_not_after_the_due_date",
            ),
        ]

    def __str__(self):
        return f"renewal {self.loan_id} -> {self.new_due_on}"


class PendingForfeiture(models.Model):
    """A forfeiture that became possible only because the grace period was changed. It needs the
    owner's explicit decision: confirm (posts the forfeiture) or decline (mandatory reason, never
    asked again for this event, and the automatic day-8 job skips the loan)."""

    class Status(models.TextChoices):
        PENDING = "pending", "Awaiting decision"
        CONFIRMED = "confirmed", "Confirmed"
        DECLINED = "declined", "Declined"

    loan = models.ForeignKey(Loan, on_delete=models.PROTECT, related_name="pending_forfeitures")
    setting_change = models.ForeignKey(
        "core.SettingChangeEvent", on_delete=models.PROTECT, related_name="pending_forfeitures"
    )
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    decline_reason = models.TextField(blank=True)
    confirm_reason = models.TextField(blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    resolved_at = models.DateTimeField(null=True, blank=True)
    resolved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    resulting_entry = models.OneToOneField(
        "finance.TokenLedgerEntry",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
    )

    class Meta:
        ordering = ["id"]
        constraints = [
            models.UniqueConstraint(
                fields=["loan", "setting_change"], name="pending_forfeiture_one_per_event"
            ),
            models.CheckConstraint(
                condition=models.Q(status__in=["pending", "confirmed", "declined"]),
                name="pending_forfeiture_status_valid",
            ),
            models.CheckConstraint(
                condition=~models.Q(status="declined") | ~models.Q(decline_reason=""),
                name="pending_forfeiture_decline_needs_reason",
            ),
            models.CheckConstraint(
                condition=~models.Q(status="confirmed")
                | (~models.Q(confirm_reason="") & models.Q(resulting_entry__isnull=False)),
                name="pending_forfeiture_confirm_needs_reason_and_entry",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(status="pending", resolved_at__isnull=True, resolved_by__isnull=True)
                    | (
                        ~models.Q(status="pending")
                        & models.Q(resolved_at__isnull=False, resolved_by__isnull=False)
                    )
                ),
                name="pending_forfeiture_resolution_fields",
            ),
        ]

    def __str__(self):
        return f"pending forfeiture loan {self.loan_id} ({self.status})"
