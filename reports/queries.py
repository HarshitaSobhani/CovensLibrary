"""The reports (PRD 6.11). Read-only; every figure comes from the ledger and records, nothing is
stored. ``start``/``end`` are inclusive dates; ``today`` is the Asia/Kolkata local date (an
argument, so tests control the clock)."""

from collections import Counter, defaultdict
from fractions import Fraction

from django.db.models import Count, Q, Sum

from catalogue.models import BookCopy, BookEdition, CopyEvent
from circulation.models import Loan, LoanRenewal
from finance import allocation
from finance.models import FinancialTransaction, PaymentEntry, TokenLedgerEntry
from members.models import Member
from memberships import policy
from memberships.models import MemberSubscription

from .core import (
    DATE,
    DATETIME,
    INT,
    MONEY,
    NUMBER,
    PERCENT,
    ROUNDING_NOTE,
    Column,
    Report,
    Section,
)
from .masking import mask

K = FinancialTransaction.Kind
LABEL = {k: v for k, v in K.choices}


def _who(member):
    return f"{member.membership_no} {member.full_name}"


def _period_text(start, end):
    return f"{start:%d %b %Y} to {end:%d %b %Y}"


# ------------------------------------------------------------------ 1. members and subscriptions
def members_subscriptions(start, end, today):
    subs = (
        MemberSubscription.objects.filter(starts_on__lte=end, expires_on__gt=start)
        .select_related("member")
        .order_by("member__membership_no", "starts_on")
    )
    rows = []
    for s in subs:
        state = "Cancelled" if s.cancelled_at else policy.state(s.period, today).capitalize()
        rows.append(
            [
                s.member.membership_no,
                s.member.full_name,
                s.member.mobile_e164,
                s.member.get_status_display(),
                s.plan_name_snapshot,
                s.starts_on,
                s.expires_on,
                policy.last_valid_day(s.expires_on),
                state,
                s.price_paise_snapshot,
            ]
        )
    section = Section(
        "Subscriptions valid on any day of the period",
        [
            Column("Member ID"),
            Column("Name"),
            Column("Mobile"),
            Column("Member status"),
            Column("Plan"),
            Column("Starts", DATE),
            Column("Expires (first expired day)", DATE),
            Column("Last valid day", DATE),
            Column("State today"),
            Column("Price", MONEY),
        ],
        rows,
        note="Cancelled subscriptions are listed; their fee is never refunded.",
    )
    counts = Member.objects.aggregate(
        total=Count("id"),
        active=Count("id", filter=Q(status="active", anonymised_at__isnull=True)),
        suspended=Count("id", filter=Q(status="suspended")),
        archived=Count("id", filter=Q(status="archived")),
    )
    summary = Section(
        "Members today",
        [Column("Measure"), Column("Count", INT)],
        [
            ["All members", counts["total"]],
            ["Active", counts["active"]],
            ["Suspended", counts["suspended"]],
            ["Archived", counts["archived"]],
        ],
    )
    return Report(
        "members",
        "Members and subscriptions",
        _period_text(start, end),
        [summary, section],
        ["Anonymised members appear as 'Anonymised member' with their member ID."],
    )


# ------------------------------------------------------------------ 2. collections (payment date)
def collections(start, end, today):
    entries = (
        PaymentEntry.objects.filter(paid_on__gte=start, paid_on__lte=end)
        .select_related("transaction__member")
        .order_by("paid_on", "id")
    )
    rows, totals = [], defaultdict(int)
    for e in entries:
        t = e.transaction
        sign = 1 if t.cash_flow == "in" else -1
        totals[t.kind] += sign * e.amount_paise
        rows.append(
            [
                e.paid_on,
                t.receipt_no,
                _who(t.member),
                LABEL[t.kind],
                e.method.upper(),
                mask(t.member.anonymised_at, e.reference),
                sign * e.amount_paise,
            ]
        )
    lines = Section(
        "Payment entries by their own payment date (money out is negative)",
        [
            Column("Paid on", DATE),
            Column("Receipt"),
            Column("Member"),
            Column("Kind"),
            Column("Method"),
            Column("Reference"),
            Column("Amount", MONEY),
        ],
        rows,
    ).with_totals(6)
    forfeited = (
        FinancialTransaction.objects.filter(
            kind=K.TOKEN_FORFEITURE, effective_date__gte=start, effective_date__lte=end
        ).aggregate(t=Sum("amount_paise"))["t"]
        or 0
    )
    summary = Section(
        "Totals (tokens are never subscription revenue)",
        [Column("Measure"), Column("Amount", MONEY)],
        [
            ["Subscription fees collected", totals[K.SUBSCRIPTION_PAYMENT]],
            ["Tokens collected", totals[K.TOKEN_COLLECTION]],
            ["Tokens refunded", totals[K.TOKEN_REFUND]],
            ["Tokens forfeited (no cash moves; by effective date)", forfeited],
            ["Adjustments (net cash)", totals[K.ADJUSTMENT]],
            ["Reversals (net cash)", totals[K.REVERSAL]],
        ],
    )
    return Report(
        "collections",
        "Subscription and payment collections (by payment date)",
        _period_text(start, end),
        [summary, lines],
        [ROUNDING_NOTE],
    )


