import uuid
from typing import cast

from django.db import transaction
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.module_access.api.serializers import ModuleChangeBatchSerializer
from apps.module_access.selectors import capabilities, module_configuration, module_events
from apps.module_access.services import apply_changes, validate_changes
from common.access.scopes import CompanyScope, bind_and_verify_company, require_permission
from common.api.headers import require_idempotency_key, require_revision


def _scope(request: Request, tenant_id: uuid.UUID, company_id: uuid.UUID) -> CompanyScope:
    return CompanyScope(tenant_id, company_id, cast(uuid.UUID, request.user.pk))


class CapabilitiesView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(responses=OpenApiTypes.OBJECT)
    def get(self, request: Request, tenant_id: uuid.UUID, company_id: uuid.UUID) -> Response:
        scope = _scope(request, tenant_id, company_id)
        with transaction.atomic():
            bind_and_verify_company(scope)
            body = capabilities(company_id)
        return Response(body)


class ModuleListView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(responses=OpenApiTypes.OBJECT)
    def get(self, request: Request, tenant_id: uuid.UUID, company_id: uuid.UUID) -> Response:
        scope = _scope(request, tenant_id, company_id)
        with transaction.atomic():
            bind_and_verify_company(scope)
            require_permission(company_id, "company.modules.manage")
            body = module_configuration(company_id)
        return Response(body, headers={"ETag": f'"{body["policy_revision"]}"'})


class ModuleChangeValidateView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(request=ModuleChangeBatchSerializer, responses=OpenApiTypes.OBJECT)
    def post(self, request: Request, tenant_id: uuid.UUID, company_id: uuid.UUID) -> Response:
        serializer = ModuleChangeBatchSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        with transaction.atomic():
            body = validate_changes(
                _scope(request, tenant_id, company_id),
                serializer.validated_data["changes"],
            )
        return Response(body)


class ModuleChangeView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(request=ModuleChangeBatchSerializer, responses=OpenApiTypes.OBJECT)
    def post(self, request: Request, tenant_id: uuid.UUID, company_id: uuid.UUID) -> Response:
        serializer = ModuleChangeBatchSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        body, response_status = apply_changes(
            _scope(request, tenant_id, company_id),
            changes=serializer.validated_data["changes"],
            reason=serializer.validated_data["reason"],
            expected_revision=require_revision(request),
            idempotency_key=require_idempotency_key(request),
            request_id=getattr(request, "request_id", None),
        )
        return Response(
            body,
            status=response_status,
            headers={"ETag": f'"{body["policy_revision"]}"'},
        )


class ModuleEventListView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(responses=OpenApiTypes.OBJECT)
    def get(self, request: Request, tenant_id: uuid.UUID, company_id: uuid.UUID) -> Response:
        scope = _scope(request, tenant_id, company_id)
        with transaction.atomic():
            bind_and_verify_company(scope)
            require_permission(company_id, "company.modules.manage")
            rows = module_events(company_id)
        return Response({"results": rows})
