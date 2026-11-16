"""Registration, renewal, fresh-token payment and cancellation.

Each operation is one database transaction that locks the member row first (then the receipt
sequence rows), so a failure anywhere leaves NO partial membership, subscription, payment or
token record, and concurrent double submissions cannot create duplicates.
"""

import uuid
from dataclasses import dataclass

from django.apps import apps
from django.db import transaction
from django.utils import timezone

from audit.services import record
from core import outbox
from core.errors import RuleViolation
from core.money import clean_reason
from finance import services as fs
from finance import tokens
from finance.services import Item
from members import services as member_services
from members.models import Member

from . import policy
from .models import MemberSubscription, SubscriptionPlan

K = fs.K
FLOW = fs.FLOW


@dataclass
class Result:
    member: Member
    subscription: MemberSubscription | None
    document: object
    transactions: list


def lock_member(pk):
    return Member.objects.select_for_update().get(pk=pk)


def _plan(code):
    plan = SubscriptionPlan.objects.filter(code=code, is_active=True).first()
    if plan is None:
        raise RuleViolation("plan_unknown", "Choose one of the available subscription plans.")
    return plan


def _live_subscriptions(member):
    return list(member.subscriptions.filter(cancelled_at__isnull=True))


def periods(member):
    return [s.period for s in _live_subscriptions(member)]


def current_subscription(member, today):
    for sub in _live_subscriptions(member):
        if policy.state(sub.period, today) == policy.ACTIVE:
            return sub
    return None


def coverage_period(member, day):
    """Contiguous membership coverage (current + adjacent renewals) used by the loan rules."""
    return policy.coverage_period(periods(member), day)


def scheduled_subscription(member, today):
    for sub in _live_subscriptions(member):
        if policy.state(sub.period, today) == policy.SCHEDULED:
            return sub
    return None


def renewal_blockers(member, today):
    """Reasons the member cannot renew right now (empty list = may renew)."""
    out = []
    if member.anonymised_at:
        out.append(("anonymised", "An anonymised member cannot be renewed."))
    state = tokens.token_state(member)[1]
    if state == tokens.FORFEITED:
        out.append(
            (
                "token_forfeited",
                "The token was forfeited. Collect a fresh token first; the current "
                "subscription stays valid until it expires.",
            )
        )
    elif state != tokens.HELD:
        out.append(("token_missing", "The member has no token on hold. Collect a token first."))
    return out


def borrowing_blockers(member, today):
    """Reasons a NEW loan cannot be issued to this member (used by Phase 4)."""
    out = []
    if member.anonymised_at:
        out.append(("anonymised", "An anonymised member cannot borrow."))
    if member.status != Member.Status.ACTIVE:
        out.append(("member_not_active", f"The member is {member.get_status_display().lower()}."))
    if member.needs_new_subscription:
        out.append(("new_subscription_required", "A new subscription is required."))
    if current_subscription(member, today) is None:
        out.append(("no_valid_subscription", "The member has no valid subscription."))
    state = tokens.token_state(member)[1]
    if state == tokens.FORFEITED:
        out.append(
            (
                "token_forfeited",
                "Borrowing is suspended: the token was forfeited. Collect a fresh ₹500.",
            )
        )
    elif state != tokens.HELD:
        out.append(("token_missing", "The member has no token on hold."))
    return out


def _create_subscription(member, plan, start, cycle, txn, actor):
    expiry = policy.expires_on(start, plan.duration_months)
    sub = MemberSubscription.objects.create(
        member=member,
        token_cycle=cycle,
        plan=plan,
        plan_name_snapshot=plan.name,
        duration_months_snapshot=plan.duration_months,
        price_paise_snapshot=plan.price_paise,
        starts_on=start,
        expires_on=expiry,
        payment_txn=txn,
    )
    return sub


def _audit_subscription(action, actor, sub, txn, today, reason="", confirmed=False):
    start_backdated = sub.starts_on < today
    record(
        action,
        actor=actor,
        entity_type="subscription",
        entity_id=sub.pk,
        summary={
            "member": sub.member.membership_no,
            "plan": sub.plan_name_snapshot,
            "starts_on": sub.starts_on.isoformat(),
            "expires_on": sub.expires_on.isoformat(),
            "receipt": txn.receipt_no,
            **fs.audit_dates(
                txn,
                event_date=sub.starts_on,
                start_backdated=start_backdated,
                start_reason=clean_reason(reason) if start_backdated else "",
                start_confirmed=confirmed,
            ),
        },
    )


