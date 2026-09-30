from django.urls import path

from apps.sales.api.orders import (
    ChannelCollectionView,
    ChannelDetailView,
    DeliveryView,
    OrderCollectionView,
    OrderDetailView,
)
from apps.sales.api.returns import (
    ReturnApplyCreditView,
    ReturnCollectionView,
    ReturnDetailView,
    ReturnInspectView,
    ReturnPostView,
    ReturnRefundView,
    ReturnReplacementSerialsView,
    ReturnReplacementView,
    ReturnStockDispositionView,
    ReturnSummaryView,
    ReturnVoidView,
)
from apps.sales.api.views import (
    SalesCreditNoteCollectionView,
    SalesInvoiceCalculateView,
    SalesInvoiceCollectionView,
    SalesInvoiceDetailView,
    SalesInvoicePostView,
)

urlpatterns = [
    path("sales/orders", OrderCollectionView.as_view(), name="sales-orders"),
    path("sales/orders/<uuid:order_id>", OrderDetailView.as_view(), name="sales-order-detail"),
    path("sales/channels", ChannelCollectionView.as_view(), name="sales-channels"),
    path(
        "sales/channels/<uuid:channel_id>", ChannelDetailView.as_view(), name="sales-channel-detail"
    ),
    path(
        "sales/invoices/<uuid:invoice_id>/delivery", DeliveryView.as_view(), name="sales-delivery"
    ),
    path(
        "sales/returns/<uuid:return_id>/stock-dispositions",
        ReturnStockDispositionView.as_view(),
        name="sale-return-stock-dispose",
    ),
    path(
        "sales/invoices/<uuid:invoice_id>/return-eligibility",
        ReturnSummaryView.as_view(),
        name="sale-return-eligibility",
    ),
    path("sales/returns/<uuid:return_id>/void", ReturnVoidView.as_view(), name="sale-return-void"),
    path(
        "sales/returns/<uuid:return_id>/apply-credit",
        ReturnApplyCreditView.as_view(),
        name="sale-return-apply-credit",
    ),
    path(
        "sales/invoices/<uuid:invoice_id>/returns",
        ReturnCollectionView.as_view(),
        name="sale-return-list",
    ),
    path("sales/returns/<uuid:return_id>", ReturnDetailView.as_view(), name="sale-return-detail"),
    path(
        "sales/returns/<uuid:return_id>/inspect",
        ReturnInspectView.as_view(),
        name="sale-return-inspect",
    ),
    path("sales/returns/<uuid:return_id>/post", ReturnPostView.as_view(), name="sale-return-post"),
    path(
        "sales/returns/<uuid:return_id>/refund",
        ReturnRefundView.as_view(),
        name="sale-return-refund",
    ),
    path(
        "sales/returns/<uuid:return_id>/replace",
        ReturnReplacementView.as_view(),
        name="sale-return-replace",
    ),
    path(
        "sales/returns/<uuid:return_id>/replacement-serials",
        ReturnReplacementSerialsView.as_view(),
        name="sale-return-replacement-serials",
    ),
    path("sales/invoices", SalesInvoiceCollectionView.as_view(), name="sales-invoice-list"),
    path(
        "sales/invoices/<uuid:invoice_id>",
        SalesInvoiceDetailView.as_view(),
        name="sales-invoice-detail",
    ),
    path(
        "sales/invoices/<uuid:invoice_id>/calculate",
        SalesInvoiceCalculateView.as_view(),
        name="sales-invoice-calculate",
    ),
    path(
        "sales/invoices/<uuid:invoice_id>/post",
        SalesInvoicePostView.as_view(),
        name="sales-invoice-post",
    ),
    path(
        "sales/invoices/<uuid:invoice_id>/credit-notes",
        SalesCreditNoteCollectionView.as_view(),
        name="sales-invoice-credit-note-list",
    ),
]
