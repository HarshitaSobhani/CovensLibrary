from django.db import models

from core.errors import RuleViolation


class Member(models.Model):
    """A library member (no login). `membership_no` is permanent and never reused."""

    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        SUSPENDED = "suspended", "Suspended"
        ARCHIVED = "archived", "Archived"

    membership_no = models.CharField(max_length=20, unique=True, editable=False)
    full_name = models.CharField(max_length=200)
    mobile_e164 = models.CharField(max_length=32, unique=True)
    email = models.EmailField(blank=True)
    joined_on = models.DateField()
    notes = models.TextField(blank=True)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.ACTIVE)
    needs_new_subscription = models.BooleanField(
        default=False,
        help_text="Owner reactivated the member choosing 'require a new subscription'.",
    )
    anonymised_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["membership_no"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(status__in=["active", "suspended", "archived"]),
                name="member_status_valid",
            ),
        ]
        indexes = [
            models.Index(fields=["full_name"], name="member_name_idx"),
            models.Index(fields=["status"], name="member_status_idx"),
        ]

    def __str__(self):
        return f"{self.membership_no} {self.full_name}"

    def delete(self, *args, **kwargs):
        raise RuleViolation(
            "member_delete_blocked",
            "Members are never hard-deleted. Archive or anonymise the member instead.",
        )