def _emit_subscription_event(event_type, member, sub, txn, today):
    outbox.emit(
        event_type,
        dedupe_key=f"subscription:{sub.pk}:{event_type.split('.')[1]}",
        member_id=member.pk,
        event_date=today,
        payload={
            "subscription_id": sub.pk,
            "plan": sub.plan_name_snapshot,
            "starts_on": sub.starts_on.isoformat(),
            "expires_on": sub.expires_on.isoformat(),
            "receipt_no": txn.receipt_no,
        },
    )


def _emit_token_collected(member, cycle, txn):
    outbox.emit(
        outbox.TOKEN_COLLECTED,
        dedupe_key=f"token:{txn.internal_txn_id}:collected",
        member_id=member.pk,
        event_date=txn.effective_date,
        payload={
            "cycle_no": cycle.cycle_no,
            "receipt_no": txn.receipt_no,
            "amount_paise": txn.amount_paise,
        },
    )


def _check_start_backdating(start, today, reason, confirmed):
    """A subscription starting in the past is a backdated operation (B19)."""
    if start < today:
        fs.check_backdating(start, [], today, reason, confirmed)


@transaction.atomic
def register_member(
    *,
    actor,
    member_fields,
    plan_code,
    subscription_entries,
    token_entries,
    activation_date=None,
    payment_date=None,
    backdate_reason="",
    backdate_confirmed=False,
    request_key=None,
    today=None,
):
    """Guided registration: details -> plan -> payments -> token -> receipts -> activation.

    Activation happens only because the full subscription fee AND the token were both paid in
    this same transaction; any failure rolls back the member, receipts and numbers.
    """
    today = today or timezone.localdate()
    plan = _plan(plan_code)
    start = activation_date or today
    pay_date = payment_date or today
    _check_start_backdating(start, today, backdate_reason, backdate_confirmed)
    member = member_services.create_member(actor=actor, **member_fields)
    member = lock_member(member.pk)
    cycle = tokens.start_cycle(member, today)
    sub_item = Item(
        K.SUBSCRIPTION_PAYMENT,
        FLOW.IN,
        plan.price_paise,
        pay_date,
        subscription_entries,
        label=plan.name,
        detail={"plan": plan.name, "starts_on": start.isoformat()},
        request_key=fs.derived_key(request_key, "subscription"),
    )
    tok_item = Item(
        K.TOKEN_COLLECTION,
        FLOW.IN,
        cycle.amount_snapshot_paise,
        pay_date,
        token_entries,
        label="Refundable security token",
        detail={"cycle": cycle.cycle_no},
        request_key=fs.derived_key(request_key, "token"),
    )
    posted = fs.post_items(
        member=member,
        items=[sub_item, tok_item],
        actor=actor,
        today=today,
        document_kind="registration",
        backdate_reason=backdate_reason,
        backdate_confirmed=backdate_confirmed,
    )
    sub_txn, tok_txn = posted.transactions
    tokens.record_entry(cycle, tokens.E_.COLLECTION, tok_txn.amount_paise, tok_txn, pay_date)
    sub = _create_subscription(member, plan, start, cycle, sub_txn, actor)
    _audit_subscription(
        "membership.registered", actor, sub, sub_txn, today, backdate_reason, backdate_confirmed
    )
    record(
        "token.collected",
        actor=actor,
        entity_type="member",
        entity_id=member.pk,
        summary={
            "cycle": cycle.cycle_no,
            "receipt": tok_txn.receipt_no,
            **fs.audit_dates(tok_txn),
        },
    )
    outbox.emit(
        outbox.MEMBER_REGISTERED,
        dedupe_key=f"member:{member.pk}:registered",
        member_id=member.pk,
        event_date=today,
        payload={"membership_no": member.membership_no},
    )
    _emit_subscription_event(outbox.SUBSCRIPTION_ACTIVATED, member, sub, sub_txn, today)
    _emit_token_collected(member, cycle, tok_txn)
    return Result(member, sub, posted.document, posted.transactions)


