from django.urls import path

from apps.parties.api.views import (
    PartnerAddressCollectionView,
    PartnerAddressDetailView,
    PartnerCollectionView,
    PartnerDetailView,
    PartnerTaxRegistrationCollectionView,
)

urlpatterns = [
    path("partners", PartnerCollectionView.as_view(), name="partner-list"),
    path("partners/<uuid:partner_id>", PartnerDetailView.as_view(), name="partner-detail"),
    path(
        "partners/<uuid:partner_id>/addresses",
        PartnerAddressCollectionView.as_view(),
        name="partner-address-list",
    ),
    path(
        "partners/<uuid:partner_id>/addresses/<uuid:address_id>",
        PartnerAddressDetailView.as_view(),
        name="partner-address-detail",
    ),
    path(
        "partners/<uuid:partner_id>/tax-registrations",
        PartnerTaxRegistrationCollectionView.as_view(),
        name="partner-tax-registration-list",
    ),
]
