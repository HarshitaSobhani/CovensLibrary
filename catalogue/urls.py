from django.urls import path

from . import import_views, views

app_name = "catalogue"
urlpatterns = [
    path("", views.edition_list, name="edition_list"),
    path("new/", views.edition_create, name="edition_create"),
    path("<int:pk>/", views.edition_detail, name="edition_detail"),
    path("<int:pk>/edit/", views.edition_edit, name="edition_edit"),
    path("<int:pk>/archive/", views.edition_archive, name="edition_archive"),
    path("<int:pk>/copies/add/", views.copies_add, name="copies_add"),
    path("<int:pk>/labels.pdf", views.labels_pdf, name="labels_pdf"),
    path("copies/<int:pk>/", views.copy_detail, name="copy_detail"),
    path("categories/", views.category_list, name="category_list"),
    path("label-layouts/", views.layout_list, name="layout_list"),
    path("label-layouts/<int:pk>/default/", views.layout_set_default, name="layout_set_default"),
    path("import/", import_views.import_start, name="import_start"),
    path("import/template.xlsx", import_views.template_download, name="import_template"),
    path("import/<int:pk>/", import_views.import_preview, name="import_preview"),
    path("import/<int:pk>/errors.csv", import_views.error_report, name="import_errors"),
]
