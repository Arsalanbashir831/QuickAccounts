from django.urls import path

from apps.licensing.api.views import (
    ActivationCommandView,
    ActivationListCreateView,
    LicenseSummaryView,
    RenewalOrderDetailView,
    RenewalOrderListCreateView,
)

urlpatterns = [
    path("license", LicenseSummaryView.as_view(), name="tenant-license"),
    path(
        "license/activations",
        ActivationListCreateView.as_view(),
        name="license-activation-list",
    ),
    path(
        "license/activations/<uuid:activation_id>/heartbeat",
        ActivationCommandView.as_view(),
        {"action": "heartbeat"},
        name="license-activation-heartbeat",
    ),
    path(
        "license/activations/<uuid:activation_id>/deactivate",
        ActivationCommandView.as_view(),
        {"action": "deactivate"},
        name="license-activation-deactivate",
    ),
    path(
        "license/renewal-orders",
        RenewalOrderListCreateView.as_view(),
        name="license-renewal-order-list",
    ),
    path(
        "license/renewal-orders/<uuid:order_id>",
        RenewalOrderDetailView.as_view(),
        name="license-renewal-order-detail",
    ),
]
