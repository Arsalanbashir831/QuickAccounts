from django.urls import path

from apps.payments.api import (
    OpenItemsView,
    PaymentAllocationView,
    PaymentCollectionView,
    PaymentDetailView,
    PaymentPostView,
    PaymentReverseView,
    PaymentWithholdingView,
)

urlpatterns = [
    path("payments", PaymentCollectionView.as_view(), name="payment-list"),
    path("payments/<uuid:payment_id>", PaymentDetailView.as_view(), name="payment-detail"),
    path(
        "payments/<uuid:payment_id>/allocations",
        PaymentAllocationView.as_view(),
        name="payment-allocations",
    ),
    path("payments/<uuid:payment_id>/post", PaymentPostView.as_view(), name="payment-post"),
    path(
        "payments/<uuid:payment_id>/withholding",
        PaymentWithholdingView.as_view(),
        name="payment-withholding",
    ),
    path(
        "payments/<uuid:payment_id>/reverse", PaymentReverseView.as_view(), name="payment-reverse"
    ),
    path(
        "reports/open-receivables",
        OpenItemsView.as_view(direction="receipt"),
        name="open-receivables",
    ),
    path(
        "reports/open-payables",
        OpenItemsView.as_view(direction="disbursement"),
        name="open-payables",
    ),
    path(
        "reports/receivable-aging",
        OpenItemsView.as_view(direction="receipt", aging=True),
        name="receivable-aging",
    ),
    path(
        "reports/payable-aging",
        OpenItemsView.as_view(direction="disbursement", aging=True),
        name="payable-aging",
    ),
]
