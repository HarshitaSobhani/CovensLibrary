"""Read-only financial summaries. Payment-date collections and period allocation are separate
views (B8/B21): tokens are never counted as subscription revenue."""

from django.db.models import Sum

from .models import FinancialTransaction, PaymentEntry

K = FinancialTransaction.Kind


def collections(start, end):
    """Cash actually moved between ``start`` and ``end`` (inclusive), by each entry's OWN paid_on.

    Returns integer paise, with tokens, refunds, adjustments and reversals kept apart.
    """
    rows = (
        PaymentEntry.objects.filter(paid_on__gte=start, paid_on__lte=end)
        .values("transaction__kind", "transaction__cash_flow", "method")
        .annotate(total=Sum("amount_paise"))
    )
    out = {
        "subscription_fees": 0,
        "token_collections": 0,
        "token_refunds": 0,
        "adjustments_in": 0,
        "adjustments_out": 0,
        "reversals_in": 0,
        "reversals_out": 0,
        "by_method": {"cash": 0, "upi": 0},
    }
    for row in rows:
        kind, flow, total = row["transaction__kind"], row["transaction__cash_flow"], row["total"]
        if kind == K.SUBSCRIPTION_PAYMENT:
            out["subscription_fees"] += total
        elif kind == K.TOKEN_COLLECTION:
            out["token_collections"] += total
        elif kind == K.TOKEN_REFUND:
            out["token_refunds"] += total
        elif kind == K.ADJUSTMENT:
            out["adjustments_in" if flow == "in" else "adjustments_out"] += total
        elif kind == K.REVERSAL:
            out["reversals_in" if flow == "in" else "reversals_out"] += total
        out["by_method"][row["method"]] += total if flow == "in" else -total
    out["tokens_forfeited"] = (
        FinancialTransaction.objects.filter(
            kind=K.TOKEN_FORFEITURE, effective_date__gte=start, effective_date__lte=end
        ).aggregate(t=Sum("amount_paise"))["t"]
        or 0
    )
    return out
