from django.urls import path

from apps.purchasing.api.claims import SupplierClaimCollectionView, SupplierClaimDetailView
from apps.purchasing.api.views import (
    PurchaseBillCalculateView,
    PurchaseBillCollectionView,
    PurchaseBillDetailView,
    PurchaseBillPostView,
    SupplierCreditCollectionView,
)

urlpatterns = [
    path(
        "purchasing/supplier-claims",
        SupplierClaimCollectionView.as_view(),
        name="supplier-claim-list",
    ),
    path(
        "purchasing/supplier-claims/<uuid:claim_id>",
        SupplierClaimDetailView.as_view(),
        name="supplier-claim-detail",
    ),
    path("purchasing/bills", PurchaseBillCollectionView.as_view(), name="purchase-bill-list"),
    path(
        "purchasing/bills/<uuid:bill_id>",
        PurchaseBillDetailView.as_view(),
        name="purchase-bill-detail",
    ),
    path(
        "purchasing/bills/<uuid:bill_id>/calculate",
        PurchaseBillCalculateView.as_view(),
        name="purchase-bill-calculate",
    ),
    path(
        "purchasing/bills/<uuid:bill_id>/post",
        PurchaseBillPostView.as_view(),
        name="purchase-bill-post",
    ),
    path(
        "purchasing/bills/<uuid:bill_id>/credit-notes",
        SupplierCreditCollectionView.as_view(),
        name="purchase-bill-credit-note-list",
    ),
]
