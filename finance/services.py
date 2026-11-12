"""Posting financial records. Only this module creates ledger rows.

Every public function that writes must run inside ``transaction.atomic`` (callers lock the member
row first, always in the order member -> id sequence, so concurrent operations cannot deadlock).
Money is integer paise. Nothing here ever updates or deletes a row: corrections are reversals and
adjustments (see ``finance.corrections``).
"""

import uuid
from dataclasses import dataclass, field
from datetime import date

from django.db import IntegrityError, connection, transaction
from django.utils import timezone

from core import outbox
from core.errors import RuleViolation
from core.models import LibrarySetting
from core.money import MAX_REASON_LENGTH, check_amount, clean_reason
from core.sequences import next_value

from .models import FinancialTransaction, PaymentEntry, ReceiptDocument

K = FinancialTransaction.Kind
FLOW = FinancialTransaction.CashFlow

# receipt sequence -> (printed prefix, id_sequence name)
RECEIPT_SEQUENCES = {
    "subscription": ("SUB", "receipt_subscription"),
    "token_collection": ("TOK", "receipt_token_collection"),
    "token_refund": ("REF", "receipt_token_refund"),
    "adjustment": ("ADJ", "receipt_adjustment"),
}
KIND_TO_SEQUENCE = {
    K.SUBSCRIPTION_PAYMENT: "subscription",
    K.TOKEN_COLLECTION: "token_collection",
    K.TOKEN_REFUND: "token_refund",
    K.ADJUSTMENT: "adjustment",
}
METHODS = {m for m, _ in PaymentEntry.Method.choices}
MAX_REFERENCE = 120
MAX_TEXT = MAX_REASON_LENGTH


@dataclass(frozen=True)
class Entry:
    """One cash or UPI component of a payment. ``paid_on`` defaults to the effective date."""

    method: str
    amount_paise: int
    paid_on: date | None = None
    reference: str = ""


@dataclass
class Item:
    """One ledger record to post (one transaction)."""

    kind: str
    cash_flow: str
    amount_paise: int
    effective_date: date
    entries: list = field(default_factory=list)
    label: str = ""
    detail: dict = field(default_factory=dict)
    reason: str = ""
    reverses: FinancialTransaction | None = None
    adjusts: FinancialTransaction | None = None
    request_key: uuid.UUID | None = None
    # The effective date is fixed by a rule, not typed by the owner (e.g. day 8 of an overdue
    # forfeiture processed late, N6): it is not a backdated owner operation.
    rule_dated: bool = False


@dataclass
class Posted:
    document: ReceiptDocument | None
    transactions: list


def format_receipt_no(sequence, year, number):
    return f"{RECEIPT_SEQUENCES[sequence][0]}-{year}-{number:06d}"


def _int(value, code, message):
    if isinstance(value, bool) or not isinstance(value, int):
        raise RuleViolation(code, message)
    return value


def _amount(value, what):
    """Whole paise within 0..MAX_PAISE; anything else is a clear validation error."""
    return check_amount(
        _int(value, "amount_invalid", f"{what} must be a whole number of paise."), what
    )


def normalise_entries(raw, effective_date):
    entries = []
    for e in raw or []:
        if isinstance(e, dict):
            e = Entry(**e)
        entries.append(
            Entry(
                method=e.method,
                amount_paise=_amount(e.amount_paise, "A payment amount"),
                paid_on=e.paid_on or effective_date,
                reference=(e.reference or "").strip(),
            )
        )
    return entries


def validate_entries(entries, amount_paise, today):
    """Split payments must settle the obligation exactly: no partial or excess payment."""
    if not entries:
        raise RuleViolation("payment_required", "Enter at least one payment (cash or UPI).")
    for e in entries:
        if e.method not in METHODS:
            raise RuleViolation("method_invalid", "Payment method must be cash or UPI.")
        if e.amount_paise <= 0:
            raise RuleViolation("amount_invalid", "Each payment amount must be above zero.")
        if e.paid_on > today:
            raise RuleViolation("future_date", "A payment date cannot be in the future.")
        if len(e.reference) > MAX_REFERENCE:
            raise RuleViolation("reference_too_long", "A payment reference is too long.")
    total = sum(e.amount_paise for e in entries)
    if total < amount_paise:
        raise RuleViolation(
            "underpaid",
            f"Payments total {total / 100:.2f} but {amount_paise / 100:.2f} is due. "
            "Nothing is activated or recorded until the full amount is paid.",
        )
    if total > amount_paise:
        raise RuleViolation(
            "overpaid",
            f"Payments total {total / 100:.2f}, more than the {amount_paise / 100:.2f} due.",
        )


