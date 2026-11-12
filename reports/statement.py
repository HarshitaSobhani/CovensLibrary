"""Member statement (PRD 6.11): one member's subscriptions, money, tokens and loans, for all
dates or a chosen range (decision D-28f). An anonymised member appears as 'Anonymised member'
with the member ID only."""

from django.db.models import Q

from circulation.models import Loan
from finance.models import FinancialTransaction, TokenLedgerEntry
from memberships import policy
from memberships.models import MemberSubscription

from .core import DATE, DATETIME, MONEY, TEXT, Column, Report, Section
from .masking import mask

LABEL = dict(FinancialTransaction.Kind.choices)


def build(member, start, end, today):
    def in_range(field):
        q = Q()
        if start:
            q &= Q(**{f"{field}__gte": start})
        if end:
            q &= Q(**{f"{field}__lte": end})
        return q

    span = (
        f"{start:%d %b %Y} to {end:%d %b %Y}"
        if start and end
        else "all dates"
        if not (start or end)
        else f"from {start:%d %b %Y}"
        if start
        else f"up to {end:%d %b %Y}"
    )
    anonymised = member.anonymised_at is not None
    details = Section(
        "Member",
        [Column("Item"), Column("Value")],
        [
            ["Member ID", member.membership_no],
            ["Name", member.full_name],
            ["Mobile", "" if anonymised else member.mobile_e164],
            ["Email", "" if anonymised else member.email],
            ["Joined", member.joined_on.strftime("%d %b %Y")],
            ["Status", member.get_status_display() + (" (anonymised)" if anonymised else "")],
        ],
    )
    subs = Section(
        "Subscriptions",
        [
            Column("Plan"),
            Column("Starts", DATE),
            Column("Expires (first expired day)", DATE),
            Column("Last valid day", DATE),
            Column("State"),
            Column("Fee", MONEY),
        ],
        [
            [
                s.plan_name_snapshot,
                s.starts_on,
                s.expires_on,
                policy.last_valid_day(s.expires_on),
                "Cancelled" if s.cancelled_at else policy.state(s.period, today).capitalize(),
                s.price_paise_snapshot,
            ]
            for s in MemberSubscription.objects.filter(member=member).order_by("starts_on")
            if (not start or s.expires_on > start) and (not end or s.starts_on <= end)
        ],
        note="Subscription fees are not refundable.",
    )
    txns = (
        FinancialTransaction.objects.filter(member=member)
        .filter(in_range("effective_date"))
        .select_related("recorded_by")
        .order_by("effective_date", "id")
    )
    money = Section(
        "Payments, receipts, refunds, forfeitures and adjustments",
        [
            Column("Date", DATE),
            Column("Kind"),
            Column("Receipt"),
            Column("Money"),
            Column("Amount", MONEY),
            Column("Recorded by"),
            Column("Reason"),
        ],
        [
            [
                t.effective_date,
                LABEL[t.kind],
                t.receipt_no,
                t.get_cash_flow_display(),
                t.amount_paise,
                t.recorded_by.get_username() if t.recorded_by_id else "automatic (day 8)",
                mask(anonymised, t.reason),
            ]
            for t in txns
        ],
    )
    ledger = Section(
        "Token ledger (the token is held for the member; it is not revenue)",
        [
            Column("Date", DATE),
            Column("Cycle"),
            Column("Entry"),
            Column("Amount", MONEY),
            Column("Processed", DATETIME),
        ],
        [
            [
                e.effective_date,
                f"Cycle {e.cycle.cycle_no}",
                e.get_entry_type_display(),
                e.delta_paise,
                e.processed_at,
            ]
            for e in TokenLedgerEntry.objects.filter(cycle__account__member=member)
            .filter(in_range("effective_date"))
            .select_related("cycle")
            .order_by("effective_date", "id")
        ],
    )
    loans = Section(
        "Loans",
        [
            Column("Copy"),
            Column("Title"),
            Column("Issued", DATE),
            Column("Due", DATE),
            Column("Returned", DATE),
            Column("Outcome", TEXT),
        ],
        [
            [
                ln.copy.copy_code,
                ln.copy.edition.title,
                ln.issued_on,
                ln.due_on,
                ln.returned_on,
                ln.get_status_display()
                + (f" ({ln.return_condition})" if ln.return_condition else ""),
            ]
            for ln in Loan.objects.filter(member=member)
            .filter(in_range("issued_on"))
            .select_related("copy__edition")
            .order_by("issued_on", "id")
        ],
    )
    return Report(
        "statement",
        f"Member statement: {member.membership_no}",
        span,
        [details, subs, money, ledger, loans],
        ["Money out is shown as recorded; an unlinked cash-out adjustment is never a fee refund."],
    )
