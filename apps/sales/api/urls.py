from django.urls import path

from apps.sales.api.views import (
    SalesCreditNoteCollectionView,
    SalesInvoiceCalculateView,
    SalesInvoiceCollectionView,
    SalesInvoiceDetailView,
    SalesInvoicePostView,
)

urlpatterns = [
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
