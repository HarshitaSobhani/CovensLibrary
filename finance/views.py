from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render

from core.errors import RuleViolation
from members.models import Member

from . import corrections, tokens
from .forms import AdjustmentForm, RefundForm, ReverseForm, to_paise
from .models import FinancialTransaction, ReceiptDocument
from .receipts import render_pdf


@login_required
def receipt(request, pk):
    document = get_object_or_404(ReceiptDocument.objects.select_related("member"), pk=pk)
    return render(
        request,
        "finance/receipt.html",
        {"document": document, "snap": document.snapshot, "nav_active": "members"},
    )


@login_required
def receipt_pdf(request, pk):
    document = get_object_or_404(ReceiptDocument.objects.select_related("member"), pk=pk)
    response = HttpResponse(
        render_pdf(document, document.member.full_name), content_type="application/pdf"
    )
    response["Content-Disposition"] = f'inline; filename="receipt-{document.pk}.pdf"'
    response["X-Content-Type-Options"] = "nosniff"
    return response


def _entries(form, prefix="pay"):
    return form.entries(prefix)


@login_required
def refund(request, pk):
    member = get_object_or_404(Member, pk=pk)
    form = RefundForm(request.POST or None)
    cycle, state = tokens.token_state(member)
    if request.method == "POST" and form.is_valid():
        try:
            posted = corrections.refund_token(
                actor=request.user,
                member=member,
                entries=_entries(form),
                request_key=form.cleaned_data["request_key"],
            )
        except RuleViolation as exc:
            form.add_error(None, exc.message)
        else:
            messages.success(request, "Token refunded in full.")
            return redirect("finance:receipt", pk=posted.document.pk)
    return render(
        request,
        "finance/form.html",
        {
            "form": form,
            "member": member,
            "title": f"Refund token — {member.membership_no}",
            "intro": "The full token is refunded; partial refunds do not exist. "
            f"Amount due: ₹{(cycle.amount_snapshot_paise / 100) if cycle else 0:,.2f}.",
            "nav_active": "members",
        },
    )


@login_required
def reverse(request, pk):
    txn = get_object_or_404(FinancialTransaction.objects.select_related("member"), pk=pk)
    form = ReverseForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            corrections.reverse_transaction(
                actor=request.user,
                transaction_id=txn.pk,
                reason=form.cleaned_data["reason"],
                entries=_entries(form),
                request_key=form.cleaned_data["request_key"],
            )
        except RuleViolation as exc:
            form.add_error(None, exc.message)
        else:
            messages.success(request, "Reversal recorded. The original record is unchanged.")
            return redirect("members:detail", pk=txn.member_id)
    return render(
        request,
        "finance/form.html",
        {
            "form": form,
            "member": txn.member,
            "title": f"Reverse {txn.receipt_no or txn.get_kind_display()}",
            "intro": "A reversal is a new record linked to the original; the original is never "
            "edited. Subscription fees cannot be reversed or refunded. For a cash reversal, "
            "enter how the money moved back.",
            "nav_active": "members",
        },
    )


@login_required
def adjust(request, pk):
    member = get_object_or_404(Member, pk=pk)
    form = AdjustmentForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        try:
            posted = corrections.post_adjustment(
                actor=request.user,
                member=member,
                direction=d["direction"],
                amount_paise=to_paise(d["amount"]),
                reason=d["reason"],
                owner_approved=d["approved"],
                confirm_not_fee_refund=d["confirm_not_fee_refund"],
                entries=_entries(form),
                request_key=d["request_key"],
            )
        except RuleViolation as exc:
            form.add_error(None, exc.message)
        else:
            messages.success(request, "Adjustment recorded.")
            return redirect("finance:receipt", pk=posted.document.pk)
    return render(
        request,
        "finance/form.html",
        {
            "form": form,
            "member": member,
            "title": f"Financial adjustment — {member.membership_no}",
            "intro": "Owner-approved correction with a mandatory reason. Cash and UPI amounts "
            "must add up to the adjustment amount.",
            "nav_active": "members",
        },
    )
