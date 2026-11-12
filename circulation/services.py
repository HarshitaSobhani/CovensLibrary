"""Loan workflows. Every operation is one transaction that locks the member row first, then the
copy row (the same order everywhere), so concurrent operations serialise without deadlock."""

from django.db import IntegrityError, transaction
from django.utils import timezone

from audit.services import record
from catalogue.models import BookCopy, CopyEvent
from catalogue.services import log_copy_event
from core import outbox
from core.errors import RuleViolation
from core.models import LibrarySetting
from core.money import clean_reason
from finance import corrections, tokens
from members.models import Member
from memberships import policy
from memberships import services as ms

from . import forfeiture
from .models import Loan, LoanRenewal


def lock_member(pk):
    return Member.objects.select_for_update().get(pk=pk)


def lock_copy(pk):
    return BookCopy.objects.select_for_update().get(pk=pk)


def member_has_active_loan(member):
    """Registered with finance: a token cannot be refunded while a book is outstanding."""
    return Loan.objects.filter(member=member, status=Loan.Status.ACTIVE).exists()


def active_loan(member):
    return Loan.objects.filter(member=member, status=Loan.Status.ACTIVE).first()


def check_event_date(day, today, what):
    if day > today:
        raise RuleViolation("future_date", f"The {what} date cannot be in the future.")


def backdating(day, today, reason, confirmed, what):
    """B19/N12: any past loan date needs a reason and an extra owner confirmation, because a
    loan date can change a due date and therefore a token forfeiture."""
    if day >= today:
        return False, ""
    reason = clean_reason(reason, f"The reason for a past {what} date")
    if not reason:
        raise RuleViolation(
            "backdate_reason_required", f"A reason is required for a past {what} date."
        )
    if not confirmed:
        raise RuleViolation(
            "backdate_confirmation_required",
            f"Confirm that this {what} carries a past date (it can change due dates and "
            "token forfeiture).",
        )
    return True, reason


@transaction.atomic
def issue_loan(
    *,
    actor,
    member,
    copy,
    due_on=None,
    issued_on=None,
    backdate_reason="",
    backdate_confirmed=False,
    notes="",
    today=None,
):
    """Issue one Good + Available copy to one eligible member.

    Enforced here (and, for the one-active-loan rules, by partial unique indexes): member
    status, valid subscription on the issue date, a held token, no book outstanding, copy Good +
    Available, the final-15-day block and the due-date cap (independent rules). The due date is
    30 days after issue unless the owner explicitly chooses an EARLIER one; it is never silently
    shortened or extended.
    """
    today = today or timezone.localdate()
    issued_on = issued_on or today
    check_event_date(issued_on, today, "issue")
    notes = clean_reason(notes, "The note")
    backdated, reason = backdating(issued_on, today, backdate_reason, backdate_confirmed, "issue")
    setting = LibrarySetting.load()

    member = lock_member(member.pk)
    copy = lock_copy(copy.pk)
    blockers = ms.borrowing_blockers(member, issued_on)
    if blockers:
        raise RuleViolation(*blockers[0])
    if Loan.objects.filter(member=member, status=Loan.Status.ACTIVE).exists():
        raise RuleViolation(
            "loan_outstanding", "The member already has a book out; it must be returned first."
        )
    if not copy.is_issuable:
        raise RuleViolation(
            "copy_unavailable",
            f"Copy {copy.copy_code} cannot be issued ({copy.get_condition_display()}, "
            f"{copy.get_status_display()}): only Good and Available copies are lent.",
        )
    subscription = ms.current_subscription(member, issued_on)
    default_due = policy.default_due_date(issued_on, setting.loan_days)
    due = due_on or default_due
    if due < issued_on or due > default_due:
        raise RuleViolation(
            "due_date_invalid",
            f"The due date must be between the issue date and {default_due:%d %b %Y} "
            f"({setting.loan_days} days).",
        )
    policy.check_issue(
        ms.coverage_period(member, issued_on), issued_on, due, setting.issue_block_days
    )

    try:
        with transaction.atomic():
            loan = Loan.objects.create(
                member=member,
                copy=copy,
                subscription=subscription,
                issued_on=issued_on,
                due_on=due,
                issue_condition=copy.condition,
                backdated=backdated,
                backdate_reason=reason,
                backdate_confirmed=backdated,
                notes=notes,
                issued_by=actor,
            )
    except IntegrityError:  # a concurrent issue won the race (DB partial unique indexes)
        raise RuleViolation(
            "loan_conflict", "This member or copy was just issued a loan elsewhere."
        ) from None
    copy.status = BookCopy.Status.BORROWED
    copy.save(update_fields=["status"])
    log_copy_event(
        copy,
        CopyEvent.Type.ISSUED,
        BookCopy.Status.AVAILABLE,
        BookCopy.Status.BORROWED,
        f"Loan {loan.pk}",
        actor,
        issued_on,
        loan,
    )
    record(
        "loan.issued",
        actor=actor,
        entity_type="loan",
        entity_id=loan.pk,
        summary={
            "member": member.membership_no,
            "copy": copy.copy_code,
            "issued_on": issued_on.isoformat(),
            "due_on": due.isoformat(),
            "processed_at": loan.issued_processed_at.isoformat(),
            "backdated": backdated,
            **({"backdate_reason": reason, "backdate_owner_confirmed": True} if backdated else {}),
        },
    )
    outbox.emit(
        outbox.LOAN_ISSUED,
        dedupe_key=f"loan:{loan.pk}:issued",
        member_id=member.pk,
        event_date=issued_on,
        occurred_at=loan.issued_processed_at,
        payload={"loan_id": loan.pk, "copy_code": copy.copy_code, "due_on": due.isoformat()},
    )
    return loan


