from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from core.errors import RuleViolation
from members.models import Member

from . import services
from .forms import CancelForm, FreshTokenForm, RegistrationForm, RenewalForm
from .models import MemberSubscription


def _go_to_receipt(request, result, text):
    messages.success(request, text)
    if result.document is not None:
        return redirect("finance:receipt", pk=result.document.pk)
    return redirect("members:detail", pk=result.member.pk)


@login_required
def register(request):
    form = RegistrationForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        paid_on = d.get("payment_date")
        try:
            result = services.register_member(
                actor=request.user,
                member_fields=form.member_fields(),
                plan_code=d["plan"],
                subscription_entries=form.entries("sub", paid_on),
                token_entries=form.entries("tok", paid_on),
                activation_date=d.get("activation_date"),
                payment_date=paid_on,
                backdate_reason=d.get("backdate_reason", ""),
                backdate_confirmed=d.get("backdate_confirmed", False),
                request_key=d["request_key"],
            )
        except RuleViolation as exc:
            form.add_error(None, exc.message)
        else:
            return _go_to_receipt(
                request, result, f"Member {result.member.membership_no} registered and activated."
            )
    return render(
        request,
        "memberships/form.html",
        {"form": form, "title": "Register a member", "nav_active": "register", "register": True},
    )


@login_required
def renew(request, pk):
    member = get_object_or_404(Member, pk=pk)
    form = RenewalForm(request.POST or None)
    today = timezone.localdate()
    blockers = services.renewal_blockers(member, today)
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        paid_on = d.get("payment_date")
        try:
            result = services.renew_subscription(
                actor=request.user,
                member=member,
                plan_code=d["plan"],
                entries=form.entries("sub", paid_on),
                activation_date=d.get("activation_date"),
                payment_date=paid_on,
                backdate_reason=d.get("backdate_reason", ""),
                backdate_confirmed=d.get("backdate_confirmed", False),
                request_key=d["request_key"],
            )
        except RuleViolation as exc:
            form.add_error(None, exc.message)
        else:
            return _go_to_receipt(request, result, "Subscription renewed.")
    return render(
        request,
        "memberships/form.html",
        {
            "form": form,
            "title": f"Renew {member.membership_no}",
            "nav_active": "members",
            "member": member,
            "blockers": blockers,
        },
    )


@login_required
def fresh_token(request, pk):
    member = get_object_or_404(Member, pk=pk)
    form = FreshTokenForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        paid_on = d.get("payment_date")
        try:
            result = services.pay_fresh_token(
                actor=request.user,
                member=member,
                entries=form.entries("tok", paid_on),
                payment_date=paid_on,
                backdate_reason=d.get("backdate_reason", ""),
                backdate_confirmed=d.get("backdate_confirmed", False),
                request_key=d["request_key"],
            )
        except RuleViolation as exc:
            form.add_error(None, exc.message)
        else:
            return _go_to_receipt(
                request, result, "Token collected. A new token cycle has started."
            )
    return render(
        request,
        "memberships/form.html",
        {
            "form": form,
            "title": f"Collect a token — {member.membership_no}",
            "nav_active": "members",
            "member": member,
        },
    )


@login_required
def cancel(request, pk):
    sub = get_object_or_404(MemberSubscription, pk=pk)
    form = CancelForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            with transaction.atomic():
                services.cancel_subscription(
                    actor=request.user, subscription=sub, reason=form.cleaned_data["reason"]
                )
        except RuleViolation as exc:
            form.add_error(None, exc.message)
        else:
            messages.success(request, "Subscription cancelled. No refund or credit is given.")
            return redirect("members:detail", pk=sub.member_id)
    return render(
        request,
        "memberships/form.html",
        {
            "form": form,
            "title": f"Cancel subscription — {sub.member.membership_no}",
            "nav_active": "members",
            "member": sub.member,
        },
    )
