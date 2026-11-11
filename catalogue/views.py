from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from core.errors import RuleViolation

from . import services
from .forms import CategoryForm, CopiesForm, CopyStateForm, EditionForm, LabelLayoutForm
from .labels import default_layout, render_labels_pdf
from .models import BookCopy, BookEdition, Category, LabelLayout

NAV = {"nav_active": "books"}


@login_required
def edition_list(request):
    q = request.GET.get("q", "")
    archived = request.GET.get("archived") == "1"
    category = Category.objects.filter(pk=request.GET.get("category") or 0).first()
    page = Paginator(services.search_editions(q, archived, category), 25).get_page(
        request.GET.get("page")
    )
    return render(
        request,
        "catalogue/edition_list.html",
        {
            "page": page,
            "q": q,
            "archived": archived,
            "category": category,
            "categories": Category.objects.all(),
            **NAV,
        },
    )


@login_required
def edition_create(request):
    form = EditionForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        data = dict(form.cleaned_data)
        ack = data.pop("isbn_acknowledged", False)
        try:
            edition = services.create_edition(actor=request.user, isbn_acknowledged=ack, **data)
        except RuleViolation as exc:
            form.add_error(None, exc.message)
        else:
            messages.success(request, "Edition created. Add its physical copies next.")
            return redirect("catalogue:edition_detail", pk=edition.pk)
    return render(
        request, "catalogue/edition_form.html", {"form": form, "title": "Add edition", **NAV}
    )


@login_required
def edition_detail(request, pk):
    edition = get_object_or_404(BookEdition.objects.select_related("category"), pk=pk)
    return render(
        request,
        "catalogue/edition_detail.html",
        {
            "edition": edition,
            "copies": edition.copies.all(),
            "layouts": LabelLayout.objects.all(),
            **NAV,
        },
    )


@login_required
def edition_edit(request, pk):
    edition = get_object_or_404(BookEdition, pk=pk)
    form = EditionForm(request.POST or None, instance=edition)
    if request.method == "POST" and form.is_valid():
        data = {k: v for k, v in form.cleaned_data.items() if k != "isbn_acknowledged"}
        try:
            services.update_edition(
                edition,
                actor=request.user,
                isbn_acknowledged=form.cleaned_data.get("isbn_acknowledged", False),
                **data,
            )
        except RuleViolation as exc:
            form.add_error(None, exc.message)
        else:
            messages.success(request, "Edition updated.")
            return redirect("catalogue:edition_detail", pk=pk)
    return render(
        request,
        "catalogue/edition_form.html",
        {"form": form, "title": f"Edit {edition.title}", **NAV},
    )


@login_required
@require_POST
def edition_archive(request, pk):
    edition = get_object_or_404(BookEdition, pk=pk)
    services.set_edition_archived(edition, request.POST.get("archive") == "1", actor=request.user)
    messages.success(request, "Edition archived." if edition.is_archived else "Edition restored.")
    return redirect("catalogue:edition_detail", pk=pk)


@login_required
def copies_add(request, pk):
    edition = get_object_or_404(BookEdition, pk=pk)
    form = CopiesForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        cost = int(d["unit_cost_rupees"] * 100) if d["unit_cost_rupees"] is not None else None
        try:
            created = services.add_copies(
                edition,
                d["count"],
                actor=request.user,
                source=d["source"],
                donor_name=d["donor_name"],
                acquired_on=d["acquired_on"],
                unit_cost_paise=cost,
                shelf_location=d["shelf_location"],
                notes=d["notes"],
            )
        except RuleViolation as exc:
            form.add_error(None, exc.message)
        else:
            messages.success(
                request, f"Added {len(created)} cop{'y' if len(created) == 1 else 'ies'}."
            )
            return redirect("catalogue:edition_detail", pk=pk)
    return render(request, "catalogue/copies_form.html", {"form": form, "edition": edition, **NAV})


@login_required
def copy_detail(request, pk):
    copy = get_object_or_404(BookCopy.objects.select_related("edition"), pk=pk)
    form = CopyStateForm(
        request.POST or None, initial={"condition": copy.condition, "status": copy.status}
    )
    if request.method == "POST" and form.is_valid():
        try:
            services.set_copy_state(copy, actor=request.user, **form.cleaned_data)
        except RuleViolation as exc:
            form.add_error(None, exc.message)
        else:
            messages.success(request, "Copy updated.")
            return redirect("catalogue:copy_detail", pk=pk)
    return render(
        request,
        "catalogue/copy_detail.html",
        {"copy": copy, "form": form, "events": copy.events.all(), **NAV},
    )


@login_required
def labels_pdf(request, pk):
    edition = get_object_or_404(BookEdition, pk=pk)
    layout = (
        LabelLayout.objects.filter(pk=request.GET.get("layout") or 0).first() or default_layout()
    )
    try:
        pdf = render_labels_pdf(edition.copies.select_related("edition"), layout)
    except RuleViolation as exc:
        messages.error(request, exc.message)
        return redirect("catalogue:edition_detail", pk=pk)
    response = HttpResponse(pdf, content_type="application/pdf")
    response["Content-Disposition"] = f'inline; filename="labels-{edition.pk}.pdf"'
    return response


@login_required
def category_list(request):
    form = CategoryForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        cat = form.save()
        from audit.services import record

        record(
            "category.create",
            actor=request.user,
            entity_type="category",
            entity_id=cat.pk,
            summary={"name": cat.name},
        )
        messages.success(request, "Category added.")
        return redirect("catalogue:category_list")
    return render(
        request,
        "catalogue/category_list.html",
        {"form": form, "categories": Category.objects.all(), **NAV},
    )


@login_required
def layout_list(request):
    form = LabelLayoutForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        layout = form.save()
        if layout.is_default:
            LabelLayout.objects.exclude(pk=layout.pk).update(is_default=False)
        messages.success(request, "Label layout saved.")
        return redirect("catalogue:layout_list")
    return render(
        request,
        "catalogue/layout_list.html",
        {"form": form, "layouts": LabelLayout.objects.all(), **NAV},
    )
