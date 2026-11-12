from django.urls import path

from . import views

app_name = "memberships"
urlpatterns = [
    path("register/", views.register, name="register"),
    path("<int:pk>/renew/", views.renew, name="renew"),
    path("<int:pk>/token/", views.fresh_token, name="fresh_token"),
    path("subscription/<int:pk>/cancel/", views.cancel, name="cancel"),
]
