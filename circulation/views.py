from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from core.errors import RuleViolation
from core.models import LibrarySetting
from memberships import policy

from . import grace, services
from .forms import DecisionForm, GraceForm, IssueForm, LostForm, RenewForm, ReturnForm
from .models import Loan, PendingForfeiture


def loan_row(loan, today, grace):
    return {
        "loan": loan,
        "overdue_days": policy.days_overdue(loan.due_on, today) if loan.status == "active" else 0,
        "grace_end": policy.grace_end(loan.due_on, grace),
        "forfeiture_day": policy.forfeiture_day(loan.due_on, grace),
    }


@login_required
def loan_list(request):
    today = timezone.localdate()
    grace = LibrarySetting.load().grace_days
    show = request.GET.get("show", "active")
    qs = Loan.objects.select_related("member", "copy__edition")
    if show == "overdue":
        qs = qs.filter(status="active", due_on__lt=today)
    elif show == "all":
        pass
    else:
        show = "active"
        qs = qs.filter(status="active")
    page = Paginator(qs.order_by("due_on", "id"), 25).get_page(request.GET.get("page"))
    rows = [loan_row(loan, today, grace) for loan in page]
    return render(
        request,
        "circulation/list.html",
        {
            "page": page,
            "rows": rows,
            "show": show,
            "today": today,
            "nav_active": "loans",
            "grace_days": grace,
        },
    )


@login_required
def loan_issue(request):
    form = IssueForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        try:
            loan = services.issue_loan(
                actor=request.user,
                member=form.member,
                copy=form.copy,
                issued_on=d.get("issued_on"),
                due_on=d.get("due_on"),
                backdate_reason=d.get("backdate_reason", ""),
                backdate_confirmed=d.get("backdate_confirmed", False),
                notes=d.get("notes", ""),
            )
        except RuleViolation as exc:
            form.add_error(None, exc.message)
        else:
            messages.success(request, f"Issued {loan.copy.copy_code}; due {loan.due_on:%d %b %Y}.")
            return redirect("circulation:detail", pk=loan.pk)
    return render(
        request,
        "circulation/form.html",
        {"form": form, "title": "Issue a book", "nav_active": "loans"},
    )


@login_required
def loan_detail(request, pk):
    loan = get_object_or_404(
        Loan.objects.select_related("member", "copy__edition", "forfeiture_entry"), pk=pk
    )
    today = timezone.localdate()
    grace = LibrarySetting.load().grace_days
    return render(
        request,
        "circulation/detail.html",
        {
            "loan": loan,
            "row": loan_row(loan, today, grace),
            "renewals": loan.renewals.all(),
            "active": loan.status == "active",
            "pending": loan.pending_forfeitures.all(),
            "nav_active": "loans",
        },
    )


@login_required
def loan_return(request, pk):
    loan = get_object_or_404(Loan, pk=pk)
    form = ReturnForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        try:
            services.return_loan(
                actor=request.user,
                loan=loan,
                returned_on=d.get("returned_on"),
                condition=d["condition"],
                owner_confirmed=d.get("owner_confirmed", False),
                reason=d.get("reason", ""),
                backdate_reason=d.get("backdate_reason", ""),
                backdate_confirmed=d.get("backdate_confirmed", False),
                notes=d.get("notes", ""),
            )
        except RuleViolation as exc:
            form.add_error(None, exc.message)
        else:
            messages.success(request, "Book returned.")
            return redirect("circulation:detail", pk=pk)
    return render(
        request,
        "circulation/form.html",
        {"form": form, "title": f"Return - loan {loan.pk}", "nav_active": "loans", "loan": loan},
    )


@login_required
def loan_lost(request, pk):
    loan = get_object_or_404(Loan, pk=pk)
    form = LostForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        try:
            services.mark_lost(
                actor=request.user,
                loan=loan,
                lost_on=d.get("lost_on"),
                owner_confirmed=d.get("owner_confirmed", False),
                reason=d.get("reason", ""),
                backdate_reason=d.get("backdate_reason", ""),
                backdate_confirmed=d.get("backdate_confirmed", False),
                notes=d.get("notes", ""),
            )
        except RuleViolation as exc:
            form.add_error(None, exc.message)
        else:
            messages.success(request, "Marked as lost; the token was forfeited.")
            return redirect("circulation:detail", pk=pk)
    return render(
        request,
        "circulation/form.html",
        {"form": form, "title": f"Mark lost - loan {loan.pk}", "nav_active": "loans", "loan": loan},
    )


@login_required
def loan_renew(request, pk):
    loan = get_object_or_404(Loan, pk=pk)
    form = RenewForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            renewal = services.renew_loan(
                actor=request.user, loan=loan, notes=form.cleaned_data.get("notes", "")
            )
        except RuleViolation as exc:
            form.add_error(None, exc.message)
        else:
            messages.success(request, f"Renewed; now due {renewal.new_due_on:%d %b %Y}.")
            return redirect("circulation:detail", pk=pk)
    return render(
        request,
        "circulation/form.html",
        {"form": form, "title": f"Renew - loan {loan.pk}", "nav_active": "loans", "loan": loan},
    )


@login_required
def grace_settings(request):
    setting = LibrarySetting.load()
    form = GraceForm(request.POST or None, initial={"grace_days": setting.grace_days})
    if request.method == "POST" and form.is_valid():
        try:
            event = grace.change_grace_days(
                actor=request.user, new_days=form.cleaned_data["grace_days"]
            )
        except RuleViolation as exc:
            form.add_error(None, exc.message)
        else:
            count = len(event.affected_loans)
            messages.success(
                request,
                f"Grace period is now {event.new_value} days."
                + (f" {count} loan(s) need your decision." if count else ""),
            )
            return redirect("circulation:decisions" if count else "circulation:grace")
    return render(
        request,
        "circulation/grace.html",
        {
            "form": form,
            "grace_days": setting.grace_days,
            "history": grace.SettingChangeEvent.objects.filter(setting_name="grace_days").order_by(
                "-id"
            )[:20],
            "nav_active": "loans",
        },
    )


@login_required
def decisions(request):
    return render(
        request,
        "circulation/decisions.html",
        {
            "pending": grace.pending_decisions(),
            "resolved": PendingForfeiture.objects.exclude(status="pending")
            .select_related("loan__member", "loan__copy")
            .order_by("-resolved_at")[:20],
            "confirm_form": DecisionForm(),
            "nav_active": "loans",
        },
    )


@login_required
def decision_resolve(request, pk, action):
    pending = get_object_or_404(PendingForfeiture, pk=pk)
    if request.method != "POST":
        return redirect("circulation:decisions")
    form = DecisionForm(request.POST)
    if not form.is_valid():
        messages.error(request, "A reason is required.")
        return redirect("circulation:decisions")
    try:
        if action == "confirm":
            grace.confirm_pending(
                actor=request.user, pending=pending, reason=form.cleaned_data["reason"]
            )
            messages.success(request, "Forfeiture confirmed.")
        else:
            grace.decline_pending(
                actor=request.user, pending=pending, reason=form.cleaned_data["reason"]
            )
            messages.success(request, "Forfeiture declined. You will not be asked again.")
    except RuleViolation as exc:
        messages.error(request, exc.message)
    return redirect("circulation:decisions")
