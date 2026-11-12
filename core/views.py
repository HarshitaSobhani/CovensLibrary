import calendar as pycalendar
from datetime import date

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render
from django.utils import timezone

from audit.services import record

from . import dashboard, dashboard_config
from .models import LibrarySetting


@login_required
def dashboard_view(request):
    setting = LibrarySetting.load()
    today = timezone.localdate()
    data = dashboard.build(setting, today)
    return render(
        request,
        "core/dashboard.html",
        {"nav_active": "dashboard", "today": today, "sections": data["sections"]},
    )


@login_required
def search(request):
    result = dashboard.search(request.GET.get("q", ""))
    return render(
        request,
        "core/search.html",
        {"nav_active": "search", "q": request.GET.get("q", "").strip(), "result": result},
    )


def _month(request, today):
    try:
        year, month = (int(x) for x in request.GET.get("month", "").split("-"))
        date(year, month, 1)
        if not 2000 <= year <= 2200:
            raise ValueError
    except ValueError:
        year, month = today.year, today.month
    return year, month


@login_required
def calendar_view(request):
    today = timezone.localdate()
    year, month = _month(request, today)
    setting = LibrarySetting.load()
    prev_y, prev_m = (year - 1, 12) if month == 1 else (year, month - 1)
    next_y, next_m = (year + 1, 1) if month == 12 else (year, month + 1)
    return render(
        request,
        "core/calendar.html",
        {
            "nav_active": "calendar",
            "today": today,
            "title": f"{pycalendar.month_name[month]} {year}",
            "days": dashboard.calendar_events(year, month, setting.grace_days),
            "prev": f"{prev_y:04d}-{prev_m:02d}",
            "next": f"{next_y:04d}-{next_m:02d}",
        },
    )


@login_required
def dashboard_settings(request):
    setting = LibrarySetting.load()
    conf = dashboard_config.load(setting)
    if request.method == "POST":
        new = dict(conf)
        action = request.POST.get("action", "save")
        if action in ("up", "down"):
            new["order"] = dashboard_config.move(
                conf["order"], request.POST.get("section", ""), -1 if action == "up" else 1
            )
        else:
            new["visible"] = {
                k: k in request.POST.getlist("visible") for k in dashboard_config.SECTIONS
            }
            new["criteria"] = {
                k: k in request.POST.getlist("criteria") for k in dashboard_config.CRITERIA
            }
            try:
                new["expiry_window_days"] = int(request.POST.get("expiry_window_days", ""))
            except ValueError:
                messages.error(request, "The expiry window must be a whole number of days.")
                return redirect("core:settings")
            if not 1 <= new["expiry_window_days"] <= dashboard_config.MAX_EXPIRY_WINDOW_DAYS:
                messages.error(
                    request,
                    f"The expiry window must be 1 to {dashboard_config.MAX_EXPIRY_WINDOW_DAYS} "
                    "days.",
                )
                return redirect("core:settings")
        clean = dashboard_config.normalise(new)
        if clean != conf:
            setting.dashboard_config = clean
            setting.save(update_fields=["dashboard_config"])
            record(
                "dashboard.configured",
                request.user,
                "LibrarySetting",
                "1",
                {"action": action},
            )
        messages.success(request, "Dashboard settings saved.")
        return redirect("core:settings")
    return render(
        request,
        "core/settings.html",
        {
            "nav_active": "settings",
            "conf": conf,
            "sections": [
                (k, dashboard_config.SECTIONS[k], conf["visible"][k]) for k in conf["order"]
            ],
            "criteria": [
                (k, label, conf["criteria"][k]) for k, label in dashboard_config.CRITERIA.items()
            ],
        },
    )
