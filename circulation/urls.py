from django.urls import path

from . import views

app_name = "circulation"
urlpatterns = [
    path("", views.loan_list, name="list"),
    path("issue/", views.loan_issue, name="issue"),
    path("issue/copies/", views.copy_search, name="copy_search"),
    path("decisions/", views.decisions, name="decisions"),
    path("decisions/<int:pk>/<str:action>/", views.decision_resolve, name="decision_resolve"),
    path("grace/", views.grace_settings, name="grace"),
    path("<int:pk>/", views.loan_detail, name="detail"),
    path("<int:pk>/renew/", views.loan_renew, name="renew"),
    path("<int:pk>/lost/", views.loan_lost, name="lost"),
    path("<int:pk>/return/", views.loan_return, name="return"),
]
