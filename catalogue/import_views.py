from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render

from core.errors import RuleViolation
from core.forms import BootstrapFormMixin

from . import import_service as svc
from .models import ImportBatch


class UploadForm(BootstrapFormMixin, forms.Form):
    file = forms.FileField(label="Excel workbook (.xlsx)")


class OptionsForm(BootstrapFormMixin, forms.Form):
    create_missing_categories = forms.BooleanField(
        required=False,
        label="Create missing categories (otherwise rows with unknown categories are rejected)",
    )
    copy_id_mode = forms.ChoiceField(
        choices=ImportBatch.CopyIdMode.choices, widget=forms.RadioSelect
    )
    isbn_warnings_acknowledged = forms.BooleanField(
        required=False, label="I have reviewed the duplicate ISBN warnings and want to continue"
    )
    accept_excluded = forms.BooleanField(
        required=False, label="Leave out rows with errors and import the valid rows"
    )


NAV = {"nav_active": "books"}


@login_required
def template_download(request):
    response = HttpResponse(
        svc.build_template(),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    response["Content-Disposition"] = 'attachment; filename="coven-import-template.xlsx"'
    return response


@login_required
def import_start(request):
    form = UploadForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        try:
            batch = svc.stage_import(form.cleaned_data["file"], actor=request.user)
        except RuleViolation as exc:
            form.add_error("file", exc.message)
        else:
            return redirect("catalogue:import_preview", pk=batch.pk)
    return render(request, "catalogue/import_start.html", {"form": form, **NAV})


@login_required
def import_preview(request, pk):
    batch = get_object_or_404(ImportBatch, pk=pk)
    initial = {
        "create_missing_categories": batch.create_missing_categories,
        "copy_id_mode": batch.copy_id_mode,
        "isbn_warnings_acknowledged": batch.isbn_warnings_acknowledged,
    }
    form = OptionsForm(request.POST or None, initial=initial)
    if (
        request.method == "POST"
        and batch.status == ImportBatch.Status.VALIDATED
        and form.is_valid()
    ):
        svc.set_options(
            batch,
            create_missing_categories=form.cleaned_data["create_missing_categories"],
            copy_id_mode=form.cleaned_data["copy_id_mode"],
            isbn_warnings_acknowledged=form.cleaned_data["isbn_warnings_acknowledged"],
        )
        if "confirm" in request.POST:
            try:
                svc.execute_import(
                    batch, actor=request.user, accept_excluded=form.cleaned_data["accept_excluded"]
                )
            except RuleViolation as exc:
                messages.error(request, exc.message)
            else:
                messages.success(request, "Import completed.")
        return redirect("catalogue:import_preview", pk=pk)
    batch.refresh_from_db()
    return render(
        request,
        "catalogue/import_preview.html",
        {
            "batch": batch,
            "form": form,
            "summary": svc.summarise(batch),
            "page": Paginator(batch.rows.all(), 100).get_page(request.GET.get("page")),
            "qs": "",
            **NAV,
        },
    )


@login_required
def error_report(request, pk):
    batch = get_object_or_404(ImportBatch, pk=pk)
    response = HttpResponse(svc.error_report_csv(batch), content_type="text/csv")
    response["Content-Disposition"] = f'attachment; filename="import-{batch.pk}-report.csv"'
    return response