# ------------------------------------------------------------------ 3. revenue allocation
def revenue_allocation(start, end, today):
    subs = (
        MemberSubscription.objects.filter(starts_on__lte=end, expires_on__gt=start)
        .select_related("member")
        .order_by("member__membership_no", "starts_on")
    )
    rows = []
    for s in subs:
        part = allocation.allocate(s.price_paise_snapshot, s.starts_on, s.expires_on, start, end)
        if part == 0:
            continue
        valid_days = allocation.period_days(s.starts_on, s.expires_on)
        rows.append(
            [
                s.member.membership_no,
                s.plan_name_snapshot,
                s.starts_on,
                s.expires_on,
                valid_days,
                s.price_paise_snapshot,
                part,
                "Cancelled (fee kept)" if s.cancelled_at else "",
            ]
        )
    section = Section(
        "Subscription fees allocated to the days of the period (daily pro-rata)",
        [
            Column("Member ID"),
            Column("Plan"),
            Column("Starts", DATE),
            Column("Expires", DATE),
            Column("Valid days", INT),
            Column("Fee", MONEY),
            Column("Allocated to period", MONEY),
            Column("Note"),
        ],
        rows,
    ).with_totals(5, 6)
    return Report(
        "allocation",
        "Subscription revenue allocation (management view)",
        _period_text(start, end),
        [section],
        [
            "Each fee is spread evenly over the subscription's valid days and the part falling in "
            "the period is shown. This is a management view, not a statutory report.",
            "Token collections are not revenue and are excluded. Cancelled subscriptions stay in "
            "the allocation because fees are not refundable.",
            ROUNDING_NOTE,
        ],
    )


# ------------------------------------------------------------------ 4. tokens
def tokens(start, end, today):
    qs = (
        FinancialTransaction.objects.filter(
            kind__in=[K.TOKEN_COLLECTION, K.TOKEN_REFUND, K.TOKEN_FORFEITURE],
            effective_date__gte=start,
            effective_date__lte=end,
        )
        .select_related("member")
        .order_by("effective_date", "id")
    )
    rows = [
        [
            t.effective_date,
            LABEL[t.kind],
            _who(t.member),
            t.receipt_no,
            t.amount_paise,
            "automatic (day 8)" if t.recorded_by_system else "owner",
        ]
        for t in qs
    ]
    section = Section(
        "Token collections, refunds and forfeitures",
        [
            Column("Effective date", DATE),
            Column("Kind"),
            Column("Member"),
            Column("Receipt"),
            Column("Amount", MONEY),
            Column("Recorded by"),
        ],
        rows,
    )
    sums = defaultdict(int)
    for t in qs:
        sums[t.kind] += t.amount_paise
    summary = Section(
        "Totals",
        [Column("Measure"), Column("Amount", MONEY)],
        [
            ["Collected", sums[K.TOKEN_COLLECTION]],
            ["Refunded", sums[K.TOKEN_REFUND]],
            ["Forfeited", sums[K.TOKEN_FORFEITURE]],
        ],
    )
    held = TokenLedgerEntry.objects.aggregate(t=Sum("delta_paise"))["t"] or 0
    return Report(
        "tokens",
        "Token collections, refunds and forfeitures",
        _period_text(start, end),
        [summary, section],
        [
            f"Token balance held for members today (all cycles): Rs {held / 100:,.2f}. "
            "Tokens are not revenue."
        ],
    )


