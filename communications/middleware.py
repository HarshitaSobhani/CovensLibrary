"""Send immediate notifications right after an owner action has been saved.

The durable record is the outbox event written in the business transaction; this only makes the
email follow promptly. If it fails the response is unaffected and the next cycle (or
``run_daily_jobs``) picks the work up, because an unconsumed event simply has no record yet."""

import logging

from django.conf import settings

log = logging.getLogger(__name__)


class NotifyAfterWriteMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if (
            getattr(settings, "NOTIFY_AFTER_WRITE", True)
            and request.method == "POST"
            and response.status_code < 400
            and getattr(request.user, "is_authenticated", False)
        ):
            try:
                from . import services

                services.run_cycle()
            except Exception:  # noqa: BLE001 - never break the owner's action
                log.exception("notification cycle after write failed")
        return response
