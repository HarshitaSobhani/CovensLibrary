import secrets
from datetime import timedelta

from django.contrib.auth.hashers import check_password, make_password
from django.db import transaction
from django.utils import timezone

from audit.services import record

from .models import RecoveryCode

ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no look-alike characters
EXPIRY = timedelta(minutes=15)


def _normalise(code):
    return "".join(ch for ch in code.upper() if ch.isalnum())


def issue_code(user):
    """Create a single-use code (hashed at rest); the plaintext is returned once, never stored."""
    raw = "".join(secrets.choice(ALPHABET) for _ in range(10))
    now = timezone.now()
    RecoveryCode.objects.create(
        user=user, code_hash=make_password(raw), created_at=now, expires_at=now + EXPIRY
    )
    record("auth.recovery_code_issued", actor=None, entity_type="user", entity_id=user.pk)
    return f"{raw[:5]}-{raw[5:]}"


def consume_code(user, submitted, now=None):
    """Return True and mark the code used if a valid, unused, unexpired code matches."""
    now = now or timezone.now()
    normalised = _normalise(submitted or "")
    with transaction.atomic():
        candidates = RecoveryCode.objects.select_for_update().filter(
            user=user, used_at__isnull=True, expires_at__gt=now
        )
        for candidate in candidates:
            if check_password(normalised, candidate.code_hash):
                candidate.used_at = now
                candidate.save(update_fields=["used_at"])
                return True
    return False
