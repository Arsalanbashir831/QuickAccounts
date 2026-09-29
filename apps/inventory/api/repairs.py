import uuid
from decimal import Decimal
from typing import Any

from django.db import DatabaseError
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.inventory.api.serializers import _non_blank
from apps.inventory.api.views import _limit, _scope
from apps.inventory.repairs import create_repair, list_repairs, repair_detail, update_repair
from common.access.scopes import company_read_scope
from common.api.errors import APIError, Conflict
from common.api.headers import require_idempotency_key, require_revision


class RepairCreateSerializer(serializers.Serializer[dict[str, Any]]):
    line_id = serializers.UUIDField()
    warehouse_id = serializers.UUIDField()
    quantity = serializers.DecimalField(
        max_digits=20, decimal_places=6, min_value=Decimal("0.000001")
    )
    diagnosis = serializers.CharField(max_length=1000, validators=[_non_blank])
    estimated_cost = serializers.DecimalField(
        max_digits=20, decimal_places=6, min_value=Decimal(0), default=Decimal(0)
    )


class RepairUpdateSerializer(serializers.Serializer[dict[str, Any]]):
    status = serializers.ChoiceField(
        choices=[
            "in_progress",
            "waiting_parts",
            "on_hold",
            "repaired",
            "qc_failed",
            "qc_passed",
            "not_repairable",
        ]
    )
    diagnosis = serializers.CharField(max_length=1000, validators=[_non_blank], required=False)
    estimated_cost = serializers.DecimalField(
        max_digits=20, decimal_places=6, min_value=Decimal(0), required=False
    )
    qc_notes = serializers.CharField(max_length=1000, validators=[_non_blank], required=False)


class RepairCollectionView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(responses=OpenApiTypes.OBJECT, operation_id="return_repair_list")
    def get(self, request: Request, tenant_id: uuid.UUID, company_id: uuid.UUID) -> Response:
        try:
            after = (
                uuid.UUID(request.query_params["cursor"])
                if "cursor" in request.query_params
                else None
            )
        except ValueError as exc:
            raise APIError(code="INVALID_CURSOR", message="Invalid repair cursor.") from exc
        limit = _limit(request)
        with company_read_scope(
            _scope(request, tenant_id, company_id), permission="inventory.view", module="inventory"
        ):
            rows = list_repairs(company_id, after, limit + 1)
        return Response(
            {
                "results": rows[:limit],
                "next_cursor": rows[limit - 1]["id"] if len(rows) > limit else None,
            }
        )

    @extend_schema(
        request=RepairCreateSerializer,
        responses=OpenApiTypes.OBJECT,
        operation_id="return_repair_create",
    )
    def post(self, request: Request, tenant_id: uuid.UUID, company_id: uuid.UUID) -> Response:
        serializer = RepairCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            result = create_repair(
                _scope(request, tenant_id, company_id),
                serializer.validated_data,
                revision=require_revision(request),
                key=require_idempotency_key(request),
                request_id=getattr(request, "request_id", None),
            )
        except DatabaseError as exc:
            raise Conflict(
                "REPAIR_CONFLICT", "Repair violates a lifecycle/stock invariant."
            ) from exc
        return Response(result, status=201)


class RepairDetailView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(responses=OpenApiTypes.OBJECT, operation_id="return_repair_detail")
    def get(
        self, request: Request, tenant_id: uuid.UUID, company_id: uuid.UUID, job_id: uuid.UUID
    ) -> Response:
        with company_read_scope(
            _scope(request, tenant_id, company_id), permission="inventory.view", module="inventory"
        ):
            result = repair_detail(company_id, job_id)
        return Response(result)

    @extend_schema(
        request=RepairUpdateSerializer,
        responses=OpenApiTypes.OBJECT,
        operation_id="return_repair_transition",
    )
    def post(
        self, request: Request, tenant_id: uuid.UUID, company_id: uuid.UUID, job_id: uuid.UUID
    ) -> Response:
        serializer = RepairUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            result = update_repair(
                _scope(request, tenant_id, company_id),
                job_id,
                serializer.validated_data,
                revision=require_revision(request),
                key=require_idempotency_key(request),
                request_id=getattr(request, "request_id", None),
            )
        except DatabaseError as exc:
            raise Conflict(
                "REPAIR_CONFLICT", "Repair violates a lifecycle/stock invariant."
            ) from exc
        return Response(result)
