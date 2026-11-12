"""Safe handling of login identifiers: never persist what a user typed into the username box."""

import hashlib
import hmac

from django.conf import settings
from django.contrib.auth import get_user_model

UNRECOGNISED = "[unrecognised]"


def normalise(typed):
    return (typed or "").strip().lower()


def key_for(typed):
    """Keyed, non-reversible identifier for rate limiting (HMAC-SHA256 under SECRET_KEY)."""
    value = normalise(typed)
    if not value:
        return ""
    return hmac.new(settings.SECRET_KEY.encode(), value.encode(), hashlib.sha256).hexdigest()


def known_username(typed):
    """The real username if `typed` matches an account, else '' (so nothing typed is stored)."""
    value = (typed or "").strip()
    if not value or len(value) > 150:
        return ""
    found = (
        get_user_model().objects.filter(username__iexact=value).values_list("username", flat=True)
    )
    return found.first() or ""


def audit_username(typed):
    return known_username(typed) or UNRECOGNISED
