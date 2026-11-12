"""Notification processing (Phase 5b). Builds on the transactional outbox, never beside it.

* ``consume_events``: every outbox event without a notification record (matched on the unique
  ``dedupe_key``) gets exactly one record - an email to send, or ``skipped`` with a reason. A
  record's existence is what marks an event consumed, so an interrupted run simply continues.
* ``schedule_reminders``: the 09:00 Asia/Kolkata reminders (expiry -15 days, expiry notice, overdue
  on the due date / day 3 / day 7), keyed ``reminder:...`` so each is created once.
* ``send_due``: claims due records with a lease (``SELECT ... FOR UPDATE SKIP LOCKED``), sends over
  SMTP outside any database transaction, then records the attempt. SMTP acceptance is stored as
  "SMTP accepted" - never as delivered or read. Failures follow the approved schedule: retry 1
  five minutes after the initial failure, retry 2 thirty minutes after retry 1 fails, retry 3 two
  hours after retry 2 fails (each from the preceding failed attempt); then ``failed``, error kept,
  no further automatic attempts.

Nothing here runs by itself: ``run_cycle`` is called after an owner action (middleware) and by
``run_daily_jobs``; unattended scheduling is Phase 7c.
"""

import logging
import re
import uuid
from datetime import date, time, timedelta

from django.conf import settings
from django.core.mail import EmailMessage
from django.db import IntegrityError, transaction
from django.db.models import Exists, OuterRef, Q
from django.utils import timezone

from core.models import DomainEvent, LibrarySetting
from members.models import Member
from memberships import policy

from . import messages
from .models import Notification, NotificationAttempt

log = logging.getLogger(__name__)

RETRY_DELAYS = (timedelta(minutes=5), timedelta(minutes=30), timedelta(hours=2))
MAX_ATTEMPTS = 1 + len(RETRY_DELAYS)
LEASE = timedelta(minutes=10)
REMINDER_TIME = time(9, 0)  # Asia/Kolkata
EXPIRY_REMINDER_DAYS = 15
OVERDUE_REMINDER_DAYS = (0, 3, 7)
ERROR_LIMIT = 2000
BACKFILL_DAYS = 7  # a missed reminder is still created for up to 7 calendar days (D-25)
ADDRESS_RETENTION = timedelta(days=90)  # accepted emails keep the address this long (D-26)

K = Notification.Kind
S = Notification.Status
EVENT_KINDS = {
    "subscription.activated": K.SUBSCRIPTION_ACTIVATION,
    "subscription.renewed": K.SUBSCRIPTION_ACTIVATION,  # a renewal activates a subscription
    "payment.receipted": K.PAYMENT_RECEIPT,
    "loan.issued": K.LOAN_ISSUED,
    "loan.renewed": K.LOAN_RENEWED,
    "loan.returned": K.LOAN_RETURNED,
    "token.forfeited": K.TOKEN_FORFEITED,
    # no email for: member.registered, token.collected, token.refunded (its REF receipt is a
    # payment.receipted event), loan.lost (the token forfeiture it causes is token.forfeited)
}


# ------------------------------------------------------------------ preparing a record
def _parse(value):
    return date.fromisoformat(value) if isinstance(value, str) and value else None


def _loan_bits(loan_id):
    from circulation.models import Loan

    loan = Loan.objects.select_related("copy__edition").filter(pk=loan_id).first()
    if loan is None:
        return {}
    return {
        "title": loan.copy.edition.title,
        "copy_code": loan.copy.copy_code,
        "due_on": loan.due_on,
    }


def event_context(event, kind):
    p = event.payload
    if kind == K.SUBSCRIPTION_ACTIVATION:
        expires = _parse(p.get("expires_on"))
        return {
            "plan": p.get("plan"),
            "starts_on": _parse(p.get("starts_on")),
            "expires_on": expires,
            "last_valid_day": policy.last_valid_day(expires) if expires else None,
        }
    if kind == K.PAYMENT_RECEIPT:
        return {"receipts": p.get("receipts", [])}
    if kind == K.LOAN_ISSUED:
        return {**_loan_bits(p.get("loan_id")), "due_on": _parse(p.get("due_on"))}
    if kind == K.LOAN_RENEWED:
        return {**_loan_bits(p.get("loan_id")), "new_due_on": _parse(p.get("new_due_on"))}
    if kind == K.LOAN_RETURNED:
        return {**_loan_bits(p.get("loan_id")), "returned_on": _parse(p.get("returned_on"))}
    if kind == K.TOKEN_FORFEITED:
        return {
            "amount_paise": p.get("amount_paise"),
            "effective_date": _parse(p.get("effective_date")),
        }
    return {}


