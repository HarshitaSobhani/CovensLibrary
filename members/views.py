from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from core.errors import RuleViolation
from core.http import clean_query, querystring

from . import services
from .forms import AnonymiseForm, MemberForm, ReactivateForm
from .models import Member


@login_required
def member_list(request):
    q = clean_query(request.GET.get("q", ""))
    status = request.GET.get("status", "")
    page = Paginator(services.search_members(q, status), 25).get_page(request.GET.get("page"))
    return render(
        request,
        "members/list.html",
        {
            "page": page,
            "qs": querystring(q=q, status=status if status in Member.Status.values else ""),
            "q": q,
            "status": status,
            "statuses": Member.Status.choices,
            "nav_active": "members",
        },
    )


@login_required
def member_create(request):
    form = MemberForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            member = services.create_member(actor=request.user, **form.cleaned_data)
        except RuleViolation as exc:
            form.add_error(None, exc.message)
        else:
            messages.success(request, f"Member {member.membership_no} created.")
            return redirect("members:detail", pk=member.pk)
    return render(
        request, "members/form.html", {"form": form, "nav_active": "members", "title": "Add member"}
    )


@login_required
def member_detail(request, pk):
    member = get_object_or_404(Member, pk=pk)
    return render(
        request,
        "members/detail.html",
        {"member": member, "nav_active": "members", "reactivate_form": ReactivateForm()},
    )


@login_required
def member_edit(request, pk):
    member = get_object_or_404(Member, pk=pk)
    if member.anonymised_at:
        messages.error(request, "An anonymised member cannot be edited.")
        return redirect("members:detail", pk=pk)
    initial = {"full_name": member.full_name, "email": member.email, "notes": member.notes}
    form = MemberForm(request.POST or None, member=member, initial=initial)
    if request.method == "POST" and form.is_valid():
        try:
            services.update_member(member, actor=request.user, **form.cleaned_data)
        except RuleViolation as exc:
            form.add_error(None, exc.message)
        else:
            messages.success(request, "Member updated.")
            return redirect("members:detail", pk=pk)
    return render(
        request,
        "members/form.html",
        {"form": form, "nav_active": "members", "title": f"Edit {member.membership_no}"},
    )


@login_required
@require_POST
def member_action(request, pk, action):
    member = get_object_or_404(Member, pk=pk)
    try:
        if action == "suspend":
            services.suspend_member(member, actor=request.user)
        elif action == "archive":
            services.archive_member(member, actor=request.user)
        elif action == "reactivate":
            form = ReactivateForm(request.POST)
            if not form.is_valid():
                raise RuleViolation("reactivation_mode", "Choose restore or new subscription.")
            services.reactivate_member(member, actor=request.user, mode=form.cleaned_data["mode"])
        else:
            raise RuleViolation("unknown_action", "Unknown action.")
        messages.success(request, f"Member {action} done.")
    except RuleViolation as exc:
        messages.error(request, exc.message)
    return redirect("members:detail", pk=pk)


@login_required
def member_anonymise(request, pk):
    member = get_object_or_404(Member, pk=pk)
    form = AnonymiseForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            services.anonymise_member(member, actor=request.user, **form.cleaned_data)
        except RuleViolation as exc:
            form.add_error(None, exc.message)
        else:
            messages.success(request, "Member anonymised.")
            return redirect("members:detail", pk=pk)
    return render(
        request, "members/anonymise.html", {"member": member, "form": form, "nav_active": "members"}
    )
