"""Token refund and forfeiture, reversals and owner-approved adjustments (Phase 3d).

Nothing here edits or deletes an existing record. A reversal or adjustment is a NEW immutable
transaction that links to the original. There is deliberately no subscription-fee refund: a
subscription payment cannot be reversed, and a token refund is always the full token amount.
"""

from django.db import transaction
from django.utils import timezone

from audit.services import record
from core import outbox
from core.errors import RuleViolation
from core.money import clean_reason
from members.models import Member

from . import services as fs
from . import tokens
from .models import SYSTEM_OVERDUE_FORFEITURE, FinancialTransaction, TokenLedgerEntry
from .services import Item

K = fs.K
FLOW = fs.FLOW
E_ = tokens.E_
T_ = TokenLedgerEntry.Trigger

# Phase 4 registers callables(member) -> truthy while the member still holds an unreturned book.
OUTSTANDING_LOAN_CHECKS = []


def _lock(member):
    return Member.objects.select_for_update().get(pk=member.pk)


def has_outstanding_loans(member):
    return any(check(member) for check in OUTSTANDING_LOAN_CHECKS)


def _require_reason(reason, what):
    return clean_reason(reason, f"A reason for {what}", required=True)


# ---- token refund ------------------------------------------------------------------------------
@transaction.atomic
def refund_token(
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
    """Full refund of a held token on an eligible exit: no partial refunds (cash and/or UPI)."""
    today = today or timezone.localdate()
    member = _lock(member)
    cycle, state = tokens.token_state(member)
    if state == tokens.FORFEITED:
        raise RuleViolation(
            "token_forfeited", "A forfeited token is never refundable or automatically restored."
        )
    if state == tokens.REFUNDED:
        raise RuleViolation("token_already_refunded", "This token was already refunded.")
    if state != tokens.HELD:
        raise RuleViolation("token_missing", "The member has no token on hold to refund.")
    if has_outstanding_loans(member):
        raise RuleViolation(
            "loans_outstanding", "The token cannot be refunded while a book is outstanding."
        )
    amount = cycle.amount_snapshot_paise  # always the full amount
    item = Item(
        K.TOKEN_REFUND,
        FLOW.OUT,
        amount,
        payment_date or today,
        entries,
        label="Refund of refundable security token",
        detail={"cycle": cycle.cycle_no},
        request_key=fs.derived_key(request_key, "refund"),
    )
    posted = fs.post_items(
        member=member,
        items=[item],
        actor=actor,
        today=today,
        document_kind="token_refund",
        backdate_reason=backdate_reason,
        backdate_confirmed=backdate_confirmed,
    )
    txn = posted.transactions[0]
    tokens.record_entry(cycle, E_.REFUND, -amount, txn, txn.effective_date)
    record(
        "token.refunded",
        actor=actor,
        entity_type="member",
        entity_id=member.pk,
        summary={
            "cycle": cycle.cycle_no,
            "receipt": txn.receipt_no,
            **fs.audit_dates(txn),
        },
    )
    outbox.emit(
        outbox.TOKEN_REFUNDED,
        dedupe_key=f"token:{txn.internal_txn_id}:refunded",
        member_id=member.pk,
        event_date=txn.effective_date,
        payload={"cycle_no": cycle.cycle_no, "receipt_no": txn.receipt_no, "amount_paise": amount},
    )
    return posted


# ---- token forfeiture --------------------------------------------------------------------------
@transaction.atomic
def forfeit_token(
    *,
    actor=None,
    member,
    trigger,
    effective_date=None,
    owner_confirmed=False,
    reason="",
    today=None,
):
    """Forfeit the full held token. Its own immutable record; no customer receipt.

    ``overdue_auto`` (day 8) needs no confirmation and may carry a past ``effective_date`` (the
    actual processing time is stored separately). Every other trigger needs the owner's
    confirmation and a reason. Phase 4 calls this from the circulation workflows.

    Attribution (D-15): the automatic ``overdue_auto`` forfeiture is attributed to the system
    process (``actor`` must be None); every other forfeiture keeps the authenticated owner.
    """
    today = today or timezone.localdate()
    if trigger not in T_.values:
        raise RuleViolation("trigger_invalid", "Unknown forfeiture reason.")
    system = ""
    if trigger == T_.OVERDUE_AUTO:
        if actor is not None:
            raise RuleViolation(
                "auto_forfeiture_is_system", "The automatic forfeiture is a system action."
            )
        system = SYSTEM_OVERDUE_FORFEITURE
    elif actor is None:
        raise RuleViolation("actor_required", "A confirmed forfeiture needs the owner.")
    if trigger != T_.OVERDUE_AUTO:
        if not owner_confirmed:
            raise RuleViolation("confirmation_required", "The owner must confirm this forfeiture.")
        reason = _require_reason(reason, "a confirmed forfeiture")
    else:
        owner_confirmed = False
    member = _lock(member)
    cycle, state = tokens.token_state(member)
    if state != tokens.HELD:
        raise RuleViolation(
            "token_not_held",
            "Only a held token can be forfeited (it is forfeited, refunded or missing).",
        )
    if cycle.entries.filter(entry_type=E_.FORFEITURE).exists():
        raise RuleViolation(
            "forfeiture_already_recorded",
            "This token cycle already has a (reversed) forfeiture record. "
            "Record an owner-approved adjustment instead.",
        )
    amount = cycle.amount_snapshot_paise
    item = Item(
        K.TOKEN_FORFEITURE,
        FLOW.NONE,
        amount,
        effective_date or today,
        label=f"Token forfeited ({trigger})",
        reason=reason,
        rule_dated=True,
    )
    posted = fs.post_items(member=member, items=[item], actor=actor, today=today, system=system)
    txn = posted.transactions[0]
    tokens.record_entry(
        cycle,
        E_.FORFEITURE,
        -amount,
        txn,
        txn.effective_date,
        reason=reason,
        trigger=trigger,
        owner_confirmed=owner_confirmed,
    )
    record(
        "token.forfeited",
        actor=actor,
        entity_type="member",
        entity_id=member.pk,
        summary={
            "trigger": trigger,
            "cycle": cycle.cycle_no,
            "owner_confirmed": owner_confirmed,
            "reason": reason,
            **({"system": system} if system else {}),
            **fs.audit_dates(txn),
        },
    )
    outbox.emit(
        outbox.TOKEN_FORFEITED,
        dedupe_key=f"token-forfeiture:{txn.internal_txn_id}",
        member_id=member.pk,
        event_date=txn.effective_date,  # the business date (day 8 for a late automatic one)
        occurred_at=txn.processed_at,
        payload={
            "cycle_no": cycle.cycle_no,
            "trigger": trigger,
            "amount_paise": amount,
            "effective_date": txn.effective_date.isoformat(),
            "system": bool(system),
        },
    )
    return posted


# ---- reversals ---------------------------------------------------------------------------------
@transaction.atomic
def reverse_transaction(
    *,
    actor,
    transaction_id,
    reason,
    entries=(),
    payment_date=None,
    backdate_reason="",
    backdate_confirmed=False,
    request_key=None,
    today=None,
):
    """Reverse a transaction with a new linked record. The original is never touched.

    Allowed: an unused token collection, a token forfeiture, an adjustment. Refused: subscription
    payments (fees are never refunded), token refunds (cash already left: record an adjustment)
    and reversals themselves.
    """
    today = today or timezone.localdate()
    reason = _require_reason(reason, "a reversal")
    original = FinancialTransaction.objects.get(pk=transaction_id)
    member = _lock(original.member)
    original = FinancialTransaction.objects.select_related("member").get(pk=transaction_id)
    if original.kind == K.SUBSCRIPTION_PAYMENT:
        raise RuleViolation(
            "fee_not_reversible",
            "Subscription fees are never refunded or reversed. Use an owner-approved adjustment "
            "if a recording error must be corrected.",
        )
    if original.kind == K.TOKEN_REFUND:
        raise RuleViolation(
            "refund_not_reversible",
            "A paid-out refund cannot be reversed. Record an owner-approved adjustment.",
        )
    if original.kind == K.REVERSAL:
        raise RuleViolation("reversal_not_reversible", "A reversal cannot itself be reversed.")
    if FinancialTransaction.objects.filter(reverses=original).exists():
        raise RuleViolation("already_reversed", "This record was already reversed.")

    ledger_entry = None
    ledger_delta = 0
    if original.kind in (K.TOKEN_COLLECTION, K.TOKEN_FORFEITURE):
        wanted = E_.COLLECTION if original.kind == K.TOKEN_COLLECTION else E_.FORFEITURE
        ledger_entry = (
            original.token_entries.filter(entry_type=wanted).select_related("cycle").get()
        )
        cycle = ledger_entry.cycle
        latest, state = tokens.token_state(member)
        if latest is None or latest.pk != cycle.pk:
            raise RuleViolation(
                "cycle_superseded",
                "Only the member's latest token cycle can be corrected. "
                "Record an owner-approved adjustment instead.",
            )
        if original.kind == K.TOKEN_COLLECTION:
            if state != tokens.HELD:
                raise RuleViolation(
                    "token_in_use",
                    "This token was already forfeited or refunded and cannot be reversed.",
                )
            if has_outstanding_loans(member):
                # O-07: the token secures the book that is out. Same rule as a token refund.
                # Distinct from a refund: a reversal returns money the owner collected in error.
                raise RuleViolation(
                    "loans_outstanding",
                    "The token collection cannot be reversed while a book is outstanding.",
                )
            ledger_delta = -original.amount_paise
        else:
            if state != tokens.FORFEITED:
                raise RuleViolation("token_not_forfeited", "The forfeiture is no longer in force.")
            ledger_delta = original.amount_paise

    flow = {FLOW.IN: FLOW.OUT, FLOW.OUT: FLOW.IN, FLOW.NONE: FLOW.NONE}[original.cash_flow]
    item = Item(
        K.REVERSAL,
        flow,
        original.amount_paise,
        payment_date or today,
        list(entries),
        label=f"Reversal of {original.receipt_no or original.get_kind_display()}",
        reason=reason,
        reverses=original,
        request_key=fs.derived_key(request_key, "reversal"),
    )
    posted = fs.post_items(
        member=member,
        items=[item],
        actor=actor,
        today=today,
        backdate_reason=backdate_reason,
        backdate_confirmed=backdate_confirmed,
    )
    txn = posted.transactions[0]
    if ledger_entry is not None:
        tokens.record_entry(
            ledger_entry.cycle,
            E_.REVERSAL,
            ledger_delta,
            txn,
            txn.effective_date,
            reason=reason,
            reverses=ledger_entry,
        )
    record(
        "finance.reversal",
        actor=actor,
        entity_type="transaction",
        entity_id=original.internal_txn_id,
        summary={
            "kind": original.kind,
            "reason": reason,
            "reversal": str(txn.internal_txn_id),
            **fs.audit_dates(txn),
        },
    )
    return posted


# ---- adjustments -------------------------------------------------------------------------------
@transaction.atomic
def post_adjustment(
    *,
    actor,
    member,
    direction,
    amount_paise,
    reason,
    owner_approved,
    confirm_not_fee_refund=False,
    entries=(),
    related_id=None,
    payment_date=None,
    backdate_reason="",
    backdate_confirmed=False,
    request_key=None,
    today=None,
):
    """An owner-approved financial adjustment (ADJ receipt). The token ledger is not changed: a
    recovered lost book, for example, leaves the forfeiture standing and is settled here (B12).

    D-7: an adjustment is never a way to refund a subscription fee. Money going OUT needs the
    owner's explicit statement that it is not a fee refund, and cannot reference a subscription
    payment (a database trigger enforces the same rule). Other accounting adjustments, with a
    mandatory reason and audit record, remain permitted.
    """
    today = today or timezone.localdate()
    if not owner_approved:
        raise RuleViolation("approval_required", "An adjustment needs the owner's approval.")
    reason = _require_reason(reason, "an adjustment")
    if direction not in (FLOW.IN, FLOW.OUT, FLOW.NONE):
        raise RuleViolation("direction_invalid", "Choose money in, money out or no cash.")
    if direction == FLOW.OUT and not confirm_not_fee_refund:
        raise RuleViolation(
            "fee_refund_ack_required",
            "Confirm that this payment out is not a refund of a subscription fee "
            "(subscription fees are never refunded).",
        )
    member = _lock(member)
    related = None
    if related_id:
        related = FinancialTransaction.objects.filter(pk=related_id, member=member).first()
        if related is None:
            raise RuleViolation("related_unknown", "The referenced record was not found.")
        if direction == FLOW.OUT and related.kind == K.SUBSCRIPTION_PAYMENT:
            raise RuleViolation(
                "fee_refund_blocked",
                "An adjustment cannot pay out against a subscription payment: "
                "subscription fees are never refunded.",
            )
    item = Item(
        K.ADJUSTMENT,
        direction,
        amount_paise,
        payment_date or today,
        list(entries),
        label="Financial adjustment",
        reason=reason,
        adjusts=related,
        request_key=fs.derived_key(request_key, "adjustment"),
    )
    posted = fs.post_items(
        member=member,
        items=[item],
        actor=actor,
        today=today,
        document_kind="adjustment",
        backdate_reason=backdate_reason,
        backdate_confirmed=backdate_confirmed,
        notes=reason,
    )
    txn = posted.transactions[0]
    record(
        "finance.adjustment",
        actor=actor,
        entity_type="member",
        entity_id=member.pk,
        summary={
            "direction": direction,
            "receipt": txn.receipt_no,
            "reason": reason,
            "related": str(related.internal_txn_id) if related else "",
            "not_a_fee_refund_confirmed": bool(confirm_not_fee_refund),
            **fs.audit_dates(txn),
        },
    )
    return posted
