from django.contrib import admin
from django.http import JsonResponse
from django.urls import include, path
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView


def health(_: object) -> JsonResponse:
    return JsonResponse({"status": "ok"})


urlpatterns = [
    path("health/live", health, name="health-live"),
    path("health/ready", health, name="health-ready"),
    path("api/schema", SpectacularAPIView.as_view(), name="openapi-schema"),
    path(
        "api/docs",
        SpectacularSwaggerView.as_view(url_name="openapi-schema"),
        name="openapi-docs",
    ),
    path("api/v1/auth/", include("apps.identity.api.urls")),
    path("api/v1/tenants/", include("apps.tenancy.api.urls")),
    path("platform-api/v1/", include("apps.licensing.platform_api.urls")),
    path(
        "api/v1/tenants/<uuid:tenant_id>/companies/<uuid:company_id>/accounting/",
        include("apps.accounting.api.urls"),
    ),
    path(
        "api/v1/tenants/<uuid:tenant_id>/companies/<uuid:company_id>/",
        include("apps.module_access.api.urls"),
    ),
    path(
        "api/v1/tenants/<uuid:tenant_id>/companies/<uuid:company_id>/reports/",
        include("apps.reporting.api.urls"),
    ),
    path("admin/", admin.site.urls),
]
