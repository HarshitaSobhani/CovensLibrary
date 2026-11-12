from datetime import date

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from audit.services import record
from members.models import Member

from . import export, periods, queries, statement
from . import render as renderers

FORMATS = {
    "csv": ("text/csv; charset=utf-8", renderers.to_csv),
    "xlsx": (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        renderers.to_xlsx,
    ),
    "pdf": ("application/pdf", renderers.to_pdf),
}


def audit_export(request, report, kind, fmt, start=None, end=None):
    """Exports are audited without personal data: what, which filters, how many rows."""
    record(
        "report.export",
        request.user,
        "Report",
        kind,
        {
            "format": fmt,
            "from": start.isoformat() if start else "",
            "to": end.isoformat() if end else "",
            "rows": report.row_count,
        },
    )


def file_response(request, report, kind, fmt, start=None, end=None, stem=None):
    content_type, build = FORMATS[fmt]
    body = build(report)
    audit_export(request, report, kind, fmt, start, end)
    span = f"-{start:%Y%m%d}-{end:%Y%m%d}" if start and end else ""
    response = HttpResponse(body, content_type=content_type)
    response["Content-Disposition"] = (
        f'attachment; filename="{stem or "coven-" + kind}{span}.{fmt}"'
    )
    response["Cache-Control"] = "no-store"
    return response


@login_required
def index(request):
    return render(
        request,
        "reports/index.html",
        {
            "nav_active": "reports",
            "reports": [(k, title) for k, (title, _) in queries.REPORTS.items()],
        },
    )


@login_required
def report(request, key):
    if key not in queries.REPORTS:
        raise Http404
    today = timezone.localdate()
    preset = request.GET.get("preset", "month")
    error = ""
    try:
        start, end, label = periods.resolve(
            preset, today, request.GET.get("start"), request.GET.get("end")
        )
    except periods.PeriodError as exc:
        error = str(exc)
        start, end, label = periods.resolve("month", today)
        preset = "month"
    fmt = request.GET.get("format", "html")
    if fmt != "html" and not error:
        if fmt not in FORMATS or (fmt == "pdf" and key not in queries.PDF_REPORTS):
            raise Http404
        return file_response(request, queries.build(key, start, end, today), key, fmt, start, end)
    data = queries.build(key, start, end, today)
    sections = []
    for section in data.sections:
        body, total = renderers.rows_for_screen(section)
        sections.append({"section": section, "rows": body, "total": total})
    return render(
        request,
        "reports/report.html",
        {
            "nav_active": "reports",
            "key": key,
            "report": data,
            "sections": sections,
            "presets": periods.PRESETS,
            "preset": preset,
            "start": start,
            "end": end,
            "label": label,
            "error": error,
            "has_pdf": key in queries.PDF_REPORTS,
        },
    )


@login_required
def statement_lookup(request):
    number = request.GET.get("member_no", "").strip().upper()
    member = Member.objects.filter(membership_no=number).first()
    if member is None:
        messages.error(request, "No member has this ID.")
        return redirect("reports:index")
    return redirect("reports:statement", pk=member.pk)


@login_required
def member_statement(request, pk):
    member = get_object_or_404(Member, pk=pk)
    today = timezone.localdate()
    start = end = None
    error = ""
    raw_start, raw_end = request.GET.get("start", ""), request.GET.get("end", "")
    try:
        if raw_start:
            start = date.fromisoformat(raw_start)
        if raw_end:
            end = date.fromisoformat(raw_end)
        if start and end and start > end:
            raise ValueError
    except ValueError:
        error = "Enter valid dates (YYYY-MM-DD) with the start not after the end."
        start = end = None
    data = statement.build(member, start, end, today)
    fmt = request.GET.get("format", "html")
    if fmt != "html" and not error:
        if fmt not in FORMATS:
            raise Http404
        return file_response(
            request,
            data,
            "statement",
            fmt,
            start,
            end,
            stem=f"coven-statement-{member.membership_no}",
        )
    sections = []
    for section in data.sections:
        body, total = renderers.rows_for_screen(section)
        sections.append({"section": section, "rows": body, "total": total})
    return render(
        request,
        "reports/statement.html",
        {
            "nav_active": "reports",
            "member": member,
            "report": data,
            "sections": sections,
            "start": start,
            "end": end,
            "error": error,
        },
    )


@login_required
@require_POST
def full_export(request):
    data, manifest = export.build_zip()
    rows = sum(f["rows"] for f in manifest["files"] if f["name"].endswith(".csv"))
    record(
        "report.full_export",
        request.user,
        "Export",
        "zip",
        {"files": len(manifest["files"]), "rows": rows},
    )
    stamp = timezone.localtime().strftime("%Y%m%d-%H%M%S")
    response = HttpResponse(data, content_type="application/zip")
    response["Content-Disposition"] = f'attachment; filename="coven-library-export-{stamp}.zip"'
    response["Cache-Control"] = "no-store"
    return response
