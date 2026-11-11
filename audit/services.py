from .models import AuditEvent

SENSITIVE_KEYS = (
    "password",
    "secret",
    "api_key",
    "otp",
    "recovery_code",
    "code_hash",
    "passphrase",
)
REDACTED = "[redacted]"


def scrub(value):
    """Return a copy of a summary with secret-looking keys redacted (recursively)."""
    if isinstance(value, dict):
        return {
            k: (REDACTED if any(s in str(k).lower() for s in SENSITIVE_KEYS) else scrub(v))
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [scrub(v) for v in value]
    return value


def record(action, actor=None, entity_type="", entity_id="", summary=None):
    """Write an immutable audit event. Summaries must be safe, small descriptions of the change."""
    return AuditEvent.objects.create(
        actor=actor if (actor is not None and getattr(actor, "pk", None)) else None,
        action=action,
        entity_type=entity_type,
        entity_id=str(entity_id) if entity_id != "" else "",
        summary=scrub(summary or {}),
    )
