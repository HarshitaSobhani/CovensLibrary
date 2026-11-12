from django.urls import path

from . import views

app_name = "reports"
urlpatterns = [
    path("", views.index, name="index"),
    path("export/", views.full_export, name="export"),
    path("statement/", views.statement_lookup, name="statement_lookup"),
    path("statement/<int:pk>/", views.member_statement, name="statement"),
    path("<str:key>/", views.report, name="report"),
]
