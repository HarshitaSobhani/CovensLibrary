from django import template
from django.utils import timezone

from finance import tokens
from finance.models import FinancialTransaction
from memberships import policy, services

register = template.Library()

TOKEN_TEXT = {
    tokens.NONE: "No token collected",
    tokens.HELD: "Held (refundable)",
    tokens.FORFEITED: "Forfeited — borrowing and renewal are blocked until a fresh token is paid",
    tokens.REFUNDED: "Refunded",
    tokens.REVERSED: "Collection reversed",
}


REVERSIBLE = ("token_collection", "token_forfeiture", "adjustment")


def _transactions(member):
    rows = list(
        FinancialTransaction.objects.filter(member=member)
        .select_related("reversed_by")
        .order_by("-id")[:50]
    )
    for t in rows:
        t.reversed_by_id = getattr(getattr(t, "reversed_by", None), "pk", None)
        t.can_reverse = t.kind in REVERSIBLE and t.reversed_by_id is None
    return rows


@register.inclusion_tag("memberships/panel.html")
def membership_panel(member):
    today = timezone.localdate()
    subs = list(member.subscriptions.select_related("payment_txn").order_by("-starts_on"))
    cycle, token_state = tokens.token_state(member)
    rows = [(s, policy.state(s.period, today)) for s in subs]
    return {
        "member": member,
        "rows": rows,
        "token_state": token_state,
        "token_text": TOKEN_TEXT[token_state],
        "cycle": cycle,
        "renewal_blockers": services.renewal_blockers(member, today),
        "borrowing_blockers": services.borrowing_blockers(member, today),
        "transactions": _transactions(member),
        "has_current": services.current_subscription(member, today) is not None,
    }
