"""Token cycles and the token ledger. The token is a refundable deposit, never revenue (B21).

A member's token state is DERIVED from the immutable ledger entries of their latest cycle:
``held`` (collected, usable), ``forfeited``, ``refunded`` or ``reversed`` (collection cancelled),
or ``none`` (no cycle yet). Nothing is stored as a flag, so state cannot drift from the ledger.
"""

from core.errors import RuleViolation
from core.models import LibrarySetting
from core.money import clean_reason

from .models import TokenAccount, TokenCycle, TokenLedgerEntry

E_ = TokenLedgerEntry.EntryType

NONE, HELD, FORFEITED, REFUNDED, REVERSED = "none", "held", "forfeited", "refunded", "reversed"


def latest_cycle(member):
    return (
        TokenCycle.objects.filter(account__member=member)
        .order_by("-cycle_no")
        .select_related("account")
        .first()
    )


def cycle_state(cycle):
    if cycle is None:
        return NONE
    entries = list(cycle.entries.all())
    reversed_ids = {e.reverses_id for e in entries if e.reverses_id}
    for e in entries:
        if e.entry_type == E_.COLLECTION and e.id in reversed_ids:
            return REVERSED
    for e in entries:
        if e.entry_type == E_.FORFEITURE and e.id not in reversed_ids:
            return FORFEITED
    for e in entries:
        if e.entry_type == E_.REFUND and e.id not in reversed_ids:
            return REFUNDED
    return HELD if any(e.entry_type == E_.COLLECTION for e in entries) else NONE


def token_state(member):
    """(latest cycle or None, state)."""
    cycle = latest_cycle(member)
    return cycle, cycle_state(cycle)


def balance_paise(cycle):
    return sum(e.delta_paise for e in cycle.entries.all())


def start_cycle(member, today):
    """Create the next token cycle (a fresh ₹500 is being collected). Same member, new cycle."""
    cycle, state = token_state(member)
    if state == HELD:
        raise RuleViolation(
            "token_already_held",
            "This member's token is already held; it is collected once per cycle.",
        )
    account, _ = TokenAccount.objects.get_or_create(member=member)
    return TokenCycle.objects.create(
        account=account,
        cycle_no=(cycle.cycle_no + 1) if cycle else 1,
        amount_snapshot_paise=LibrarySetting.load().token_amount_paise,
        started_on=today,
    )


def record_entry(
    cycle,
    entry_type,
    delta_paise,
    txn,
    effective_date,
    *,
    reason="",
    trigger="",
    owner_confirmed=False,
    reverses=None,
):
    return TokenLedgerEntry.objects.create(
        cycle=cycle,
        entry_type=entry_type,
        delta_paise=delta_paise,
        forfeiture_trigger=trigger,
        owner_confirmed=owner_confirmed,
        transaction=txn,
        reverses=reverses,
        effective_date=effective_date,
        reason=clean_reason(reason),
    )