def prepare(kind, member, ctx):
    """Resolve the recipient from the member NOW and build the record's fields."""
    if member.anonymised_at:
        return {
            "status": Notification.Status.SKIPPED,
            "skip_code": Notification.SkipCode.ANONYMISED,
            "skip_reason": "member anonymised; no email address remains",
        }
    email = (member.email or "").strip()
    if not email:
        return {
            "status": Notification.Status.SKIPPED,
            "skip_code": Notification.SkipCode.NO_EMAIL,
            "skip_reason": "no email address",
        }
    subject, body = messages.compose(kind, member=member, library=LibrarySetting.load(), ctx=ctx)
    return {"recipient_email": email, "subject": subject, "body": body}


def _create(*, dedupe_key, source, kind, member, fields, event_id=None, scheduled_for=None, now):
    """Insert one record; False if that dedupe_key (or event) already has one."""
    fields = dict(fields)
    status = fields.setdefault("status", Notification.Status.PREPARED)
    if status == Notification.Status.PREPARED:
        fields["next_attempt_at"] = now
    try:
        with transaction.atomic():
            Notification.objects.create(
                dedupe_key=dedupe_key,
                source=source,
                event_id=event_id,
                scheduled_for=scheduled_for,
                kind=kind,
                member=member,
                created_at=now,
                updated_at=now,
                **fields,
            )
    except IntegrityError:
        if Notification.objects.filter(dedupe_key=dedupe_key).exists():
            return False
        raise
    return True


# ------------------------------------------------------------------ outbox consumption
def unconsumed_events():
    done = Notification.objects.filter(dedupe_key=OuterRef("dedupe_key"))
    return DomainEvent.objects.annotate(consumed=Exists(done)).filter(consumed=False).order_by("id")


def consume_event(event, now=None):
    now = now or timezone.now()
    member = Member.objects.filter(pk=event.member_id).first()
    if member is None:
        log.warning("outbox event %s has no member; left unconsumed", event.event_id)
        return None
    kind = EVENT_KINDS.get(event.event_type)
    if kind is None:
        fields = {
            "status": Notification.Status.SKIPPED,
            "skip_code": Notification.SkipCode.NOT_NOTIFIED,
            "skip_reason": f"no email is defined for {event.event_type}",
        }
        kind = K.NONE
    else:
        fields = prepare(kind, member, event_context(event, kind))
    return _create(
        dedupe_key=event.dedupe_key,
        source=Notification.Source.EVENT,
        kind=kind,
        member=member,
        fields=fields,
        event_id=event.event_id,
        now=now,
    )


def consume_events(now=None, limit=500):
    created = 0
    for event in list(unconsumed_events()[:limit]):
        if consume_event(event, now):
            created += 1
    return created


# ------------------------------------------------------------------ scheduled reminders
def reminder_time_reached(now):
    return timezone.localtime(now).time() >= REMINDER_TIME


def _continued(sub):
    from memberships.models import MemberSubscription

    return MemberSubscription.objects.filter(
        member=sub.member,
        cancelled_at__isnull=True,
        starts_on__lte=sub.expires_on,
        expires_on__gt=sub.expires_on,
    ).exists()


def _valid_on(member, day):
    from memberships.models import MemberSubscription

    return MemberSubscription.objects.filter(
        member=member, cancelled_at__isnull=True, starts_on__lte=day, expires_on__gt=day
    ).exists()