OUTCOMES = {
    # outcome: (copy condition, copy status)
    "good": (BookCopy.Condition.GOOD, BookCopy.Status.AVAILABLE),
    "poor": (BookCopy.Condition.POOR, BookCopy.Status.WITHDRAWN),  # Poor is always Withdrawn (Q6)
    "damaged": (BookCopy.Condition.DAMAGED, BookCopy.Status.DAMAGED),
    "lost": (BookCopy.Condition.LOST, BookCopy.Status.LOST),
}


def _decision(outcome, owner_confirmed, reason):
    """Poor / damaged / lost need the owner's explicit confirmation and a mandatory reason."""
    if outcome == "good":
        return ""
    if not owner_confirmed:
        raise RuleViolation(
            "confirmation_required",
            "Confirm the outcome: the full token is forfeited and the copy leaves circulation.",
        )
    return clean_reason(reason, "A reason for this outcome", required=True)


def _close_loan(
    *,
    actor,
    loan,
    outcome,
    closed_on,
    today,
    owner_confirmed,
    reason,
    backdate_reason,
    backdate_confirmed,
    notes,
):
    """Shared by returns and losses. The caller holds the member, loan and copy locks."""
    member, copy = loan.member, loan.copy
    setting = LibrarySetting.load()
    if loan.status != Loan.Status.ACTIVE:
        raise RuleViolation("loan_closed", "This loan is already closed.")
    if closed_on < loan.issued_on:
        raise RuleViolation(
            "return_before_issue", "A book cannot be returned before it was issued."
        )
    decision = _decision(outcome, owner_confirmed, reason)
    backdated, why = backdating(closed_on, today, backdate_reason, backdate_confirmed, "return")
    condition, status = OUTCOMES[outcome]

    loan.status = Loan.Status.LOST if outcome == "lost" else Loan.Status.RETURNED
    loan.returned_on = closed_on
    loan.returned_processed_at = timezone.now()
    loan.return_condition = outcome
    loan.returned_by = actor
    loan.return_backdated = backdated
    loan.return_backdate_reason = why
    extra = " ".join(x for x in (notes, f"Decision: {decision}" if decision else "") if x)
    if extra:
        loan.notes = (loan.notes + "\n" + extra).strip()
    loan.save()
    old_condition = copy.condition
    copy.status, copy.condition = status, condition
    copy.save(update_fields=["status", "condition"])
    if outcome == "lost":
        log_copy_event(
            copy,
            CopyEvent.Type.STATUS_CHANGED,
            BookCopy.Status.BORROWED,
            status,
            f"Loan {loan.pk}: lost",
            actor,
            closed_on,
            loan,
        )
    else:
        log_copy_event(
            copy,
            CopyEvent.Type.RETURNED,
            BookCopy.Status.BORROWED,
            status,
            f"Loan {loan.pk}",
            actor,
            closed_on,
            loan,
        )
    if condition != old_condition:
        log_copy_event(
            copy,
            CopyEvent.Type.CONDITION_CHANGED,
            old_condition,
            condition,
            f"Loan {loan.pk}: {decision}"[:200],
            actor,
            closed_on,
            loan,
        )

    # 1. the automatic day-8 forfeiture comes first: it is a fact of time (a return ON day 8 or
    #    later still counts), attributed to the system actor, no confirmation needed
    automatic = forfeiture.apply_automatic(loan, today, setting.grace_days)
    # 2. then the owner's confirmed decision, only if a held token is still there to forfeit
    manual = None
    if outcome != "good" and tokens.token_state(member)[1] == tokens.HELD:
        posted = corrections.forfeit_token(
            actor=actor,
            member=member,
            trigger=outcome,
            effective_date=closed_on,
            owner_confirmed=True,
            reason=decision,
            today=today,
        )
        manual = posted.transactions[0].token_entries.get()
        forfeiture.link_forfeiture(loan, manual)
    record(
        "loan.lost" if outcome == "lost" else "loan.returned",
        actor=actor,
        entity_type="loan",
        entity_id=loan.pk,
        summary={
            "member": member.membership_no,
            "copy": copy.copy_code,
            "returned_on": closed_on.isoformat(),
            "processed_at": loan.returned_processed_at.isoformat(),
            "outcome": outcome,
            "owner_confirmed": bool(decision),
            "reason": decision,
            "copy_state": [condition, status],
            "automatic_forfeiture": automatic is not None,
            "confirmed_forfeiture": manual is not None,
            "backdated": backdated,
            **({"backdate_reason": why, "backdate_owner_confirmed": True} if backdated else {}),
        },
    )
    outbox.emit(
        outbox.LOAN_LOST if outcome == "lost" else outbox.LOAN_RETURNED,
        dedupe_key=f"loan:{loan.pk}:{'lost' if outcome == 'lost' else 'returned'}",
        member_id=member.pk,
        event_date=closed_on,
        occurred_at=loan.returned_processed_at,
        payload={
            "loan_id": loan.pk,
            "copy_code": copy.copy_code,
            "outcome": outcome,
            "returned_on": closed_on.isoformat(),
        },
    )
    return loan


