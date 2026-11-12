"""Changing the grace period (decisions B17, N11/Q5) and the owner's decision on every forfeiture
it newly makes possible.

The grace period applies to existing loans (no per-loan snapshot). When a change makes an
unreturned loan eligible for forfeiture that was not eligible before, nothing is posted
automatically: a ``PendingForfeiture`` is created and the owner must confirm it (full record) or
decline it (mandatory reason; never asked again for this event; the automatic job skips the loan).
Loans that are already closed are not re-evaluated (OPEN_QUESTIONS D-17).
"""

from django.db import transaction
from django.utils import timezone

from audit.services import record
from core.errors import RuleViolation
from core.models import LibrarySetting, SettingChangeEvent
from core.money import clean_reason
from finance import corrections
from members.models import Member
from memberships import policy

from . import forfeiture
from .models import Loan, PendingForfeiture

MAX_GRACE_DAYS = 365


@transaction.atomic
def change_grace_days(*, actor, new_days, today=None):
    today = today or timezone.localdate()
    if (
        isinstance(new_days, bool)
        or not isinstance(new_days, int)
        or not 0 <= new_days <= MAX_GRACE_DAYS
    ):
        raise RuleViolation(
            "grace_invalid",
            f"The grace period must be a whole number of days from 0 to {MAX_GRACE_DAYS}.",
        )
    LibrarySetting.load()
    setting = LibrarySetting.objects.select_for_update().get(pk=1)
    old_days = setting.grace_days
    if new_days == old_days:
        raise RuleViolation("grace_unchanged", "The grace period is already that many days.")

    affected = []
    for loan in (
        Loan.objects.select_related("member")
        .filter(status=Loan.Status.ACTIVE, forfeiture_entry__isnull=True, due_on__lt=today)
        .order_by("due_on", "id")
    ):
        was_eligible = today >= policy.forfeiture_day(loan.due_on, old_days)
        now_eligible = today >= policy.forfeiture_day(loan.due_on, new_days)
        if now_eligible and not was_eligible and not forfeiture.awaiting_or_refused_decision(loan):
            affected.append(loan)

    event = SettingChangeEvent.objects.create(
        setting_name="grace_days",
        old_value=str(old_days),
        new_value=str(new_days),
        actor=actor,
        affected_loans=[
            {
                "loan_id": loan.pk,
                "member": loan.member.membership_no,
                "due_on": loan.due_on.isoformat(),
                "forfeiture_day": policy.forfeiture_day(loan.due_on, new_days).isoformat(),
            }
            for loan in affected
        ],
    )
    for loan in affected:
        PendingForfeiture.objects.create(loan=loan, setting_change=event)
    setting.grace_days = new_days
    setting.save(update_fields=["grace_days"])
    record(
        "setting.grace_days_changed",
        actor=actor,
        entity_type="setting",
        entity_id="grace_days",
        summary={
            "old": old_days,
            "new": new_days,
            "newly_eligible_loans": len(affected),
            "changed_at": event.changed_at.isoformat(),
        },
    )
    return event


def _lock_pending(pending_pk):
    member_pk = PendingForfeiture.objects.select_related("loan").get(pk=pending_pk).loan.member_id
    Member.objects.select_for_update().get(pk=member_pk)
    LibrarySetting.objects.select_for_update().get(pk=1)
    return (
        PendingForfeiture.objects.select_for_update()
        .select_related("loan__member")
        .get(pk=pending_pk)
    )


@transaction.atomic
def confirm_pending(*, actor, pending, reason, today=None):
    """The owner confirms: the full token is forfeited now, effective on the loan's forfeiture
    day under the current grace period, with the mandatory reason and the owner as the actor."""
    today = today or timezone.localdate()
    reason = clean_reason(reason, "A reason for confirming this forfeiture", required=True)
    pending = _lock_pending(pending.pk)
    if pending.status != PendingForfeiture.Status.PENDING:
        raise RuleViolation("already_resolved", "This decision was already made.")
    loan = pending.loan
    grace = LibrarySetting.load().grace_days
    if not forfeiture.is_eligible(loan, today, grace):
        raise RuleViolation(
            "not_eligible",
            "The loan is no longer eligible under the current grace period; decline it instead.",
        )
    if loan.forfeiture_entry_id:
        raise RuleViolation("already_forfeited", "This loan's token was already forfeited.")
    posted = corrections.forfeit_token(
        actor=actor,
        member=loan.member,
        trigger="grace_change_confirmed",
        effective_date=forfeiture.forfeiture_day(loan, grace),
        owner_confirmed=True,
        reason=reason,
        today=today,
    )
    txn = posted.transactions[0]
    entry = txn.token_entries.get()
    forfeiture.link_forfeiture(loan, entry)
    pending.status = PendingForfeiture.Status.CONFIRMED
    pending.confirm_reason = reason
    pending.resolved_at = timezone.now()
    pending.resolved_by = actor
    pending.resulting_entry = entry
    pending.save()
    record(
        "forfeiture.grace_change_confirmed",
        actor=actor,
        entity_type="loan",
        entity_id=loan.pk,
        summary={
            "reason": reason,
            "setting_change": pending.setting_change_id,
            "effective_date": txn.effective_date.isoformat(),
            "processed_at": txn.processed_at.isoformat(),
            "transaction": str(txn.internal_txn_id),
        },
    )
    return pending


@transaction.atomic
def decline_pending(*, actor, pending, reason):
    """The owner declines: nothing is posted, the reason is mandatory and audited, the owner is
    not asked again for this event and the daily job skips the loan."""
    reason = clean_reason(reason, "A reason for declining this forfeiture", required=True)
    pending = _lock_pending(pending.pk)
    if pending.status != PendingForfeiture.Status.PENDING:
        raise RuleViolation("already_resolved", "This decision was already made.")
    pending.status = PendingForfeiture.Status.DECLINED
    pending.decline_reason = reason
    pending.resolved_at = timezone.now()
    pending.resolved_by = actor
    pending.save()
    record(
        "forfeiture.grace_change_declined",
        actor=actor,
        entity_type="loan",
        entity_id=pending.loan_id,
        summary={
            "reason": reason,
            "setting_change": pending.setting_change_id,
            "processed_at": pending.resolved_at.isoformat(),
        },
    )
    return pending


def pending_decisions():
    return PendingForfeiture.objects.filter(status=PendingForfeiture.Status.PENDING).select_related(
        "loan__member", "loan__copy", "setting_change"
    )
