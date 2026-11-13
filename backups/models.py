from django.db import models
from django.utils import timezone


class BackupRun(models.Model):
    """One backup attempt. History is never deleted. The monthly backups are not a separate row
    kind: they are COMPUTED from the successful runs (the last of each calendar month, D-29c)."""

    class Kind(models.TextChoices):
        DAILY = "daily", "Daily"
        PRE_RESTORE = "pre_restore", "Safety backup before a restore"

    class Trigger(models.TextChoices):
        SCHEDULED = "scheduled", "Scheduled"
        CATCHUP = "catchup", "Catch-up at launch"
        MANUAL = "manual", "Manual"
        PRE_RESTORE = "pre_restore", "Before a restore"

    class Status(models.TextChoices):
        RUNNING = "running", "Running"
        SUCCEEDED = "succeeded", "Succeeded"
        FAILED = "failed", "Failed"

    kind = models.CharField(max_length=12, choices=Kind.choices, default=Kind.DAILY)
    trigger = models.CharField(max_length=12, choices=Trigger.choices, default=Trigger.MANUAL)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.RUNNING)
    started_at = models.DateTimeField(default=timezone.now)
    finished_at = models.DateTimeField(null=True, blank=True)
    file_name = models.CharField(max_length=200, blank=True)
    size_bytes = models.PositiveBigIntegerField(null=True, blank=True)
    sha256 = models.CharField(max_length=64, blank=True)  # of the ENCRYPTED file
    key_fingerprint = models.CharField(max_length=32, blank=True)  # non-secret key id
    destination = models.CharField(max_length=40, blank=True)
    remote_id = models.CharField(max_length=300, blank=True)
    manifest = models.JSONField(default=dict, blank=True)
    error_summary = models.CharField(max_length=500, blank=True)
    verified_at = models.DateTimeField(null=True, blank=True)  # upload verified at this time
    remote_deleted_at = models.DateTimeField(null=True, blank=True)  # retention removed the file

    class Meta:
        ordering = ["-started_at", "-id"]
        indexes = [models.Index(fields=["status", "started_at"], name="backuprun_status_idx")]
        constraints = [
            models.UniqueConstraint(
                fields=["status"],
                condition=models.Q(status="running"),
                name="backuprun_one_running",
            ),
            models.CheckConstraint(
                condition=models.Q(status__in=["running", "succeeded", "failed"]),
                name="backuprun_status_valid",
            ),
            models.CheckConstraint(
                condition=models.Q(kind__in=["daily", "pre_restore"]), name="backuprun_kind_valid"
            ),
            models.CheckConstraint(
                condition=~models.Q(status="succeeded")
                | (
                    ~models.Q(sha256="")
                    & ~models.Q(remote_id="")
                    & models.Q(size_bytes__gt=0)
                    & models.Q(finished_at__isnull=False)
                    & models.Q(verified_at__isnull=False)
                ),
                name="backuprun_success_is_verified",
            ),
        ]

    def __str__(self):
        return f"{self.get_kind_display()} {self.status} {self.started_at:%Y-%m-%d %H:%M}"


class RestoreRun(models.Model):
    """One restore (or refused/failed restore attempt) recorded in the restored database."""

    class Status(models.TextChoices):
        SUCCEEDED = "succeeded", "Restored and verified"
        VERIFICATION_FAILED = "verification_failed", "Restored but verification failed"
        FAILED = "failed", "Failed"

    backup_file_name = models.CharField(max_length=200)
    backup_sha256 = models.CharField(max_length=64, blank=True)
    key_fingerprint = models.CharField(max_length=32, blank=True)
    target_database = models.CharField(max_length=63)
    status = models.CharField(max_length=20, choices=Status.choices)
    started_at = models.DateTimeField(default=timezone.now)
    finished_at = models.DateTimeField(default=timezone.now)
    pre_restore = models.JSONField(default=dict, blank=True)  # the safety backup, if one was made
    verification = models.JSONField(default=dict, blank=True)
    key_imported = models.BooleanField(default=False)
    error_summary = models.CharField(max_length=500, blank=True)

    class Meta:
        ordering = ["-started_at", "-id"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(status__in=["succeeded", "verification_failed", "failed"]),
                name="restorerun_status_valid",
            ),
        ]

    def __str__(self):
        return f"restore {self.status} {self.started_at:%Y-%m-%d %H:%M}"
