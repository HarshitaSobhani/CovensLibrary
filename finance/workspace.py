"""Read-only queries for the Payments & Tokens workspace.

Nothing here writes. Every figure comes from the immutable ledger and the existing services
(``finance.reporting`` for collections, ``finance.tokens`` for token state,
``memberships.services`` for the renewal and borrowing rules); no business rule is repeated.
"""

from dataclasses import dataclass
from datetime import date

from django.db.models import Q, Sum

from audit.models import AuditEvent
from circulation.models import Loan
from core.http import clean_query
from members.models import Member
from memberships import services as membership_services
from memberships.models import MemberSubscription
from reports.masking import mask

from . import reporting, tokens
from .models import FinancialTransaction, TokenCycle, TokenLedgerEntry

K = FinancialTransaction.Kind
E_ = TokenLedgerEntry.EntryType

KIND_LABEL = dict(K.choices)
MODE_CHOICES = [("cash", "Cash"), ("upi", "UPI (online)"), ("none", "No payment entry")]
MODE_LABEL = {"cash": "Cash", "upi": "UPI"}
TOKEN_STATUS_LABEL = {
    tokens.HELD: "Held",
    tokens.REFUNDED: "Refunded",
    tokens.FORFEITED: "Forfeited",
    tokens.REVERSED: "Collection reversed",
}
PAGE_SIZE = 25


# ------------------------------------------------------------------ A. overview
def overview(start, end):
    """Cards for the period. Collections are the existing report figures (cash by payment date;
    forfeitures by effective date). Token money is a liability, never subscription revenue."""
    c = reporting.collections(start, end)
    held = TokenLedgerEntry.objects.aggregate(t=Sum("delta_paise"))["t"] or 0
    return {
        "subscription_collections": c["subscription_fees"],
        "token_collections": c["token_collections"],
        "token_refunds": c["token_refunds"],
        "token_forfeitures": c["tokens_forfeited"],
        "adjustments_in": c["adjustments_in"],
        "adjustments_out": c["adjustments_out"],
        "reversals_in": c["reversals_in"],
        "reversals_out": c["reversals_out"],
        "by_method": c["by_method"],
        "token_balance_held": held,
    }


# ------------------------------------------------------------------ B. ledger
@dataclass
class LedgerFilter:
    start: date
    end: date
    member_id: int | None = None
    member_q: str = ""
    kind: str = ""
    mode: str = ""
    receipt: str = ""


def ledger_queryset(f):
    qs = (
        FinancialTransaction.objects.filter(effective_date__gte=f.start, effective_date__lte=f.end)
        .select_related("member", "reversed_by")
        .prefetch_related("entries")
        .order_by("-effective_date", "-id")
    )
    if f.member_id:
        qs = qs.filter(member_id=f.member_id)
    elif f.member_q:
        q = clean_query(f.member_q)
        qs = qs.filter(Q(member__full_name__icontains=q) | Q(member__membership_no__icontains=q))
    if f.kind in K.values:
        qs = qs.filter(kind=f.kind)
    if f.mode in MODE_LABEL:
        qs = qs.filter(entries__method=f.mode).distinct()
    elif f.mode == "none":
        qs = qs.filter(entries__isnull=True)
    receipt = clean_query(f.receipt, 40)
    if receipt:
        qs = qs.filter(receipt_no__icontains=receipt)
    return qs


def payment_mode(txn):
    methods = sorted({e.method for e in txn.entries.all()})
    return " + ".join(MODE_LABEL[m] for m in methods) if methods else ""


def reference_text(txn):
    refs = [e.reference for e in txn.entries.all() if e.reference]
    return mask(txn.member.anonymised_at, ", ".join(refs))


def ledger_row(txn):
    """What the ledger table shows for one transaction (a plain dict; nothing is changed)."""
    return {
        "txn": txn,
        "kind_label": KIND_LABEL[txn.kind],
        "mode": payment_mode(txn),
        "reference": reference_text(txn),
        "flow": txn.cash_flow,
        "reversed": hasattr(txn, "reversed_by"),
    }


# ------------------------------------------------------------------ C. tokens
@dataclass
class TokenFilter:
    status: str = ""
    start: date | None = None
    end: date | None = None


def _entry(entries, entry_type, reversed_ids):
    for e in entries:
        if e.entry_type == entry_type and e.id not in reversed_ids:
            return e
    return None


