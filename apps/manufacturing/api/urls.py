from django.urls import path

from apps.manufacturing.api.views import (
    BomActivateView,
    BomCollectionView,
    BomDetailView,
    BomRetireView,
    OrderCancelView,
    OrderCollectionView,
    OrderCompleteView,
    OrderDetailView,
    OrderMaterialIssueView,
    OrderOutputView,
    OrderReleaseView,
)

urlpatterns = [
    path(
        "manufacturing/orders/<uuid:order_id>/release",
        OrderReleaseView.as_view(),
        name="manufacturing-order-release",
    ),
    path(
        "manufacturing/orders/<uuid:order_id>/material-issues",
        OrderMaterialIssueView.as_view(),
        name="manufacturing-order-material-issues",
    ),
    path(
        "manufacturing/orders/<uuid:order_id>/outputs",
        OrderOutputView.as_view(),
        name="manufacturing-order-output",
    ),
    path(
        "manufacturing/orders/<uuid:order_id>/complete",
        OrderCompleteView.as_view(),
        name="manufacturing-order-complete",
    ),
    path("manufacturing/boms", BomCollectionView.as_view(), name="manufacturing-bom-list"),
    path(
        "manufacturing/boms/<uuid:bom_id>", BomDetailView.as_view(), name="manufacturing-bom-detail"
    ),
    path(
        "manufacturing/boms/<uuid:bom_id>/activate",
        BomActivateView.as_view(),
        name="manufacturing-bom-activate",
    ),
    path(
        "manufacturing/boms/<uuid:bom_id>/retire",
        BomRetireView.as_view(),
        name="manufacturing-bom-retire",
    ),
    path("manufacturing/orders", OrderCollectionView.as_view(), name="manufacturing-order-list"),
    path(
        "manufacturing/orders/<uuid:order_id>",
        OrderDetailView.as_view(),
        name="manufacturing-order-detail",
    ),
    path(
        "manufacturing/orders/<uuid:order_id>/cancel",
        OrderCancelView.as_view(),
        name="manufacturing-order-cancel",
    ),
]
