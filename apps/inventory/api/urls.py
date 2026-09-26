from django.urls import path

from apps.inventory.api.views import (
    ItemAccountingProfileView,
    ItemCollectionView,
    ItemDetailView,
    UnitOfMeasureCollectionView,
)

urlpatterns = [
    path("reference/uoms", UnitOfMeasureCollectionView.as_view(), name="uom-list"),
    path("inventory/items", ItemCollectionView.as_view(), name="item-list"),
    path("inventory/items/<uuid:item_id>", ItemDetailView.as_view(), name="item-detail"),
    path(
        "inventory/items/<uuid:item_id>/accounting-profile",
        ItemAccountingProfileView.as_view(),
        name="item-accounting-profile",
    ),
]
