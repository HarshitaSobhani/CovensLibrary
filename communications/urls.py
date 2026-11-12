from django.urls import path

from . import views

app_name = "communications"
urlpatterns = [
    path("", views.notification_list, name="list"),
    path("<int:pk>/", views.notification_detail, name="detail"),
    path("process/", views.process_now, name="process"),
    path("test-email/", views.test_email, name="test_email"),
    path("templates/", views.templates, name="templates"),
    path("whatsapp/<int:member_pk>/", views.whatsapp, name="whatsapp"),
]