def due_reminders(now, grace_days):
    """Reminders that should exist as of ``now``: (key, kind, member, ctx, scheduled_for).

    A reminder is due on its own date from 09:00 Asia/Kolkata and, if that day was missed, for up
    to BACKFILL_DAYS afterwards - but only while it is still relevant NOW (D-25):
    * expiry reminder (15 days before): only while the subscription has not expired or been
      continued by a renewal;
    * expiry notice (expiry date): only while the member is still not covered by any subscription;
    * overdue (due date, day 3, day 7): only for a loan still out and before its day 8; when
      several milestones were missed only the LATEST one is created (older ones are superseded).
    Pure reads; nothing is written here."""
    from circulation.models import Loan
    from memberships.models import MemberSubscription

    today = timezone.localtime(now).date()
    last = today if reminder_time_reached(now) else today - timedelta(days=1)
    first = today - timedelta(days=BACKFILL_DAYS)
    out = []
    live = MemberSubscription.objects.filter(
        cancelled_at__isnull=True, member__anonymised_at__isnull=True, member__status="active"
    ).select_related("member")
    lead = timedelta(days=EXPIRY_REMINDER_DAYS)
    for sub in live.filter(expires_on__gte=first + lead, expires_on__lte=last + lead):
        if sub.expires_on > today and not _continued(sub):
            ctx = {
                "expires_on": sub.expires_on,
                "last_valid_day": policy.last_valid_day(sub.expires_on),
            }
            out.append(
                (
                    f"reminder:expiry:{sub.pk}",
                    K.EXPIRY_REMINDER,
                    sub.member,
                    ctx,
                    sub.expires_on - lead,
                )
            )
    for sub in live.filter(expires_on__gte=first, expires_on__lte=last):
        if not _valid_on(sub.member, today):
            ctx = {"expires_on": sub.expires_on}
            out.append(
                (
                    f"reminder:expired:{sub.pk}",
                    K.SUBSCRIPTION_EXPIRED,
                    sub.member,
                    ctx,
                    sub.expires_on,
                )
            )
    loans = Loan.objects.filter(status="active", member__anonymised_at__isnull=True).select_related(
        "member", "copy__edition"
    )
    for loan in loans.filter(
        due_on__gte=first - timedelta(days=max(OVERDUE_REMINDER_DAYS)), due_on__lte=last
    ):
        if today >= policy.forfeiture_day(loan.due_on, grace_days):
            continue  # day 8 reached: the forfeiture itself is the notice
        eligible = [
            n for n in OVERDUE_REMINDER_DAYS if first <= loan.due_on + timedelta(days=n) <= last
        ]
        if not eligible:
            continue
        n = max(eligible)
        ctx = {
            "title": loan.copy.edition.title,
            "due_on": loan.due_on,
            "days_overdue": max(0, (today - loan.due_on).days),
            "grace_end": policy.grace_end(loan.due_on, grace_days),
            "forfeiture_day": policy.forfeiture_day(loan.due_on, grace_days),
        }
        out.append(
            (
                f"reminder:overdue:{loan.pk}:{n}",
                K.OVERDUE_REMINDER,
                loan.member,
                ctx,
                loan.due_on + timedelta(days=n),
            )
        )
    return out


def schedule_reminders(now=None):
    """Create the reminders that are due and still relevant (see ``due_reminders``). Each is
    created once (unique key); ``scheduled_for`` keeps the original date, ``created_at`` the real
    processing time. A day missed for longer than BACKFILL_DAYS is not recovered."""
    now = now or timezone.now()
    created = 0
    for key, kind, member, ctx, scheduled_for in due_reminders(
        now, LibrarySetting.load().grace_days
    ):
        if Notification.objects.filter(dedupe_key=key).exists():
            continue
        fields = prepare(kind, member, ctx)
        if _create(
            dedupe_key=key,
            source=Notification.Source.SCHEDULE,
            kind=kind,
            member=member,
            fields=fields,
            scheduled_for=scheduled_for,
            now=now,
        ):
            created += 1
    return created


# ------------------------------------------------------------------ sending
def _scrub(text, recipient=""):
    """Remove SMTP credentials and the recipient's address from an error before it is stored."""
    text = str(text)
    for secret in (settings.EMAIL_HOST_PASSWORD, settings.EMAIL_HOST_USER):
        if secret:
            text = text.replace(secret, "***")
    if recipient:
        text = re.sub(re.escape(recipient), "<recipient>", text, flags=re.IGNORECASE)
    return text[:ERROR_LIMIT]


