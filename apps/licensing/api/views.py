import uuid
from typing import cast

from django.db import transaction
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.licensing.api.serializers import (
    ActivationCreateSerializer,
    LicenseRedeemSerializer,
    RenewalOrderCreateSerializer,
)
from apps.licensing.selectors import license_summary, list_activations, renewal_order
from apps.licensing.services import (
    create_activation,
    create_renewal_order,
    redeem_license,
    touch_activation,
)
from common.access.scopes import TenantScope, bind_and_verify_tenant
from common.api.errors import ScopeNotFound
from common.api.headers import require_idempotency_key


def _scope(request: Request, tenant_id: uuid.UUID) -> TenantScope:
    return TenantScope(tenant_id, cast(uuid.UUID, request.user.pk))


class LicenseSummaryView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(responses=OpenApiTypes.OBJECT)
    def get(self, request: Request, tenant_id: uuid.UUID) -> Response:
        with transaction.atomic():
            bind_and_verify_tenant(_scope(request, tenant_id))
            body = license_summary(tenant_id)
        return Response(body)


class LicenseRedeemView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(request=LicenseRedeemSerializer, responses=OpenApiTypes.OBJECT)
    def post(self, request: Request, tenant_id: uuid.UUID) -> Response:
        serializer = LicenseRedeemSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        body, response_status = redeem_license(
            _scope(request, tenant_id),
            credential=serializer.validated_data["credential"],
            idempotency_key=require_idempotency_key(request),
        )
        return Response(body, status=response_status)


class ActivationListCreateView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(responses=OpenApiTypes.OBJECT)
    def get(self, request: Request, tenant_id: uuid.UUID) -> Response:
        with transaction.atomic():
            bind_and_verify_tenant(_scope(request, tenant_id), administer=True)
            summary = license_summary(tenant_id)
            rows = list_activations(tenant_id, summary["license_id"])
        return Response({"results": rows})

    @extend_schema(request=ActivationCreateSerializer, responses={201: OpenApiTypes.OBJECT})
    def post(self, request: Request, tenant_id: uuid.UUID) -> Response:
        serializer = ActivationCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        body, response_status = create_activation(
            _scope(request, tenant_id),
            fingerprint=serializer.validated_data["device_fingerprint"],
            public_key=serializer.validated_data.get("device_public_key"),
            idempotency_key=require_idempotency_key(request),
        )
        return Response(body, status=response_status)


class ActivationCommandView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(request=None, responses=OpenApiTypes.OBJECT)
    def post(
        self,
        request: Request,
        tenant_id: uuid.UUID,
        activation_id: uuid.UUID,
        action: str,
    ) -> Response:
        body = touch_activation(
            _scope(request, tenant_id), activation_id, deactivate=action == "deactivate"
        )
        return Response(body)


class RenewalOrderListCreateView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(request=RenewalOrderCreateSerializer, responses={201: OpenApiTypes.OBJECT})
    def post(self, request: Request, tenant_id: uuid.UUID) -> Response:
        serializer = RenewalOrderCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        body, _ = create_renewal_order(
            _scope(request, tenant_id),
            plan_version_id=serializer.validated_data["plan_version_id"],
            provider=serializer.validated_data["provider"],
            idempotency_key=require_idempotency_key(request),
        )
        return Response(body, status=status.HTTP_201_CREATED)


class RenewalOrderDetailView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(responses=OpenApiTypes.OBJECT)
    def get(self, request: Request, tenant_id: uuid.UUID, order_id: uuid.UUID) -> Response:
        with transaction.atomic():
            bind_and_verify_tenant(_scope(request, tenant_id), administer=True)
            body = renewal_order(tenant_id, order_id)
            if body is None:
                raise ScopeNotFound()
        return Response(body)