@transaction.atomic
def renew_subscription(
    *,
    actor,
    member,
    plan_code,
    entries,
    activation_date=None,
    payment_date=None,
    backdate_reason="",
    backdate_confirmed=False,
    request_key=None,
    today=None,
):
    """Renew: early renewal starts on the current expiry; late renewal on the entered date."""
    today = today or timezone.localdate()
    member = lock_member(member.pk)
    blockers = renewal_blockers(member, today)
    if blockers:
        raise RuleViolation(*blockers[0])
    plan = _plan(plan_code)
    start = policy.next_start(periods(member), today, activation_date)
    _check_start_backdating(start, today, backdate_reason, backdate_confirmed)
    cycle = tokens.latest_cycle(member)
    item = Item(
        K.SUBSCRIPTION_PAYMENT,
        FLOW.IN,
        plan.price_paise,
        payment_date or today,
        entries,
        label=plan.name,
        detail={"plan": plan.name, "starts_on": start.isoformat()},
        request_key=fs.derived_key(request_key, "subscription"),
    )
    posted = fs.post_items(
        member=member,
        items=[item],
        actor=actor,
        today=today,
        document_kind="subscription",
        backdate_reason=backdate_reason,
        backdate_confirmed=backdate_confirmed,
    )
    txn = posted.transactions[0]
    sub = _create_subscription(member, plan, start, cycle, txn, actor)
    # The held token carries over an ordinary renewal: recorded as a zero-value ledger entry.
    tokens.record_entry(cycle, tokens.E_.CARRY_FORWARD, 0, txn, txn.effective_date)
    if member.needs_new_subscription:
        member.needs_new_subscription = False
        member.save(update_fields=["needs_new_subscription", "updated_at"])
    _audit_subscription(
        "subscription.renewed", actor, sub, txn, today, backdate_reason, backdate_confirmed
    )
    _emit_subscription_event(outbox.SUBSCRIPTION_RENEWED, member, sub, txn, today)
    return Result(member, sub, posted.document, posted.transactions)


@transaction.atomic
def pay_fresh_token(
    *,
    actor,
    member,
    entries,
    payment_date=None,
    backdate_reason="",
    backdate_confirmed=False,
    request_key=None,
    today=None,
):
    """Collect a fresh ₹500: a NEW token cycle for the same member. Never touches subscriptions."""
    today = today or timezone.localdate()
    member = lock_member(member.pk)
    if member.anonymised_at:
        raise RuleViolation("anonymised", "An anonymised member cannot pay a token.")
    cycle = tokens.start_cycle(member, today)  # refuses while a token is already held
    item = Item(
        K.TOKEN_COLLECTION,
        FLOW.IN,
        cycle.amount_snapshot_paise,
        payment_date or today,
        entries,
        label="Refundable security token",
        detail={"cycle": cycle.cycle_no},
        request_key=fs.derived_key(request_key, "token"),
    )
    posted = fs.post_items(
        member=member,
        items=[item],
        actor=actor,
        today=today,
        document_kind="token_collection",
        backdate_reason=backdate_reason,
        backdate_confirmed=backdate_confirmed,
    )
    txn = posted.transactions[0]
    tokens.record_entry(cycle, tokens.E_.COLLECTION, txn.amount_paise, txn, txn.effective_date)
    record(
        "token.collected",
        actor=actor,
        entity_type="member",
        entity_id=member.pk,
        summary={"cycle": cycle.cycle_no, "receipt": txn.receipt_no, **fs.audit_dates(txn)},
    )
    _emit_token_collected(member, cycle, txn)
    return Result(member, None, posted.document, posted.transactions)


@transaction.atomic
def cancel_subscription(*, actor, subscription, reason, today=None):
    """Cancel a subscription. NO financial effect: fees are never refunded (B7)."""
    member = lock_member(subscription.member_id)
    sub = MemberSubscription.objects.get(pk=subscription.pk)
    reason = clean_reason(reason, "A reason to cancel a subscription", required=True)
    if sub.cancelled_at:
        raise RuleViolation("already_cancelled", "This subscription is already cancelled.")
    # O-06: a book out must keep its valid membership. Checked under the member lock, so a loan
    # issued concurrently either commits first (and blocks this) or waits for the cancellation.
    loan_model = apps.get_model("circulation", "Loan")
    if loan_model.objects.filter(member=member, status="active").exists():
        raise RuleViolation(
            "loan_outstanding",
            "A book is still out on this member's loan. Return it first; the subscription "
            "cannot be cancelled while a loan is outstanding.",
        )
    sub.cancelled_at = timezone.now()
    sub.cancel_reason = reason
    sub.save(update_fields=["cancelled_at", "cancel_reason"])
    record(
        "subscription.cancelled",
        actor=actor,
        entity_type="subscription",
        entity_id=sub.pk,
        summary={"member": member.membership_no, "refund": "none (fees are non-refundable)"},
    )
    return sub


def new_request_key():
    return uuid.uuid4()
