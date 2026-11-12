from django.contrib.auth.models import AbstractUser
from django.db import models
from django.utils import timezone


class User(AbstractUser):
    """The single owner account (no member accounts). Email is used for password reset."""

    email = models.EmailField("email address", blank=True)

    class Meta:
        db_table = "accounts_user"
        constraints = [
            # Single-owner rule (PRD §4.1): the table can never hold more than one account.
            models.UniqueConstraint(models.Value(1), name="accounts_single_owner"),
        ]


class LoginEvent(models.Model):
    """Every authentication attempt. Drives rate limiting (5 failures / 15 min) and the audit trail.

    Never stores passwords. There is no account lockout: failures only throttle further attempts.
    """

    class Outcome(models.TextChoices):
        SUCCESS = "success", "Success"
        FAILED = "failed", "Failed"
        THROTTLED = "throttled", "Throttled"
        RECOVERY_FAILED = "recovery_failed", "Recovery code failed"
        RESET_REQUESTED = "reset_requested", "Password reset requested"
        RESET_THROTTLED = "reset_throttled", "Password reset throttled"

    user = models.ForeignKey(
        User, null=True, blank=True, on_delete=models.PROTECT, related_name="login_events"
    )
    # Only a username that matches a real account is stored. Anything else (including a password
    # typed into the username box) is never persisted; `username_key` is a keyed hash used only
    # to rate-limit repeated attempts.
    username_tried = models.CharField(max_length=150, blank=True)
    username_key = models.CharField(max_length=64, blank=True, default="")
    outcome = models.CharField(max_length=20, choices=Outcome.choices)
    ip = models.GenericIPAddressField(null=True, blank=True)
    occurred_at = models.DateTimeField(default=timezone.now)

    class Meta:
        indexes = [
            models.Index(fields=["outcome", "occurred_at"], name="login_outcome_time_idx"),
            models.Index(fields=["username_tried", "occurred_at"], name="login_user_time_idx"),
            models.Index(fields=["username_key", "occurred_at"], name="login_key_time_idx"),
            models.Index(fields=["ip", "occurred_at"], name="login_ip_time_idx"),
        ]

    def __str__(self):
        return f"{self.occurred_at:%Y-%m-%d %H:%M} {self.outcome}"


class RecoveryCode(models.Model):
    """Single-use password-recovery code: stored hashed, shown once, expires after 15 minutes."""

    user = models.ForeignKey(User, on_delete=models.PROTECT, related_name="recovery_codes")
    code_hash = models.CharField(max_length=256)
    created_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"recovery code for {self.user_id} (expires {self.expires_at:%H:%M})"
