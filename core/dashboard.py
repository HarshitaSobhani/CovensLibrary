"""Read-only data for the owner dashboard, calendar and global search (Phase 5a).

This module only READS other apps' tables (imports are inside the functions so ``core`` keeps no
import-time dependency on them). Nothing here writes, and every date comes from the ``today``
argument (Asia/Kolkata local date) so tests control the clock.
"""

import calendar as pycalendar
from collections import defaultdict
from datetime import date, timedelta

from django.db.models import Count, Q, Sum

from . import dashboard_config as cfg

LIST_LIMIT = 10


# ---------------------------------------------------------------- sections
def overdue_loans(today, grace_days, limit=LIST_LIMIT):
    from circulation.models import Loan
    from memberships import policy

    qs = Loan.objects.filter(status="active", due_on__lt=today).select_related(
        "member", "copy__edition"
    )
    rows = [
        {
            "loan": loan,
            "days": policy.days_overdue(loan.due_on, today),
            "forfeiture_day": policy.forfeiture_day(loan.due_on, grace_days),
        }
        for loan in qs.order_by("due_on", "id")[:limit]
    ]
    return {"count": qs.count(), "rows": rows}


def upcoming_expiries(today, window_days, limit=LIST_LIMIT):
    """Live subscriptions whose expiry date falls inside the window and that are not already
    continued by another subscription starting on that date (an early renewal)."""
    from memberships.models import MemberSubscription

    live = MemberSubscription.objects.filter(cancelled_at__isnull=True)
    qs = live.filter(
        starts_on__lte=today,
        expires_on__gt=today,
        expires_on__lte=today + timedelta(days=window_days),
        member__status="active",
        member__anonymised_at__isnull=True,
    ).select_related("member")
    rows = []
    for sub in qs.order_by("expires_on", "id"):
        if live.filter(
            member=sub.member, starts_on__lte=sub.expires_on, expires_on__gt=sub.expires_on
        ).exists():
            continue
        rows.append({"subscription": sub, "days": (sub.expires_on - today).days})
    return {"count": len(rows), "rows": rows[:limit]}


def decisions(today, limit=LIST_LIMIT):
    """Owner decisions waiting (grace-change forfeitures) and copies still lost or damaged."""
    from catalogue.models import BookCopy
    from circulation.models import PendingForfeiture

    pending = PendingForfeiture.objects.filter(status="pending").select_related(
        "loan__member", "loan__copy__edition"
    )
    cases = BookCopy.objects.filter(
        Q(status="lost") | Q(condition__in=["damaged", "poor"]) & ~Q(status="available")
    ).select_related("edition")
    return {
        "count": pending.count() + cases.count(),
        "pending": list(pending.order_by("created_at")[:limit]),
        "pending_count": pending.count(),
        "cases": list(cases.order_by("copy_code")[:limit]),
        "case_count": cases.count(),
    }


def recent_forfeitures(limit=LIST_LIMIT):
    from finance.models import FinancialTransaction

    qs = FinancialTransaction.objects.filter(kind="token_forfeiture").select_related("member")
    return {"count": qs.count(), "rows": list(qs.order_by("-processed_at", "-id")[:limit])}


def failed_emails(limit=LIST_LIMIT):
    from django.apps import apps

    if not apps.is_installed("communications"):
        return {"count": 0, "rows": [], "available": False}
    from communications.models import Notification

    qs = Notification.objects.filter(status__in=["smtp_failed", "failed"]).select_related("member")
    return {
        "count": qs.count(),
        "rows": list(qs.order_by("-updated_at")[:limit]),
        "available": True,
    }


def backup_failures():
    # Backups are built in Phase 7a; the section exists with an honest empty state.
    return {"count": 0, "rows": [], "available": False}


def counts(today):
    from catalogue.models import BookCopy, BookEdition
    from members.models import Member

    copies = BookCopy.objects.aggregate(
        total=Count("id"),
        available=Count("id", filter=Q(status="available")),
        borrowed=Count("id", filter=Q(status="borrowed")),
    )
    members = Member.objects.aggregate(
        total=Count("id"), active=Count("id", filter=Q(status="active", anonymised_at__isnull=True))
    )
    return {
        "editions": BookEdition.objects.filter(is_archived=False).count(),
        "copies": copies["total"],
        "available": copies["available"],
        "borrowed": copies["borrowed"],
        "members": members["total"],
        "active_members": members["active"],
    }


def monthly_collections(today):
    """Actual money received in the month by receipt kind. Tokens are shown apart from
    subscription fees and are never revenue. Reversed/adjusted rows are separate kinds."""
    from finance.models import FinancialTransaction

    first = today.replace(day=1)
    rows = (
        FinancialTransaction.objects.filter(effective_date__gte=first, effective_date__lte=today)
        .values("kind", "cash_flow")
        .annotate(total=Sum("amount_paise"), n=Count("id"))
        .order_by("kind", "cash_flow")
    )
    return {"month": first, "rows": list(rows)}


def recent_transactions(limit=LIST_LIMIT):
    from finance.models import FinancialTransaction

    return list(
        FinancialTransaction.objects.select_related("member").order_by("-processed_at", "-id")[
            :limit
        ]
    )


