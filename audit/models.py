from django.conf import settings
from django.db import models
from django.utils import timezone


class AuditEvent(models.Model):
    """Append-only record of a sensitive action (DB trigger rejects UPDATE/DELETE)."""

    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    action = models.CharField(max_length=80)
    entity_type = models.CharField(max_length=80, blank=True)
    entity_id = models.CharField(max_length=80, blank=True)
    summary = models.JSONField(default=dict, blank=True)
    occurred_at = models.DateTimeField(default=timezone.now, editable=False)

    class Meta:
        indexes = [
            models.Index(fields=["entity_type", "entity_id"], name="audit_entity_idx"),
            models.Index(fields=["occurred_at"], name="audit_time_idx"),
            models.Index(fields=["action"], name="audit_action_idx"),
        ]

    def __str__(self):
        return f"{self.occurred_at:%Y-%m-%d %H:%M} {self.action}"