# ------------------------------------------------------------------ 5. adjustments (D-7)
def adjustments(start, end, today):
    qs = (
        FinancialTransaction.objects.filter(
            kind=K.ADJUSTMENT, effective_date__gte=start, effective_date__lte=end
        )
        .select_related("member", "recorded_by", "adjusts")
        .order_by("effective_date", "id")
    )

    def row(t):
        return [
            t.effective_date,
            t.receipt_no,
            _who(t.member),
            t.get_cash_flow_display(),
            t.amount_paise,
            t.adjusts.receipt_no if t.adjusts_id else "",
            mask(t.member.anonymised_at, t.reason),
            t.recorded_by.get_username() if t.recorded_by_id else "",
            t.processed_at,
        ]

    columns = [
        Column("Effective date", DATE),
        Column("Receipt"),
        Column("Member"),
        Column("Cash flow"),
        Column("Amount", MONEY),
        Column("Linked to"),
        Column("Reason"),
        Column("Owner"),
        Column("Recorded at", DATETIME),
    ]
    unlinked_out = [t for t in qs if t.cash_flow == "out" and not t.adjusts_id]
    others = [t for t in qs if not (t.cash_flow == "out" and not t.adjusts_id)]
    return Report(
        "adjustments",
        "Financial adjustments",
        _period_text(start, end),
        [
            Section(
                "Unlinked cash-out adjustments: owner-approved accounting corrections, "
                "NOT subscription-fee refunds",
                columns,
                [row(t) for t in unlinked_out],
                note="Each was confirmed by the owner with a reason; fees are never refundable.",
            ).with_totals(4),
            Section("All other adjustments", columns, [row(t) for t in others]).with_totals(4),
        ],
        [
            "Decision D-7: unlinked cash-out adjustments are always listed apart from other "
            "adjustments."
        ],
    )


# ------------------------------------------------------------------ 6. outstanding and overdue
def loans_outstanding(start, end, today):
    asof = min(end, today)
    qs = (
        Loan.objects.filter(issued_on__lte=asof)
        .filter(Q(returned_on__isnull=True) | Q(returned_on__gt=asof))
        .select_related("member", "copy__edition")
        .order_by("due_on", "id")
    )
    rows = []
    for loan in qs:
        late = (asof - loan.due_on).days
        rows.append(
            [
                _who(loan.member),
                loan.copy.copy_code,
                loan.copy.edition.title,
                loan.issued_on,
                loan.due_on,
                max(late, 0),
                "Overdue" if late > 0 else "On loan",
            ]
        )
    return Report(
        "loans",
        "Outstanding and overdue loans",
        f"as at {asof:%d %b %Y}",
        [
            Section(
                "Loans out on that date",
                [
                    Column("Member"),
                    Column("Copy"),
                    Column("Title"),
                    Column("Issued", DATE),
                    Column("Due", DATE),
                    Column("Days overdue", INT),
                    Column("State"),
                ],
                rows,
            )
        ],
        ["The report date is the end of the chosen period (not later than today)."],
    )


# ------------------------------------------------------------------ 7. lost and damaged
def lost_damaged(start, end, today):
    copies = (
        BookCopy.objects.filter(Q(status="lost") | Q(condition__in=["damaged", "poor", "lost"]))
        .exclude(status="available", condition="good")
        .select_related("edition")
        .order_by("copy_code")
    )
    now_rows = [
        [c.copy_code, c.edition.title, c.get_status_display(), c.get_condition_display()]
        for c in copies
    ]
    closed = (
        Loan.objects.filter(returned_on__gte=start, returned_on__lte=end)
        .filter(Q(status="lost") | Q(return_condition__in=["damaged", "poor"]))
        .select_related("member", "copy__edition")
        .order_by("returned_on", "id")
    )
    event_rows = [
        [
            loan.returned_on,
            loan.copy.copy_code,
            loan.copy.edition.title,
            _who(loan.member),
            "Lost" if loan.status == "lost" else loan.return_condition.capitalize(),
            "yes" if loan.forfeiture_entry_id else "no",
        ]
        for loan in closed
    ]
    return Report(
        "lost_damaged",
        "Lost and damaged copies",
        _period_text(start, end),
        [
            Section(
                "Copies currently lost, damaged, poor or withdrawn",
                [Column("Copy"), Column("Title"), Column("Status"), Column("Condition")],
                now_rows,
            ),
            Section(
                "Loans closed as lost, damaged or poor in the period",
                [
                    Column("Date", DATE),
                    Column("Copy"),
                    Column("Title"),
                    Column("Member"),
                    Column("Outcome"),
                    Column("Token forfeited"),
                ],
                event_rows,
            ),
        ],
    )


