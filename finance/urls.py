from django.urls import path

from . import views

app_name = "finance"
urlpatterns = [
    path("receipts/<int:pk>/", views.receipt, name="receipt"),
    path("receipts/<int:pk>/pdf/", views.receipt_pdf, name="receipt_pdf"),
    path("member/<int:pk>/refund/", views.refund, name="refund"),
    path("member/<int:pk>/adjust/", views.adjust, name="adjust"),
    path("transaction/<int:pk>/reverse/", views.reverse, name="reverse"),
]
