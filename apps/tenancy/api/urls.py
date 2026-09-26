from django.urls import path
from django.urls.conf import include

from apps.tenancy.api.views import CompanyListView, TenantListView

urlpatterns = [
    path("", TenantListView.as_view(), name="tenant-list"),
    path("<uuid:tenant_id>/companies", CompanyListView.as_view(), name="company-list"),
    path("<uuid:tenant_id>/", include("apps.licensing.api.urls")),
]