def check_backdating(effective_date, entries, today, reason, confirmed):
    """B19/N12: any past date needs a reason and an extra financial confirmation."""
    if effective_date > today:
        raise RuleViolation("future_date", "A financial date cannot be in the future.")
    backdated = effective_date < today or any(e.paid_on < today for e in entries)
    if not backdated:
        return False, ""
    reason = clean_reason(reason, "The reason for the past date")
    if not reason:
        raise RuleViolation("backdate_reason_required", "A reason is required for a past date.")
    if not confirmed:
        raise RuleViolation(
            "backdate_confirmation_required",
            "Confirm that this financial record carries a past date.",
        )
    return True, reason


def derived_key(request_key, kind):
    return uuid.uuid5(request_key, kind) if request_key else None


def allocate_receipt(sequence, today):
    """Next receipt number of a sequence; the year is that of the ISSUANCE date (Asia/Kolkata)."""
    prefix, name = RECEIPT_SEQUENCES[sequence]
    return today.year, format_receipt_no(sequence, today.year, next_value(name, today.year))


def _library_header():
    s = LibrarySetting.load()
    return {
        "name": s.library_name,
        "address": s.library_address,
        "phone": s.library_phone,
        "email": s.library_email,
        "footer": s.receipt_footer,
    }


def post_items(
    *,
    member,
    items,
    actor,
    today,
    document_kind="",
    backdate_reason="",
    backdate_confirmed=False,
    notes="",
    system="",
):
    """Post one or more ledger records atomically, with one receipt document for the receipted ones.

    Validates amounts, split payments (exact settlement), dates and backdating; allocates the
    per-year receipt numbers; stores an immutable receipt snapshot; returns ``Posted``.
    """
    if not connection.in_atomic_block:
        raise RuntimeError("post_items must run inside transaction.atomic")
    notes = clean_reason(notes, "The note")
    if not items:
        raise ValueError("no items")
    if system:  # D-15: only the automatic forfeiture may be attributed to a system process
        if actor is not None or any(i.kind != K.TOKEN_FORFEITURE for i in items):
            raise RuleViolation(
                "system_actor_misuse",
                "A system attribution is valid only for an automatic token forfeiture.",
            )
    elif actor is None:
        raise RuleViolation("actor_required", "A financial record needs the authenticated owner.")
    prepared = []
    backdated_any, reason_text = False, ""
    for item in items:
        amount = _amount(item.amount_paise, "The amount")
        item.reason = clean_reason(item.reason, "The reason")
        if amount <= 0:
            raise RuleViolation("amount_invalid", "The amount must be above zero.")
        item.entries = normalise_entries(item.entries, item.effective_date)
        if item.cash_flow == FLOW.NONE:
            if item.entries:
                raise RuleViolation("unexpected_payment", "This record moves no cash.")
        else:
            validate_entries(item.entries, amount, today)
        if item.rule_dated:
            if item.effective_date > today:
                raise RuleViolation("future_date", "A financial date cannot be in the future.")
            backdated, text = False, ""
        else:
            backdated, text = check_backdating(
                item.effective_date, item.entries, today, backdate_reason, backdate_confirmed
            )
        backdated_any, reason_text = backdated_any or backdated, text or reason_text
        if (
            item.request_key
            and FinancialTransaction.objects.filter(request_key=item.request_key).exists()
        ):
            raise RuleViolation(
                "duplicate_submission", "This form was already submitted; it was not repeated."
            )
        prepared.append(item)

    receipt_items = [i for i in prepared if i.kind in KIND_TO_SEQUENCE]
    ids = {id(i): uuid.uuid4() for i in prepared}
    numbers = {}
    for item in receipt_items:  # fixed order: the order of the items
        sequence = KIND_TO_SEQUENCE[item.kind]
        numbers[id(item)] = (sequence, *allocate_receipt(sequence, today))

    document = None
    if receipt_items:
        lines = []
        for item in receipt_items:
            sequence, year, number = numbers[id(item)]
            lines.append(
                {
                    "kind": item.kind,
                    "label": item.label or item.kind,
                    "receipt_no": number,
                    "internal_txn_id": str(ids[id(item)]),
                    "amount_paise": item.amount_paise,
                    "cash_flow": item.cash_flow,
                    "effective_date": item.effective_date.isoformat(),
                    "detail": item.detail,
                    "payments": [
                        {
                            "method": e.method,
                            "amount_paise": e.amount_paise,
                            "paid_on": e.paid_on.isoformat(),
                            "reference": e.reference,
                        }
                        for e in item.entries
                    ],
                }
            )
        document = ReceiptDocument.objects.create(
            member=member,
            kind=document_kind or KIND_TO_SEQUENCE[receipt_items[0].kind],
            document_date=today,
            snapshot={
                "library": _library_header(),
                "membership_no": member.membership_no,  # the name is shown live (anonymisation)
                "date": today.isoformat(),
                "lines": lines,
                "notes": notes,
            },
        )

    posted = []
    for item in prepared:
        receipt = {}
        if id(item) in numbers:
            sequence, year, number = numbers[id(item)]
            receipt = dict(
                receipt_sequence=sequence,
                receipt_issued_on=today,
                receipt_year=year,
                receipt_no=number,
                receipt_document=document,
            )
        try:
            with transaction.atomic():  # savepoint: a duplicate key must not poison the caller
                txn = FinancialTransaction.objects.create(
                    internal_txn_id=ids[id(item)],
                    member=member,
                    kind=item.kind,
                    cash_flow=item.cash_flow,
                    amount_paise=item.amount_paise,
                    effective_date=item.effective_date,
                    reverses=item.reverses,
                    adjusts=item.adjusts,
                    reason=item.reason,
                    backdated=backdated_any,
                    backdate_reason=reason_text if backdated_any else "",
                    backdate_financial_confirmed=backdated_any,
                    request_key=item.request_key,
                    recorded_by=None if system else actor,
                    recorded_by_system=system,
                    **receipt,
                )
        except IntegrityError:
            if (
                item.request_key
                and FinancialTransaction.objects.filter(request_key=item.request_key).exists()
            ):
                raise RuleViolation(
                    "duplicate_submission", "This form was already submitted; it was not repeated."
                ) from None
            raise
        PaymentEntry.objects.bulk_create(
            PaymentEntry(
                transaction=txn,
                method=e.method,
                amount_paise=e.amount_paise,
                paid_on=e.paid_on,
                reference=e.reference,
            )
            for e in item.entries
        )
        posted.append(txn)
    if document is not None:
        outbox.emit(
            outbox.PAYMENT_RECEIPTED,
            dedupe_key=f"receipt:{document.pk}",
            member_id=member.pk,
            event_date=today,
            payload={
                "document_id": document.pk,
                "document_kind": document.kind,
                "receipts": [
                    {
                        "receipt_no": t.receipt_no,
                        "kind": t.kind,
                        "amount_paise": t.amount_paise,
                    }
                    for t in posted
                    if t.receipt_no
                ],
            },
        )
    return Posted(document=document, transactions=posted)


def audit_dates(
    txn, *, event_date=None, start_backdated=False, start_reason="", start_confirmed=False
):
    """The dates/backdating part of an audit summary (B19, PRD section 11).

    Always records: whether the operation was backdated, the financial effective date, the actual
    processing timestamp and, when backdated, the reason and the owner's extra confirmation.
    ``event_date`` is the non-financial event date (for example a subscription start date); when
    only that date is in the past (payment made today) its reason and confirmation come from the
    ``start_*`` arguments.
    """
    backdated = bool(txn.backdated or start_backdated)
    summary = {
        "backdated": backdated,
        "effective_date": txn.effective_date.isoformat(),
        "processed_at": txn.processed_at.isoformat(),
    }
    if event_date is not None:
        summary["event_date"] = event_date.isoformat()
        summary["event_date_backdated"] = bool(start_backdated)
    if backdated:
        summary["backdate_reason"] = txn.backdate_reason or start_reason
        summary["backdate_owner_confirmed"] = bool(
            txn.backdate_financial_confirmed or start_confirmed
        )
    return summary


def today_local():
    return timezone.localdate()
