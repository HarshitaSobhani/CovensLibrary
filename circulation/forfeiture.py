"""Overdue token forfeiture ('day 8'), applied automatically and exactly once per loan.

Rules (decisions B9, B10, N6, N11/Q5): grace is ``grace_days`` calendar days after the due date
(setting, live - no per-loan snapshot); on the day after it ('day 8') the full token is forfeited
automatically, with no owner confirmation, even when the book is returned on day 8. If the
application was offline the forfeiture is posted later with the ORIGINAL effective date (day 8)
and the real processing timestamp. A loan whose forfeiture awaits (or was refused in) an owner
decision after a grace change is never forfeited automatically. The forfeiture is attributed to
the restricted system actor (decision D-15), never to a user.

Functions here assume the caller already holds the member row lock.
"""

from django.db import transaction
from django.utils import timezone

from audit.services import record
from core.models import LibrarySetting
from finance import corrections, tokens
from finance.models import SYSTEM_OVERDUE_FORFEITURE
from members.models import Member
from memberships import policy

from .models import Loan, PendingForfeiture


def reference_day(loan, today):
    """The day a loan is judged on: its return/loss date once closed, otherwise today."""
    return loan.returned_on or today


def forfeiture_day(loan, grace_days):
    return policy.forfeiture_day(loan.due_on, grace_days)


def is_eligible(loan, today, grace_days):
    """True when the loan reached 'day 8' (a return ON day 8 or later still counts)."""
    return reference_day(loan, today) >= forfeiture_day(loan, grace_days)


def awaiting_or_refused_decision(loan):
    return loan.pending_forfeitures.filter(
        status__in=[PendingForfeiture.Status.PENDING, PendingForfeiture.Status.DECLINED]
    ).exists()


def link_forfeiture(loan, entry):
    """Attach the ledger entry to the loan once (a database trigger forbids any later change)."""
    if loan.forfeiture_entry_id is None:
        Loan.objects.filter(pk=loan.pk, forfeiture_entry__isnull=True).update(
            forfeiture_entry=entry
        )
        loan.forfeiture_entry = entry


def apply_automatic(loan, today, grace_days):
    """Post the automatic forfeiture if (and only if) it is due; returns the entry or None."""
    if loan.forfeiture_entry_id or awaiting_or_refused_decision(loan):
        return None
    if not is_eligible(loan, today, grace_days):
        return None
    member = loan.member
    if tokens.token_state(member)[1] != tokens.HELD:
        return None  # already forfeited, refunded or missing: nothing to forfeit
    effective = forfeiture_day(loan, grace_days)
    posted = corrections.forfeit_token(
        member=member, trigger="overdue_auto", effective_date=effective, today=today
    )
    txn = posted.transactions[0]
    entry = txn.token_entries.get()
    link_forfeiture(loan, entry)
    record(
        "loan.token_forfeited",
        actor=None,
        entity_type="loan",
        entity_id=loan.pk,
        summary={
            "system": SYSTEM_OVERDUE_FORFEITURE,
            "trigger": "overdue_auto",
            "member": member.membership_no,
            "due_on": loan.due_on.isoformat(),
            "grace_days": grace_days,
            "effective_date": effective.isoformat(),
            "processed_at": txn.processed_at.isoformat(),
            "processed_late": effective < today,
        },
    )
    return entry


def process_overdue_forfeitures(today=None):
    """The daily (and launch catch-up) job: idempotent, one transaction per loan.

    Returns counts: ``forfeited`` (posted now), ``skipped_decision`` (a grace-change decision is
    pending or was declined), ``skipped_token`` (no held token to forfeit).
    """
    today = today or timezone.localdate()
    summary = {"checked": 0, "forfeited": 0, "skipped_decision": 0, "skipped_token": 0, "late": 0}
    grace = LibrarySetting.load().grace_days
    cutoff = policy.latest_due_date_for_forfeiture(today, grace)
    candidates = list(
        Loan.objects.filter(
            status=Loan.Status.ACTIVE, forfeiture_entry__isnull=True, due_on__lte=cutoff
        )
        .order_by("due_on", "id")
        .values_list("pk", "member_id")
    )
    for loan_pk, member_pk in candidates:
        summary["checked"] += 1
        with transaction.atomic():
            # same order as every other operation: member first
            Member.objects.select_for_update().get(pk=member_pk)
            current_grace = LibrarySetting.objects.select_for_update().get(pk=1).grace_days
            loan = Loan.objects.select_for_update().select_related("member").get(pk=loan_pk)
            if loan.status != Loan.Status.ACTIVE or loan.forfeiture_entry_id:
                continue  # closed or handled meanwhile
            if awaiting_or_refused_decision(loan):
                summary["skipped_decision"] += 1
                continue
            entry = apply_automatic(loan, today, current_grace)
            if entry is None:
                if is_eligible(loan, today, current_grace):
                    summary["skipped_token"] += 1
                continue
            summary["forfeited"] += 1
            if entry.effective_date < today:
                summary["late"] += 1
    return summary