# ------------------------------------------------------------------ 8. inventory and circulation
def inventory_circulation(start, end, today):
    by_status = Counter(BookCopy.objects.values_list("status", flat=True))
    by_condition = Counter(BookCopy.objects.values_list("condition", flat=True))
    editions = BookEdition.objects.filter(is_archived=False).count()
    status_labels = dict(BookCopy.Status.choices)
    condition_labels = dict(BookCopy.Condition.choices)
    inventory = Section(
        "Inventory today (editions and physical copies are separate)",
        [Column("Measure"), Column("Count", INT)],
        [["Editions", editions], ["Physical copies", sum(by_status.values())]]
        + [[f"Copies {status_labels[k].lower()}", by_status.get(k, 0)] for k in status_labels]
        + [
            [f"Condition {condition_labels[k].lower()}", by_condition.get(k, 0)]
            for k in condition_labels
        ],
    )
    issued = Loan.objects.filter(issued_on__gte=start, issued_on__lte=end).count()
    returned = Loan.objects.filter(
        returned_on__gte=start, returned_on__lte=end, status="returned"
    ).count()
    renewals = LoanRenewal.objects.filter(renewed_on__gte=start, renewed_on__lte=end).count()
    moves = CopyEvent.objects.filter(event_date__gte=start, event_date__lte=end).count()
    circulation = Section(
        "Circulation in the period",
        [Column("Measure"), Column("Count", INT)],
        [
            ["Loans issued", issued],
            ["Books returned", returned],
            ["Renewals", renewals],
            ["Copy status or condition changes", moves],
        ],
    )
    return Report(
        "inventory",
        "Inventory and circulation",
        _period_text(start, end),
        [inventory, circulation],
    )


# ------------------------------------------------------------------ 9. borrowing statistics
def borrowing_stats(start, end, today):
    loans = list(
        Loan.objects.filter(issued_on__gte=start, issued_on__lte=end).select_related(
            "copy__edition__category"
        )
    )
    months = defaultdict(lambda: [0, 0])
    for loan in loans:
        months[(loan.issued_on.year, loan.issued_on.month)][0] += 1
    for loan in Loan.objects.filter(
        returned_on__gte=start, returned_on__lte=end, status="returned"
    ):
        months[(loan.returned_on.year, loan.returned_on.month)][1] += 1
    month_rows = [[f"{y}-{m:02d}", a, b] for (y, m), (a, b) in sorted(months.items())]
    returned = [ln for ln in loans if ln.status == "returned" and ln.returned_on]
    kept = [(ln.returned_on - ln.issued_on).days for ln in returned]
    average = Fraction(sum(kept), len(kept)) if kept else Fraction(0)
    late = sum(1 for ln in returned if ln.returned_on > ln.due_on)
    rate = Fraction(late * 100, len(returned)) if returned else Fraction(0)
    titles = Counter(ln.copy.edition.title for ln in loans).most_common(10)
    categories = Counter(ln.copy.edition.category.name for ln in loans).most_common(10)
    return Report(
        "borrowing",
        "Borrowing statistics",
        _period_text(start, end),
        [
            Section(
                "Loans per month",
                [Column("Month"), Column("Issued", INT), Column("Returned", INT)],
                month_rows,
            ),
            Section(
                "Behaviour (loans issued in the period and already returned)",
                [Column("Measure"), Column("Value", INT)],
                [["Loans returned", len(returned)], ["Returned after the due date", late]],
            ),
            Section(
                "Average days a book was kept",
                [Column("Measure"), Column("Days", NUMBER)],
                [["Average days kept", average]],
            ),
            Section(
                "Overdue rate",
                [Column("Measure"), Column("Rate", PERCENT)],
                [["Returned after the due date", rate]],
            ),
            Section(
                "Top 10 titles", [Column("Title"), Column("Loans", INT)], [list(t) for t in titles]
            ),
            Section(
                "Top 10 categories",
                [Column("Category"), Column("Loans", INT)],
                [list(c) for c in categories],
            ),
        ],
        [
            "Overdue rate = loans returned after their due date / loans returned, among loans "
            "issued in the period."
        ],
    )


REPORTS = {
    "members": ("Members and subscriptions", members_subscriptions),
    "collections": ("Collections (payment date)", collections),
    "allocation": ("Revenue allocation", revenue_allocation),
    "tokens": ("Tokens: collections, refunds, forfeitures", tokens),
    "adjustments": ("Financial adjustments", adjustments),
    "loans": ("Outstanding and overdue loans", loans_outstanding),
    "lost_damaged": ("Lost and damaged copies", lost_damaged),
    "inventory": ("Inventory and circulation", inventory_circulation),
    "borrowing": ("Borrowing statistics", borrowing_stats),
}
PDF_REPORTS = {"collections", "allocation"}  # decision D-28b (plus the member statement)


def build(key, start, end, today):
    return REPORTS[key][1](start, end, today)
