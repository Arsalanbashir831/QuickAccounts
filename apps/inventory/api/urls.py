from django.urls import path

from apps.inventory.api.costing import (
    CostBasisView,
    CostCheckpointView,
    CostPolicyView,
    CostReconciliationView,
)
from apps.inventory.api.stock import (
    LotView,
    ReservationReleaseView,
    ReservationView,
    StockCollectionView,
    StockDocumentView,
    StockPostView,
    StockReadCollectionView,
    StockRebuildView,
    StockReconciliationView,
    StockVoidView,
)
from apps.inventory.api.valuation import InventoryValuationView
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
    path(
        "inventory/cost-reconciliation",
        CostReconciliationView.as_view(),
        name="inventory-cost-reconciliation",
    ),
    path(
        "reports/inventory-valuation-reconciliation",
        InventoryValuationView.as_view(),
        name="inventory-valuation-reconciliation",
    ),
    path(
        "inventory/cost-checkpoints",
        CostCheckpointView.as_view(),
        name="inventory-cost-checkpoints",
    ),
    path("inventory/cost-policy", CostPolicyView.as_view(), name="inventory-cost-policy"),
    path(
        "inventory/movements/<uuid:movement_id>/cost-basis",
        CostBasisView.as_view(),
        name="inventory-cost-basis",
    ),
    path(
        "inventory/documents/<uuid:document_id>/void",
        StockVoidView.as_view(),
        name="stock-document-void",
    ),
    path("inventory/rebuild", StockRebuildView.as_view(), name="stock-rebuild"),
    path("inventory/documents", StockCollectionView.as_view(), name="stock-document-list"),
    path(
        "inventory/documents/<uuid:document_id>",
        StockDocumentView.as_view(),
        name="stock-document-detail",
    ),
    path(
        "inventory/documents/<uuid:document_id>/post",
        StockPostView.as_view(),
        name="stock-document-post",
    ),
    path("inventory/reservations", ReservationView.as_view(), name="stock-reservation-list"),
    path(
        "inventory/reservations/<uuid:reservation_id>/release",
        ReservationReleaseView.as_view(),
        name="stock-reservation-release",
    ),
    path("inventory/lots", LotView.as_view(), name="stock-lot-list"),
    path(
        "inventory/movements",
        StockReadCollectionView.as_view(resource="movements"),
        name="stock-movement-list",
    ),
    path(
        "inventory/availability",
        StockReadCollectionView.as_view(resource="availability"),
        name="stock-availability",
    ),
    path(
        "inventory/reconciliation", StockReconciliationView.as_view(), name="stock-reconciliation"
    ),
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
