from datetime import datetime, time, timedelta

from django.utils import timezone


def end_of_day(now=None):
    """The next local midnight in the configured timezone (Asia/Kolkata)."""
    now = timezone.localtime(now or timezone.now())
    tomorrow = datetime.combine(now.date() + timedelta(days=1), time.min)
    return timezone.make_aware(tomorrow, now.tzinfo)


def seconds_until_end_of_day(now=None):
    now = now or timezone.now()
    return max(1, int((end_of_day(now) - now).total_seconds()))
