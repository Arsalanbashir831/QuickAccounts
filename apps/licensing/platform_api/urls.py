from django.urls import path

from apps.licensing.platform_api.views import (
    CompanyProvisionView,
    LicenseAssignView,
    LicenseIssueView,
    LicenseStatusView,
    PlanPublishView,
    PlanVersionCreateView,
    TenantProvisionView,
)

urlpatterns = [
    path("tenants", TenantProvisionView.as_view(), name="platform-tenant-provision"),
    path(
        "tenants/<uuid:tenant_id>/companies",
        CompanyProvisionView.as_view(),
        name="platform-company-provision",
    ),
    path("licenses", LicenseIssueView.as_view(), name="platform-license-issue"),
    path("plan-versions", PlanVersionCreateView.as_view(), name="platform-plan-version-create"),
    path(
        "licenses/<uuid:license_id>/assign",
        LicenseAssignView.as_view(),
        name="platform-license-assign",
    ),
    path(
        "licenses/<uuid:license_id>/suspend",
        LicenseStatusView.as_view(),
        {"action": "suspend"},
        name="platform-license-suspend",
    ),
    path(
        "licenses/<uuid:license_id>/revoke",
        LicenseStatusView.as_view(),
        {"action": "revoke"},
        name="platform-license-revoke",
    ),
    path(
        "licenses/<uuid:license_id>/resume",
        LicenseStatusView.as_view(),
        {"action": "resume"},
        name="platform-license-resume",
    ),
    path(
        "plan-versions/<uuid:plan_version_id>/publish",
        PlanPublishView.as_view(),
        name="platform-plan-publish",
    ),
]
