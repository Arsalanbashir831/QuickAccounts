import uuid
from typing import Any

from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.inventory.api.stock import StockAPIView
from apps.inventory.api.views import _scope
from apps.inventory.cost_checkpoints import create_cost_checkpoint
from apps.inventory.cost_reconciliation import cost_reconciliation
from apps.payments.services import _json, _rows
from common.access.scopes import company_read_scope
from common.api.errors import ScopeNotFound
from common.api.headers import require_idempotency_key


class CostReconciliationQuerySerializer(serializers.Serializer[dict[str, Any]]):
    limit = serializers.IntegerField(default=200, min_value=1, max_value=200)


class CostReconciliationView(StockAPIView):
    @extend_schema(
        parameters=[CostReconciliationQuerySerializer],
        responses=OpenApiTypes.OBJECT,
        operation_id="inventory_cost_reconciliation",
    )
    def get(self, request: Request, tenant_id: uuid.UUID, company_id: uuid.UUID) -> Response:
        query = CostReconciliationQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        with company_read_scope(
            _scope(request, tenant_id, company_id),
            permission="inventory.reconcile",
            module="inventory",
        ):
            result = cost_reconciliation(company_id, limit=query.validated_data["limit"])
        return Response(result)


class CostCheckpointSerializer(serializers.Serializer[dict[str, Any]]):
    warehouse_id = serializers.UUIDField()
    item_id = serializers.UUIDField()
    lot_id = serializers.UUIDField(required=False, allow_null=True)
    reason = serializers.CharField(max_length=2000, allow_blank=False)


class CostCheckpointView(StockAPIView):
    @extend_schema(request=CostCheckpointSerializer, responses=OpenApiTypes.OBJECT)
    def post(self, request: Request, tenant_id: uuid.UUID, company_id: uuid.UUID) -> Response:
        serializer = CostCheckpointSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        result = create_cost_checkpoint(
            _scope(request, tenant_id, company_id),
            serializer.validated_data,
            key=require_idempotency_key(request),
        )
        return Response(result, status=201)


class CostPolicyView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(responses=OpenApiTypes.OBJECT, operation_id="inventory_cost_policy")
    def get(self, request: Request, tenant_id: uuid.UUID, company_id: uuid.UUID) -> Response:
        with company_read_scope(
            _scope(request, tenant_id, company_id), permission="inventory.view", module="inventory"
        ):
            policies = _rows(
                "SELECT * FROM erp.inventory_cost_policies WHERE company_id=%s AND version=1",
                [company_id],
            )
        return Response(
            {
                "configured": bool(policies),
                "policy": _json(policies[0]) if policies else None,
                "supported_method": "scope_average",
            }
        )


class CostBasisView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(responses=OpenApiTypes.OBJECT, operation_id="inventory_movement_cost_basis")
    def get(
        self, request: Request, tenant_id: uuid.UUID, company_id: uuid.UUID, movement_id: uuid.UUID
    ) -> Response:
        with company_read_scope(
            _scope(request, tenant_id, company_id), permission="inventory.view", module="inventory"
        ):
            if not _rows(
                "SELECT id FROM erp.stock_movements WHERE company_id=%s AND id=%s",
                [company_id, movement_id],
            ):
                raise ScopeNotFound()
            snapshots = _rows(
                "SELECT b.*,p.version AS policy_version,p.method,p.rounding_policy "
                "FROM erp.inventory_cost_basis_snapshots b JOIN erp.inventory_cost_policies p "
                "ON p.company_id=b.company_id AND p.id=b.policy_id "
                "WHERE b.company_id=%s AND b.movement_id=%s",
                [company_id, movement_id],
            )
            if not snapshots:
                snapshots = _rows(
                    "SELECT b.*,'linked_return_history' AS method "
                    "FROM erp.inventory_return_cost_basis b "
                    "WHERE b.company_id=%s AND b.movement_id=%s",
                    [company_id, movement_id],
                )
            for snapshot in snapshots:
                snapshot.pop("allocation_seal_xid", None)
        return Response(
            {"available": bool(snapshots), "snapshot": _json(snapshots[0]) if snapshots else None}
        )
