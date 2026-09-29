import uuid
from typing import Any

from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.inventory.api.serializers import _non_blank
from apps.inventory.api.views import _limit, _scope
from apps.inventory.warehouse_selectors import (
    list_warehouses,
    warehouse_balances,
    warehouse_detail,
)
from apps.inventory.warehouse_services import save_warehouse
from common.access.scopes import company_read_scope
from common.api.errors import APIError
from common.api.headers import require_revision

CATEGORIES = ["sellable", "quarantine", "damaged", "supplier_return"]


class WarehouseSerializer(serializers.Serializer[dict[str, Any]]):
    code = serializers.CharField(max_length=100, validators=[_non_blank])
    name = serializers.CharField(max_length=255, validators=[_non_blank])
    address_text = serializers.CharField(max_length=2000, allow_null=True, default=None)
    stock_category = serializers.ChoiceField(choices=CATEGORIES, default="sellable")
    operational_role = serializers.ChoiceField(
        choices=[
            "sellable",
            "inspection",
            "repair",
            "dead_stock",
            "clearance",
            "supplier_return",
            "scrap",
        ],
        allow_null=True,
        default=None,
    )
    is_active = serializers.BooleanField(default=True)

    def validate(self, attrs: dict[str, object]) -> dict[str, object]:
        if not attrs:
            raise serializers.ValidationError("At least one field is required.")
        return attrs


class WarehouseCollectionView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(responses=OpenApiTypes.OBJECT, operation_id="inventory_warehouse_list")
    def get(self, request: Request, tenant_id: uuid.UUID, company_id: uuid.UUID) -> Response:
        limit = _limit(request)
        after = request.query_params.get("cursor")
        try:
            after_id = uuid.UUID(after) if after else None
        except ValueError as exc:
            raise APIError(code="INVALID_CURSOR", message="Invalid warehouse cursor.") from exc
        with company_read_scope(
            _scope(request, tenant_id, company_id), permission="inventory.view", module="inventory"
        ):
            rows = list_warehouses(company_id, after_id=after_id, limit=limit)
        return Response(
            {
                "results": rows[:limit],
                "next_cursor": rows[limit - 1]["id"] if len(rows) > limit else None,
            }
        )

    @extend_schema(
        request=WarehouseSerializer,
        responses={201: OpenApiTypes.OBJECT},
        operation_id="inventory_warehouse_create",
    )
    def post(self, request: Request, tenant_id: uuid.UUID, company_id: uuid.UUID) -> Response:
        serializer = WarehouseSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        result = save_warehouse(
            _scope(request, tenant_id, company_id),
            serializer.validated_data,
            request_id=getattr(request, "request_id", None),
        )
        return Response(result, status=201, headers={"ETag": f'"{result["row_version"]}"'})


class WarehouseReadView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(responses=OpenApiTypes.OBJECT, operation_id="inventory_warehouse_retrieve")
    def get(
        self, request: Request, tenant_id: uuid.UUID, company_id: uuid.UUID, warehouse_id: uuid.UUID
    ) -> Response:
        with company_read_scope(
            _scope(request, tenant_id, company_id), permission="inventory.view", module="inventory"
        ):
            result = warehouse_detail(company_id, warehouse_id)
        return Response(result, headers={"ETag": f'"{result["row_version"]}"'})


class WarehouseBalanceView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(responses=OpenApiTypes.OBJECT, operation_id="inventory_warehouse_balances")
    def get(
        self, request: Request, tenant_id: uuid.UUID, company_id: uuid.UUID, warehouse_id: uuid.UUID
    ) -> Response:
        limit = _limit(request)
        after = request.query_params.get("cursor")
        try:
            after_id = uuid.UUID(after) if after else None
        except ValueError as exc:
            raise APIError(code="INVALID_CURSOR", message="Invalid stock balance cursor.") from exc
        with company_read_scope(
            _scope(request, tenant_id, company_id), permission="inventory.view", module="inventory"
        ):
            warehouse = warehouse_detail(company_id, warehouse_id)
            rows = warehouse_balances(company_id, warehouse_id, after_id=after_id, limit=limit)
        return Response(
            {
                "warehouse": warehouse,
                "results": rows[:limit],
                "next_cursor": rows[limit - 1]["id"] if len(rows) > limit else None,
            }
        )


class WarehouseDetailView(WarehouseReadView):
    @extend_schema(
        request=WarehouseSerializer,
        responses=OpenApiTypes.OBJECT,
        operation_id="inventory_warehouse_update",
    )
    def patch(
        self, request: Request, tenant_id: uuid.UUID, company_id: uuid.UUID, warehouse_id: uuid.UUID
    ) -> Response:
        serializer = WarehouseSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        result = save_warehouse(
            _scope(request, tenant_id, company_id),
            serializer.validated_data,
            warehouse_id=warehouse_id,
            expected_revision=require_revision(request),
            request_id=getattr(request, "request_id", None),
        )
        return Response(result, headers={"ETag": f'"{result["row_version"]}"'})
