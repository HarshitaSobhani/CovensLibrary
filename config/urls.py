from django.urls import include, path

urlpatterns = [
    path("accounts/", include("accounts.urls")),
    path("members/", include("members.urls")),
    path("memberships/", include("memberships.urls")),
    path("finance/", include("finance.urls")),
    path("books/", include("catalogue.urls")),
    path("", include("core.urls")),
]