def claim_due(now, limit=50):
    """Atomically lease due records. Other workers skip locked and already-leased rows."""
    token = uuid.uuid4()
    with transaction.atomic():
        ids = list(
            Notification.objects.select_for_update(skip_locked=True)
            .filter(
                status__in=[Notification.Status.PREPARED, Notification.Status.SMTP_FAILED],
                next_attempt_at__lte=now,
            )
            .filter(Q(claimed_until__isnull=True) | Q(claimed_until__lt=now))
            .order_by("next_attempt_at", "id")
            .values_list("id", flat=True)[:limit]
        )
        Notification.objects.filter(id__in=ids).update(
            claim_token=token, claimed_until=now + LEASE, updated_at=now
        )
    return token, ids


def _skip(note, code, reason, now):
    note.status = Notification.Status.SKIPPED
    note.skip_code, note.skip_reason = code, reason
    note.recipient_email = note.subject = note.body = ""  # personal data is not kept for a skip
    note.last_error = _error_class_only(note.last_error)
    note.next_attempt_at = note.claim_token = note.claimed_until = None
    note.updated_at = now
    note.save()


def attempt(note_id, token, now):
    """Send one claimed notification and record the outcome. Returns the final status or None
    if the claim was lost."""
    note = (
        Notification.objects.select_related("member").filter(pk=note_id, claim_token=token).first()
    )
    if note is None:
        return None
    member = note.member
    if member.anonymised_at or not (member.email or "").strip():
        # re-resolve at send time: anonymisation or removal of the address stops the email
        with transaction.atomic():
            note = (
                Notification.objects.select_for_update()
                .filter(pk=note_id, claim_token=token)
                .first()
            )
            if note is None:
                return None
            if member.anonymised_at:
                _skip(
                    note, Notification.SkipCode.ANONYMISED, "member anonymised before sending", now
                )
            else:
                _skip(note, Notification.SkipCode.NO_EMAIL, "no email address", now)
        return Notification.Status.SKIPPED
    recipient = (member.email or "").strip()
    error = ""
    try:
        sent = EmailMessage(
            subject=note.subject,
            body=note.body,
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=[recipient],
        ).send(fail_silently=False)
        if sent != 1:
            error = "the SMTP backend reported that nothing was sent"
    except Exception as exc:  # noqa: BLE001 - every failure is recorded and retried by the schedule
        error = _scrub(f"{type(exc).__name__}: {exc}", recipient) or type(exc).__name__
    with transaction.atomic():
        note = (
            Notification.objects.select_for_update().filter(pk=note_id, claim_token=token).first()
        )
        if note is None:
            return None
        number = note.attempt_count + 1
        NotificationAttempt.objects.create(
            notification=note, attempt_no=number, attempted_at=now,
            accepted=not error, error=error,
        )  # fmt: skip
        note.attempt_count = number
        note.last_attempt_at = now
        note.recipient_email = recipient
        note.claim_token = note.claimed_until = None
        note.updated_at = now
        if not error:
            note.status = Notification.Status.SMTP_ACCEPTED  # accepted by SMTP; not "delivered"
            note.accepted_at = now
            note.next_attempt_at = None
            note.last_error = ""
        else:
            note.last_error = error
            if number >= MAX_ATTEMPTS:
                note.status = Notification.Status.FAILED
                note.next_attempt_at = None
            else:
                note.status = Notification.Status.SMTP_FAILED
                note.next_attempt_at = now + RETRY_DELAYS[number - 1]
        note.save()
    return note.status


def send_due(now=None, limit=50):
    now = now or timezone.now()
    token, ids = claim_due(now, limit)
    result = {}
    for note_id in ids:
        status = attempt(note_id, token, now)
        result[status] = result.get(status, 0) + 1
    return result


# ------------------------------------------------------------------ anonymisation (D-26)
def _error_class_only(text):
    """A stored error reduced to its type, so nothing personal can survive in it."""
    head = (text or "").split(":", 1)[0].strip()
    return f"{head} (details removed)" if head else ""