def _locked(loan_pk):
    member = lock_member(Loan.objects.only("member_id").get(pk=loan_pk).member_id)
    loan = Loan.objects.select_for_update().select_related("member", "copy").get(pk=loan_pk)
    loan.member = member
    loan.copy = lock_copy(loan.copy_id)
    return loan


@transaction.atomic
def return_loan(
    *,
    actor,
    loan,
    returned_on=None,
    condition="good",
    owner_confirmed=False,
    reason="",
    backdate_reason="",
    backdate_confirmed=False,
    notes="",
    today=None,
):
    """Record a return. Good puts the copy back on the shelf. Poor (condition Poor + status
    Withdrawn) and Damaged need the owner's confirmation and a mandatory reason, forfeit the full
    token and take the copy out of circulation. A Lost book uses ``mark_lost``."""
    today = today or timezone.localdate()
    returned_on = returned_on or today
    check_event_date(returned_on, today, "return")
    notes = clean_reason(notes, "The note")
    if condition not in ("good", "poor", "damaged"):
        raise RuleViolation(
            "outcome_invalid", "Choose good, poor or damaged (use 'lost' for a lost book)."
        )
    loan = _locked(loan.pk)
    return _close_loan(
        actor=actor,
        loan=loan,
        outcome=condition,
        closed_on=returned_on,
        today=today,
        owner_confirmed=owner_confirmed,
        reason=reason,
        backdate_reason=backdate_reason,
        backdate_confirmed=backdate_confirmed,
        notes=notes,
    )


@transaction.atomic
def mark_lost(
    *,
    actor,
    loan,
    lost_on=None,
    owner_confirmed=False,
    reason="",
    backdate_reason="",
    backdate_confirmed=False,
    notes="",
    today=None,
):
    """The owner marks a book Lost: the loan closes as Lost, the copy becomes Lost, the full token
    is forfeited (owner-confirmed, mandatory reason). No replacement charge exists."""
    today = today or timezone.localdate()
    lost_on = lost_on or today
    check_event_date(lost_on, today, "loss")
    notes = clean_reason(notes, "The note")
    loan = _locked(loan.pk)
    return _close_loan(
        actor=actor,
        loan=loan,
        outcome="lost",
        closed_on=lost_on,
        today=today,
        owner_confirmed=owner_confirmed,
        reason=reason,
        backdate_reason=backdate_reason,
        backdate_confirmed=backdate_confirmed,
        notes=notes,
    )


@transaction.atomic
def renew_loan(*, actor, loan, notes="", today=None):
    """Renew: +30 days on the EXISTING due date, unlimited, never after the due date, never
    beyond the member's last valid subscription day (the owner renews the membership first)."""
    today = today or timezone.localdate()
    notes = clean_reason(notes, "The note")
    loan = _locked(loan.pk)
    member = loan.member
    if loan.status != Loan.Status.ACTIVE:
        raise RuleViolation("loan_closed", "Only a loan that is still out can be renewed.")
    setting = LibrarySetting.load()
    new_due = policy.renewed_due_date(loan.due_on, setting.loan_days)
    try:
        policy.check_loan_renewal(ms.coverage_period(member, today), today, loan.due_on, new_due)
    except RuleViolation as exc:
        if exc.code == "due_after_last_valid_day":
            raise RuleViolation(
                exc.code,
                exc.message
                + " Renew the membership first; the due date is never extended silently.",
            ) from None
        raise
    old_due = loan.due_on
    loan.due_on = new_due
    loan.save(update_fields=["due_on"])
    renewal = LoanRenewal.objects.create(
        loan=loan,
        renewed_on=today,
        old_due_on=old_due,
        new_due_on=new_due,
        notes=notes,
        renewed_by=actor,
    )
    record(
        "loan.renewed",
        actor=actor,
        entity_type="loan",
        entity_id=loan.pk,
        summary={
            "member": member.membership_no,
            "renewed_on": today.isoformat(),
            "old_due_on": old_due.isoformat(),
            "new_due_on": new_due.isoformat(),
            "processed_at": renewal.renewed_at.isoformat(),
        },
    )
    outbox.emit(
        outbox.LOAN_RENEWED,
        dedupe_key=f"loan:{loan.pk}:renewed:{new_due.isoformat()}",
        member_id=member.pk,
        event_date=today,
        occurred_at=renewal.renewed_at,
        payload={
            "loan_id": loan.pk,
            "old_due_on": old_due.isoformat(),
            "new_due_on": new_due.isoformat(),
        },
    )
    return renewal
