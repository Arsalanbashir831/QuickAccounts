from django.urls import path

from apps.accounting.api.views import (
    AccountDetailView,
    ConfigCollectionView,
    EntryCollectionView,
    EntryDetailView,
    EntryPostView,
    EntryReverseView,
    LedgerView,
    PeriodCommandView,
)

urlpatterns = [
    path("accounts", ConfigCollectionView.as_view(), {"resource": "accounts"}, name="account-list"),
    path("accounts/<uuid:account_id>", AccountDetailView.as_view(), name="account-detail"),
    path("journals", ConfigCollectionView.as_view(), {"resource": "journals"}, name="journal-list"),
    path("periods", ConfigCollectionView.as_view(), {"resource": "periods"}, name="period-list"),
    path(
        "periods/<uuid:period_id>/close",
        PeriodCommandView.as_view(),
        {"action": "close"},
        name="period-close",
    ),
    path(
        "periods/<uuid:period_id>/reopen",
        PeriodCommandView.as_view(),
        {"action": "reopen"},
        name="period-reopen",
    ),
    path(
        "dimension-types",
        ConfigCollectionView.as_view(),
        {"resource": "dimension-types"},
        name="dimension-type-list",
    ),
    path(
        "dimension-values",
        ConfigCollectionView.as_view(),
        {"resource": "dimension-values"},
        name="dimension-value-list",
    ),
    path(
        "posting-rules",
        ConfigCollectionView.as_view(),
        {"resource": "posting-rules"},
        name="posting-rule-list",
    ),
    path("entries", EntryCollectionView.as_view(), name="entry-list"),
    path("entries/<uuid:entry_id>", EntryDetailView.as_view(), name="entry-detail"),
    path("entries/<uuid:entry_id>/post", EntryPostView.as_view(), name="entry-post"),
    path("entries/<uuid:entry_id>/reverse", EntryReverseView.as_view(), name="entry-reverse"),
    path("ledger", LedgerView.as_view(), name="ledger"),
]
