from django.urls import path

from apps.module_access.api.views import (
    CapabilitiesView,
    ModuleChangeValidateView,
    ModuleChangeView,
    ModuleEventListView,
    ModuleListView,
)

urlpatterns = [
    path("capabilities", CapabilitiesView.as_view(), name="company-capabilities"),
    path("admin/modules", ModuleListView.as_view(), name="company-module-list"),
    path(
        "admin/module-changes/validate",
        ModuleChangeValidateView.as_view(),
        name="company-module-change-validate",
    ),
    path("admin/module-changes", ModuleChangeView.as_view(), name="company-module-change"),
    path("admin/module-events", ModuleEventListView.as_view(), name="company-module-events"),
]
