from .models import AuditEvent
from .redaction import REDACTED, scrub, scrub_text

__all__ = ["record", "scrub", "scrub_text", "REDACTED"]


def record(action, actor=None, entity_type="", entity_id="", summary=None):
    """Write an immutable audit event. Everything is redacted first (see audit/redaction.py)."""
    return AuditEvent.objects.create(
        actor=actor if (actor is not None and getattr(actor, "pk", None)) else None,
        action=scrub_text(str(action))[:80],
        entity_type=scrub_text(str(entity_type))[:80],
        entity_id=scrub_text(str(entity_id))[:80] if entity_id != "" else "",
        summary=scrub(summary or {}),
    )