# ---------------------------------------------------------------- attention
def attention(today, criteria, grace_days=None):
    """Members requiring attention: {member: [reason labels]} for the ENABLED criteria only."""
    from catalogue.models import BookCopy
    from circulation.models import Loan, PendingForfeiture
    from finance import tokens
    from members.models import Member
    from memberships import services as ms

    reasons = defaultdict(list)
    members = {m.pk: m for m in Member.objects.filter(status="active", anonymised_at__isnull=True)}

    def add(member_id, key):
        if member_id in members and criteria.get(key):
            reasons[member_id].append(cfg.CRITERIA[key])

    overdue = set(
        Loan.objects.filter(status="active", due_on__lt=today).values_list("member_id", flat=True)
    )
    pending = set(
        PendingForfeiture.objects.filter(status="pending").values_list("loan__member_id", flat=True)
    )
    problem_loans = Loan.objects.filter(
        copy__in=BookCopy.objects.filter(
            Q(status="lost") | (Q(condition__in=["damaged", "poor"]) & ~Q(status="available"))
        )
    ).values_list("member_id", flat=True)
    problem = set(problem_loans)
    for member in members.values():
        subs = ms._live_subscriptions(member)
        valid_now = ms.current_subscription(member, today) is not None
        if subs and not valid_now and not ms.scheduled_subscription(member, today):
            add(member.pk, "expired")
        if tokens.token_state(member)[1] == tokens.FORFEITED:
            add(member.pk, "token_forfeited")
        if member.pk in overdue:
            add(member.pk, "overdue")
        if member.pk in problem:
            add(member.pk, "damaged_lost")
        if member.pk in pending:
            add(member.pk, "pending_decision")
        if not subs or member.needs_new_subscription:  # an email is optional, not a defect
            add(member.pk, "incomplete")
    ordered = sorted(reasons, key=lambda pk: members[pk].membership_no)
    return [{"member": members[pk], "reasons": reasons[pk]} for pk in ordered]


def build(setting, today, grace_days=None):
    """Everything the dashboard template needs, honouring the owner's configuration."""
    conf = cfg.load(setting)
    grace = setting.grace_days if grace_days is None else grace_days
    visible = conf["visible"]
    loaders = {
        "overdue": lambda: overdue_loans(today, grace),
        "expiries": lambda: upcoming_expiries(today, conf["expiry_window_days"]),
        "backup_failures": backup_failures,
        "failed_emails": failed_emails,
        "decisions": lambda: decisions(today),
        "forfeitures": recent_forfeitures,
        "attention": lambda: attention(today, conf["criteria"], grace),
        "counts": lambda: counts(today),
        "collections": lambda: monthly_collections(today),
        "transactions": recent_transactions,
    }
    sections = []
    for key in conf["order"]:
        if visible[key]:
            sections.append({"key": key, "title": cfg.SECTIONS[key], "data": loaders[key]()})
    return {"sections": sections, "config": conf}


# ---------------------------------------------------------------- calendar
def calendar_events(year, month, grace_days, reminder_days=(0, 3, 7), expiry_reminder_days=15):
    """Dated items in one month: expiries, due dates, reminder dates, day-8 forfeitures."""
    from circulation.models import Loan
    from memberships import policy
    from memberships.models import MemberSubscription

    first = date(year, month, 1)
    last = date(year, month, pycalendar.monthrange(year, month)[1])
    days = defaultdict(list)

    live = MemberSubscription.objects.filter(
        cancelled_at__isnull=True, member__anonymised_at__isnull=True
    )
    window = timedelta(days=expiry_reminder_days)
    for sub in live.select_related("member").filter(
        expires_on__gte=first, expires_on__lte=last + window
    ):
        if first <= sub.expires_on <= last:
            days[sub.expires_on].append(
                ("expiry", f"Subscription expires: {sub.member.full_name}", sub.member_id)
            )
        reminder = sub.expires_on - window
        if first <= reminder <= last:
            days[reminder].append(
                ("reminder", f"Expiry reminder: {sub.member.full_name}", sub.member_id)
            )
    loans = Loan.objects.filter(status="active", member__anonymised_at__isnull=True).select_related(
        "member", "copy__edition"
    )
    for loan in loans:
        title = loan.copy.edition.title
        if first <= loan.due_on <= last:
            days[loan.due_on].append(
                ("due", f"Due: {title} ({loan.member.full_name})", loan.member_id)
            )
        for n in reminder_days:
            day = loan.due_on + timedelta(days=n)
            if first <= day <= last:
                label = "Overdue reminder (due date)" if n == 0 else f"Overdue reminder (day {n})"
                days[day].append(("reminder", f"{label}: {loan.member.full_name}", loan.member_id))
        forfeit = policy.forfeiture_day(loan.due_on, grace_days)
        if first <= forfeit <= last:
            days[forfeit].append(
                ("forfeiture", f"Token forfeiture day: {loan.member.full_name}", loan.member_id)
            )
    return [(day, sorted(items)) for day, items in sorted(days.items())]


# ---------------------------------------------------------------- search
SEARCH_LIMIT = 15


def search(q):
    from catalogue.models import BookCopy, BookEdition
    from finance.models import FinancialTransaction
    from members.models import Member

    q = (q or "").strip()
    if len(q) < 2:
        return None
    digits = "".join(ch for ch in q if ch.isdigit() or ch == "+")
    members = Q(full_name__icontains=q) | Q(membership_no__icontains=q)
    if len(digits) >= 3:
        members |= Q(mobile_e164__contains=digits)
    return {
        "q": q,
        "members": list(Member.objects.filter(members, anonymised_at__isnull=True)[:SEARCH_LIMIT]),
        "editions": list(
            BookEdition.objects.filter(
                Q(title__icontains=q)
                | Q(author__icontains=q)
                | Q(isbn__icontains=q.replace("-", ""))
            )[:SEARCH_LIMIT]
        ),
        "copies": list(
            BookCopy.objects.filter(
                Q(copy_code__icontains=q) | Q(barcode__iexact=q)
            ).select_related("edition")[:SEARCH_LIMIT]
        ),
        "transactions": list(
            FinancialTransaction.objects.filter(receipt_no__icontains=q).select_related("member")[
                :SEARCH_LIMIT
            ]
        ),
    }
