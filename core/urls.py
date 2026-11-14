from django.urls import path

from . import views

app_name = "core"
urlpatterns = [
    path("healthz/", views.healthz, name="healthz"),
    path("", views.dashboard_view, name="dashboard"),
    path("search/", views.search, name="search"),
    path("calendar/", views.calendar_view, name="calendar"),
    path("settings/", views.dashboard_settings, name="settings"),
]
