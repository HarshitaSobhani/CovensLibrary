from django.urls import path

from . import views, workspace_views

app_name = "finance"
urlpatterns = [
    path("payments/", workspace_views.payments, name="payments"),
    path("payments/tokens/", workspace_views.token_activity, name="payments_tokens"),
    path(
        "payments/transactions/<int:pk>/",
        workspace_views.transaction_detail,
        name="payments_detail",
    ),
    path("receipts/<int:pk>/", views.receipt, name="receipt"),
    path("receipts/<int:pk>/pdf/", views.receipt_pdf, name="receipt_pdf"),
    path("member/<int:pk>/refund/", views.refund, name="refund"),
    path("member/<int:pk>/adjust/", views.adjust, name="adjust"),
    path("transaction/<int:pk>/reverse/", views.reverse, name="reverse"),
]
