"""Payments & Tokens: a read-only monitoring workspace for the owner. It posts nothing; refunds,
adjustments, reversals, registration and renewal stay where they are (member profile)."""

from datetime import date

from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.views.decorators.http import require_safe

from core.http import clean_query, int_param, querystring
from members.models import Member
from memberships.templatetags.membership_panel import REVERSIBLE
from reports import periods
from reports.masking import mask

from . import workspace as ws
from .models import FinancialTransaction


def _period(request, default="year"):
    today = timezone.localdate()
    preset = request.GET.get("preset", default)
    error = ""
    try:
        start, end, label = periods.resolve(
            preset, today, request.GET.get("start"), request.GET.get("end")
        )
    except periods.PeriodError as exc:
        error = str(exc)
        preset = default
        start, end, label = periods.resolve(default, today)
    return today, preset, start, end, label, error


@login_required
@require_safe
def payments(request):
    """Overview cards and the searchable transaction ledger for a period."""
    today, preset, start, end, label, error = _period(request)
    member_id, member_ok = int_param(request, "member")
    member = Member.objects.filter(pk=member_id).first() if member_id else None
    q = clean_query(request.GET.get("q", ""), 100)
    kind = request.GET.get("kind", "")
    mode = request.GET.get("mode", "")
    receipt = clean_query(request.GET.get("receipt", ""), 40)
    kind = kind if kind in ws.KIND_LABEL else ""
    mode = mode if mode in dict(ws.MODE_CHOICES) else ""
    flt = ws.LedgerFilter(
        start=start,
        end=end,
        member_id=member.pk if member else None,
        member_q=q,
        kind=kind,
        mode=mode,
        receipt=receipt,
    )
    page = Paginator(ws.ledger_queryset(flt), ws.PAGE_SIZE).get_page(request.GET.get("page"))
    page.object_list = [ws.ledger_row(t) for t in page.object_list]
    qs = querystring(
        preset=preset,
        start=start.isoformat() if preset == "custom" else "",
        end=end.isoformat() if preset == "custom" else "",
        member=member.pk if member else "",
        q=q,
        kind=kind,
        mode=mode,
        receipt=receipt,
    )
    return render(
        request,
        "finance/workspace/payments.html",
        {
            "nav_active": "payments",
            "tab": "ledger",
            "cards": ws.overview(start, end),
            "page": page,
            "qs": qs,
            "presets": periods.PRESETS,
            "preset": preset,
            "start": start,
            "end": end,
            "label": label,
            "error": error or ("" if member_ok else "The member filter is not valid."),
            "member": member,
            "q": q,
            "kind": kind,
            "mode": mode,
            "receipt": receipt,
            "kinds": list(ws.KIND_LABEL.items()),
            "modes": ws.MODE_CHOICES,
            "today": today,
        },
    )


def _optional_date(raw):
    try:
        return date.fromisoformat(raw) if raw else None
    except ValueError:
        return None


@login_required
@require_safe
def token_activity(request):
    """Token cycles (held, refunded, forfeited) and the members who need a fresh token."""
    today = timezone.localdate()
    status = request.GET.get("status", "")
    status = status if status in ws.TOKEN_STATUS_LABEL else ""
    raw_start, raw_end = request.GET.get("start", ""), request.GET.get("end", "")
    start, end = _optional_date(raw_start), _optional_date(raw_end)
    error = ""
    if (raw_start and start is None) or (raw_end and end is None):
        error = "Enter the dates as YYYY-MM-DD."
        start = end = None
    elif start and end and start > end:
        error = "The start date must not be after the end date."
        start = end = None
    rows = ws.token_rows(ws.TokenFilter(status=status, start=start, end=end))
    page = Paginator(rows, ws.PAGE_SIZE).get_page(request.GET.get("page"))
    return render(
        request,
        "finance/workspace/tokens.html",
        {
            "nav_active": "payments",
            "tab": "tokens",
            "page": page,
            "totals": [(ws.TOKEN_STATUS_LABEL[k], v) for k, v in ws.token_totals(rows).items()],
            "qs": querystring(
                status=status,
                start=start.isoformat() if start else "",
                end=end.isoformat() if end else "",
            ),
            "status": status,
            "statuses": list(ws.TOKEN_STATUS_LABEL.items()),
            "start": start,
            "end": end,
            "error": error,
            "needs_token": ws.needs_fresh_token(today),
        },
    )


@login_required
@require_safe
def transaction_detail(request, pk):
    txn = get_object_or_404(
        FinancialTransaction.objects.select_related(
            "member", "recorded_by", "receipt_document", "reverses", "adjusts"
        ).prefetch_related("entries"),
        pk=pk,
    )
    row = ws.ledger_row(txn)
    anonymised = txn.member.anonymised_at
    return render(
        request,
        "finance/workspace/detail.html",
        {
            "nav_active": "payments",
            "tab": "ledger",
            "txn": txn,
            "row": row,
            "entries": [
                {"entry": e, "reference": mask(anonymised, e.reference)} for e in txn.entries.all()
            ],
            "links": ws.related(txn),
            "audit": ws.audit_history(txn),
            "reason": mask(anonymised, txn.reason),
            "backdate_reason": mask(anonymised, txn.backdate_reason),
            "can_reverse": txn.kind in REVERSIBLE and not hasattr(txn, "reversed_by"),
        },
    )
