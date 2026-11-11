"""Login rate limiting: 5 failed attempts within 15 minutes throttle further attempts.

Not an account lockout: the account stays active, the throttle lifts when the window passes, and a
successful login resets the counter. Throttled attempts are recorded but do not extend the window.
"""

from datetime import timedelta

from django.utils import timezone

from .models import LoginEvent

MAX_FAILURES = 5
WINDOW = timedelta(minutes=15)
FAILURE_KINDS = (LoginEvent.Outcome.FAILED, LoginEvent.Outcome.RECOVERY_FAILED)


def _failures_for(filter_kwargs, now):
    window_start = now - WINDOW
    last_success = (
        LoginEvent.objects.filter(outcome=LoginEvent.Outcome.SUCCESS, **filter_kwargs)
        .order_by("-occurred_at")
        .values_list("occurred_at", flat=True)
        .first()
    )
    start = max(window_start, last_success) if last_success else window_start
    return list(
        LoginEvent.objects.filter(
            outcome__in=FAILURE_KINDS, occurred_at__gt=start, **filter_kwargs
        ).values_list("occurred_at", flat=True)
    )


def retry_after(username, ip, now=None):
    """Return a timedelta until attempts are allowed again, or None if not throttled."""
    now = now or timezone.now()
    waits = []
    keys = []
    if username:
        keys.append({"username_tried__iexact": username})
    if ip:
        keys.append({"ip": ip})
    for key in keys:
        failures = sorted(_failures_for(key, now))
        if len(failures) >= MAX_FAILURES:
            # Throttle lifts once the oldest of the counted failures leaves the window.
            oldest_counted = failures[-MAX_FAILURES]
            waits.append(oldest_counted + WINDOW - now)
    return max(waits) if waits else None
