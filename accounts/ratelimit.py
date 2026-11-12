"""Failed-login and password-reset rate limiting (database-backed, safe under concurrency).

Login: 5 failed attempts within 15 minutes throttle further attempts. This is NOT an account
lockout: the account stays active, the throttle lifts when the window passes, a successful login
resets the counter, and throttled attempts are recorded but never extend the window.

Callers must hold ``accounts.locks.advisory_locks`` for the limiter keys inside one transaction
while they check, authenticate and record, so simultaneous requests cannot exceed the limit.

Password reset: at most 3 reset requests per 15 minutes per email key or per client address.
(The reset numbers are an implementation default: no approved requirement specifies them.)
"""

from datetime import timedelta

from django.utils import timezone

from .models import LoginEvent

MAX_FAILURES = 5
WINDOW = timedelta(minutes=15)
FAILURE_KINDS = (LoginEvent.Outcome.FAILED, LoginEvent.Outcome.RECOVERY_FAILED)

MAX_RESET_REQUESTS = 3
RESET_WINDOW = timedelta(minutes=15)


def lock_keys(username_key, ip, prefix="login"):
    return [
        f"{prefix}:user:{username_key}" if username_key else "",
        f"{prefix}:ip:{ip}" if ip else "",
    ]


def _counted(filter_kwargs, kinds, window, now, reset_on_success):
    start = now - window
    if reset_on_success:
        last_success = (
            LoginEvent.objects.filter(outcome=LoginEvent.Outcome.SUCCESS, **filter_kwargs)
            .order_by("-occurred_at")
            .values_list("occurred_at", flat=True)
            .first()
        )
        if last_success:
            start = max(start, last_success)
    return sorted(
        LoginEvent.objects.filter(
            outcome__in=kinds, occurred_at__gt=start, **filter_kwargs
        ).values_list("occurred_at", flat=True)
    )


def _wait(events, limit, window, now):
    if len(events) >= limit:
        # The throttle lifts once the oldest of the counted events leaves the window.
        return events[-limit] + window - now
    return None


def retry_after(username_key, ip, now=None):
    """Return a timedelta until login attempts are allowed again, or None if not throttled."""
    now = now or timezone.now()
    waits = []
    keys = []
    if username_key:
        keys.append({"username_key": username_key})
    if ip:
        keys.append({"ip": ip})
    for key in keys:
        wait = _wait(_counted(key, FAILURE_KINDS, WINDOW, now, True), MAX_FAILURES, WINDOW, now)
        if wait is not None:
            waits.append(wait)
    return max(waits) if waits else None


def reset_retry_after(email_key, ip, now=None):
    """Return a timedelta until another reset email may be requested, or None."""
    now = now or timezone.now()
    kinds = (LoginEvent.Outcome.RESET_REQUESTED,)
    waits = []
    keys = []
    if email_key:
        keys.append({"username_key": email_key})
    if ip:
        keys.append({"ip": ip})
    for key in keys:
        wait = _wait(
            _counted(key, kinds, RESET_WINDOW, now, False), MAX_RESET_REQUESTS, RESET_WINDOW, now
        )
        if wait is not None:
            waits.append(wait)
    return max(waits) if waits else None
