"""The transactional outbox: durable business events for notifications (decision D-23).

``emit`` inserts an event row inside the caller's database transaction. There is no in-memory
callback anywhere: if the surrounding operation rolls back the event never existed; if it commits
the event is stored with it and cannot be lost by a shutdown. ``dedupe_key`` is unique in the
database, so emitting the same fact twice stores one event.

Phase 5b contract (NOT implemented here): a processor reads events that have no notification
record (matched on ``dedupe_key``), claims them with ``SELECT ... FOR UPDATE SKIP LOCKED`` or by
inserting the notification row under its own unique ``dedupe_key`` (concurrent workers are then
safe and idempotent), resolves the recipient email from ``member_id`` at processing time
(never stored in an event), keeps pending / skipped / failed / retry state in the notification
table,
applies the approved SMTP retry schedule and never edits or deletes an event.
"""

import json

from django.db import IntegrityError, connection, transaction
from django.utils import timezone

from .models import DomainEvent

# Phase 3 (membership and finance)
MEMBER_REGISTERED = "member.registered"
SUBSCRIPTION_ACTIVATED = "subscription.activated"
SUBSCRIPTION_RENEWED = "subscription.renewed"
PAYMENT_RECEIPTED = "payment.receipted"
TOKEN_COLLECTED = "token.collected"
TOKEN_REFUNDED = "token.refunded"
TOKEN_FORFEITED = "token.forfeited"
# Phase 4 (circulation)
LOAN_ISSUED = "loan.issued"
LOAN_RENEWED = "loan.renewed"
LOAN_RETURNED = "loan.returned"
LOAN_LOST = "loan.lost"

EVENT_TYPES = frozenset(
    {
        MEMBER_REGISTERED,
        SUBSCRIPTION_ACTIVATED,
        SUBSCRIPTION_RENEWED,
        PAYMENT_RECEIPTED,
        TOKEN_COLLECTED,
        TOKEN_REFUNDED,
        TOKEN_FORFEITED,
        LOAN_ISSUED,
        LOAN_RENEWED,
        LOAN_RETURNED,
        LOAN_LOST,
    }
)

# A payload carries ids and codes only. These names must never appear as payload keys.
FORBIDDEN_KEYS = frozenset(
    {
        "password",
        "secret",
        "session",
        "credential",
        "email",
        "name",
        "full_name",
        "mobile",
        "phone",
        "address",
        "notes",
        "reason",
    }
)
MAX_KEY = 200


class OutboxKeyConflict(Exception):
    """A ``dedupe_key`` was reused for a materially different event (different event type, member
    or payload). The stored event is never changed; raising inside the business transaction rolls
    that transaction back, so the inconsistent operation cannot commit."""


def canonical(payload):
    """Canonical JSON: key order and whitespace do not matter."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _check_payload(payload):
    for key, value in payload.items():
        if str(key).lower() in FORBIDDEN_KEYS:
            raise ValueError(f"'{key}' must not be stored in an outbox payload")
        if isinstance(value, dict):
            _check_payload(value)


def emit(
    event_type, *, dedupe_key, member_id=None, payload=None, event_date=None, occurred_at=None
):
    """Store one event in the current transaction. Returns True if stored, False if that
    ``dedupe_key`` already exists with an identical event type, member and payload (compared as
    canonical JSON). A reused key with any difference raises ``OutboxKeyConflict``; the stored
    event is never updated or replaced."""
    if not connection.in_atomic_block:
        raise RuntimeError("outbox events must be emitted inside the business transaction")
    if event_type not in EVENT_TYPES:
        raise ValueError(f"unknown event type {event_type!r}")
    if not dedupe_key or len(dedupe_key) > MAX_KEY:
        raise ValueError("an event needs a non-empty dedupe_key of at most 200 characters")
    payload = dict(payload or {})
    _check_payload(payload)
    now = timezone.now()
    event = DomainEvent(
        dedupe_key=dedupe_key,
        event_type=event_type,
        member_id=member_id,
        payload=payload,
        event_date=event_date or timezone.localdate(),
        occurred_at=occurred_at or now,
        created_at=now,
    )
    try:
        with transaction.atomic():  # savepoint: a duplicate must not poison the caller
            event.save(force_insert=True)
    except IntegrityError:
        existing = DomainEvent.objects.filter(dedupe_key=dedupe_key).first()
        if existing is None:
            raise
        if (
            existing.event_type != event_type
            or existing.member_id != member_id
            or canonical(existing.payload) != canonical(payload)
        ):
            raise OutboxKeyConflict(
                f"dedupe_key {dedupe_key!r} already stores a different event "
                f"({existing.event_type!r}, member {existing.member_id!r})"
            ) from None
        return False  # the same fact was already stored (possibly by a concurrent worker)
    return True