def redact_member_notifications(member, now=None, actor=None):
    """Called INSIDE the anonymisation transaction (members.services.ANONYMISATION_HOOKS).

    * not yet accepted by SMTP (prepared, retry-pending): skipped, address, subject and body erased;
    * failed after all retries: marked skipped, address, subject and body erased;
    * SMTP accepted: subject and body erased; the address and the accepted time stay for
      ADDRESS_RETENTION after anonymisation, then ``redact_expired_addresses`` erases the address.
    Errors keep only their type. Returns the number of records changed."""
    now = now or timezone.now()
    erase = {"subject": "", "body": "", "updated_at": now}
    open_rows = Notification.objects.filter(member=member, status__in=[S.PREPARED, S.SMTP_FAILED])
    changed = 0
    for note in open_rows.select_for_update():
        _skip(note, Notification.SkipCode.ANONYMISED, "member anonymised", now)
        changed += 1
    for note in Notification.objects.select_for_update().filter(member=member, status=S.FAILED):
        Notification.objects.filter(pk=note.pk).update(
            status=S.SKIPPED,
            skip_code=Notification.SkipCode.ANONYMISED,
            skip_reason="member anonymised; retries stopped",
            recipient_email="",
            last_error=_error_class_only(note.last_error),
            claim_token=None,
            claimed_until=None,
            **erase,
        )
        changed += 1
    for note in Notification.objects.select_for_update().filter(
        member=member, status=S.SMTP_ACCEPTED
    ):
        if note.subject or note.body:
            Notification.objects.filter(pk=note.pk).update(**erase)
            changed += 1
    from audit.services import record

    record(
        "notifications.redacted",
        actor=actor,
        entity_type="member",
        entity_id=member.membership_no,
        summary={"records": changed},
    )
    return changed


def redact_expired_addresses(now=None):
    """Erase the retained address of accepted emails once ADDRESS_RETENTION has passed since the
    member was anonymised. The status, the accepted time and the attempt log stay."""
    now = now or timezone.now()
    cutoff = now - ADDRESS_RETENTION
    rows = Notification.objects.filter(
        status=S.SMTP_ACCEPTED, member__anonymised_at__lte=cutoff
    ).exclude(recipient_email="")
    ids = list(rows.values_list("id", flat=True))
    if ids:
        Notification.objects.filter(id__in=ids).update(recipient_email="", updated_at=now)
    return len(ids)


def find_unredacted(now=None):
    """Notification records of anonymised members that still hold personal data (D-27b):
    a subject or body, an address on a record that was not SMTP-accepted, or an accepted
    record's address older than the retention window. Used by the deployment preflight."""
    now = now or timezone.now()
    cutoff = now - ADDRESS_RETENTION
    anonymised = Notification.objects.filter(member__anonymised_at__isnull=False)
    return anonymised.filter(
        Q(~Q(subject="") | ~Q(body=""))
        | (~Q(recipient_email="") & ~Q(status=S.SMTP_ACCEPTED))
        | (
            Q(status=S.SMTP_ACCEPTED)
            & ~Q(recipient_email="")
            & Q(member__anonymised_at__lte=cutoff)
        )
    ).order_by("id")


def redact_all_anonymised(now=None, actor=None):
    """Apply the anonymisation redaction to EVERY anonymised member (historic records created
    before the redaction hook existed), then erase addresses whose retention has ended."""
    from members.models import Member

    now = now or timezone.now()
    with transaction.atomic():
        done = 0
        for member in Member.objects.filter(anonymised_at__isnull=False).order_by("id"):
            done += redact_member_notifications(member, now, actor)
        erased = redact_expired_addresses(now)
    return {"changed": done, "addresses_erased": erased}


def run_cycle(now=None):
    """Consume new events, create the reminders that are due, send what is due, and erase
    addresses whose retention has ended."""
    now = now or timezone.now()
    consumed = consume_events(now)
    reminders = schedule_reminders(now)
    sent = send_due(now)
    redacted = redact_expired_addresses(now)
    return {"consumed": consumed, "reminders": reminders, "sent": sent, "redacted": redacted}


def next_retry_label(note):
    if note.status == Notification.Status.SMTP_FAILED and note.next_attempt_at:
        return timezone.localtime(note.next_attempt_at).strftime("%d %b %Y %H:%M")
    return ""