def token_rows(f):
    """One row per token cycle (the ledger is never edited; state is derived from it)."""
    cycles = (
        TokenCycle.objects.select_related("account__member")
        .prefetch_related("entries__transaction", "entries__loan")
        .order_by("-id")
    )
    rows = []
    for cycle in cycles:
        entries = list(cycle.entries.all())
        state = tokens.cycle_state(cycle)
        if state == tokens.NONE:
            continue
        reversed_ids = {e.reverses_id for e in entries if e.reverses_id}
        collection = next((e for e in entries if e.entry_type == E_.COLLECTION), None)
        refund = _entry(entries, E_.REFUND, reversed_ids)
        forfeiture = _entry(entries, E_.FORFEITURE, reversed_ids)
        closing = forfeiture or refund
        member = cycle.account.member
        row = {
            "cycle": cycle,
            "member": member,
            "state": state,
            "status_label": TOKEN_STATUS_LABEL[state],
            "amount_paise": tokens.balance_paise(cycle) if state == tokens.HELD else 0,
            "collected_paise": collection.delta_paise if collection else 0,
            "collected_on": collection.effective_date if collection else None,
            "collection_txn": collection.transaction if collection else None,
            "closed_on": closing.effective_date if closing else None,
            "closing_txn": closing.transaction if closing else None,
            "closing_kind": ("Forfeited" if forfeiture else "Refunded") if closing else "",
            "forfeiture_reason": (
                mask(member.anonymised_at, forfeiture.reason) if forfeiture else ""
            ),
            "forfeiture_trigger": forfeiture.get_forfeiture_trigger_display() if forfeiture else "",
            "automatic": bool(forfeiture and forfeiture.transaction.recorded_by_system),
            "loan": getattr(forfeiture, "loan", None) if forfeiture else None,
        }
        rows.append(row)
    if f.status in TOKEN_STATUS_LABEL:
        rows = [r for r in rows if r["state"] == f.status]
    if f.start or f.end:
        rows = [r for r in rows if _any_event_in(r, f.start, f.end)]
    return rows


def _any_event_in(row, start, end):
    for day in (row["collected_on"], row["closed_on"]):
        if day and (start is None or day >= start) and (end is None or day <= end):
            return True
    return False


def token_totals(rows):
    out = {s: {"count": 0, "paise": 0} for s in TOKEN_STATUS_LABEL}
    for r in rows:
        out[r["state"]]["count"] += 1
        out[r["state"]]["paise"] += r["collected_paise"]
    return out


def needs_fresh_token(today):
    """Active, non-anonymised members who may not borrow or renew until a token is on hold.
    Uses the existing renewal rule (``renewal_blockers``) so the two can never disagree."""
    out = []
    for member in Member.objects.filter(status=Member.Status.ACTIVE, anonymised_at__isnull=True):
        codes = {c for c, _ in membership_services.renewal_blockers(member, today)}
        if "token_forfeited" in codes:
            out.append((member, "Token forfeited"))
        elif "token_missing" in codes:
            cycle, state = tokens.token_state(member)
            label = "No token collected yet" if cycle is None else f"Token {state}"
            out.append((member, label))
    return sorted(out, key=lambda pair: pair[0].membership_no)


# ------------------------------------------------------------------ D. details
AUDIT_FIELDS = (
    "trigger",
    "cycle",
    "owner_confirmed",
    "direction",
    "backdated",
    "effective_date",
    "processed_at",
    "event_date",
    "backdate_owner_confirmed",
    "plan",
    "system",
    "not_a_fee_refund_confirmed",
)
MASKED_FIELDS = ("reason", "backdate_reason")


def audit_history(txn):
    """Audit events that belong to this transaction. The audit log has no foreign key to the
    ledger, so events are matched on the facts they record about it: the exact processing
    timestamp, the receipt number, or the transaction's own id."""
    uid = str(txn.internal_txn_id)
    cond = (
        Q(entity_type="transaction", entity_id=uid)
        | Q(summary__processed_at=txn.processed_at.isoformat())
        | Q(summary__reversal=uid)
        | Q(summary__related=uid)
    )
    if txn.receipt_no:
        cond |= Q(summary__receipt=txn.receipt_no)
    anonymised = txn.member.anonymised_at
    history = []
    for event in (
        AuditEvent.objects.filter(cond).select_related("actor").order_by("occurred_at", "id")
    ):
        summary = event.summary or {}
        details = [(k, summary[k]) for k in AUDIT_FIELDS if k in summary]
        details += [(k, mask(anonymised, summary[k])) for k in MASKED_FIELDS if summary.get(k)]
        history.append(
            {
                "event": event,
                "actor": event.actor.get_username() if event.actor_id else "system",
                "details": details,
            }
        )
    return history


def related(txn):
    """The records this transaction is linked to (all read-only)."""
    entries = list(txn.token_entries.select_related("cycle"))
    entry = entries[0] if entries else None
    return {
        "subscription": MemberSubscription.objects.filter(payment_txn=txn).first(),
        "token_entry": entry,
        "token_cycle": entry.cycle if entry else None,
        "loan": Loan.objects.filter(forfeiture_entry__transaction=txn)
        .select_related("copy")
        .first(),
        "original": txn.reverses or txn.adjusts,
        "reversed_by": getattr(txn, "reversed_by", None),
        "adjustments": list(txn.adjustments.all()),
        "receipt_lines": list(txn.receipt_document.lines.exclude(pk=txn.pk))
        if txn.receipt_document_id
        else [],
    }
