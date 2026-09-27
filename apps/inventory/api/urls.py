from django.urls import path

from apps.inventory.api.views import (
    ItemAccountingProfileView,
    ItemCollectionView,
    ItemDetailView,
    UnitOfMeasureCollectionView,
)
from apps.inventory.api.warehouses import (
    WarehouseBalanceView,
    WarehouseCollectionView,
    WarehouseDetailView,
)
from apps.inventory.dead_stock import DeadStockView

urlpatterns = [
    path("reports/dead-stock", DeadStockView.as_view(), name="dead-stock-report"),
    path("inventory/warehouses", WarehouseCollectionView.as_view(), name="warehouse-list"),
    path(
        "inventory/warehouses/<uuid:warehouse_id>",
        WarehouseDetailView.as_view(),
        name="warehouse-detail",
    ),
    path(
        "inventory/warehouses/<uuid:warehouse_id>/balances",
        WarehouseBalanceView.as_view(),
        name="warehouse-balances",
    ),
    path("reference/uoms", UnitOfMeasureCollectionView.as_view(), name="uom-list"),
    path("inventory/items", ItemCollectionView.as_view(), name="item-list"),
    path("inventory/items/<uuid:item_id>", ItemDetailView.as_view(), name="item-detail"),
    path(
        "inventory/items/<uuid:item_id>/accounting-profile",
        ItemAccountingProfileView.as_view(),
        name="item-accounting-profile",
    ),
]
