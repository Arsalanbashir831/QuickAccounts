import datetime as dt
import uuid
from typing import Any

from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.request import Request
from rest_framework.response import Response

from apps.inventory.api.stock import StockAPIView
from apps.inventory.api.views import _scope
from apps.inventory.valuation import valuation_reconciliation
from common.access.scopes import company_read_scope, require_permission
from common.api.errors import APIError


class ValuationQuerySerializer(serializers.Serializer[dict[str, Any]]):
    as_of = serializers.DateField()
    limit = serializers.IntegerField(default=200, min_value=1, max_value=200)


class InventoryValuationView(StockAPIView):
    @extend_schema(
        parameters=[ValuationQuerySerializer],
        responses=OpenApiTypes.OBJECT,
        operation_id="inventory_valuation_reconciliation",
    )
    def get(self, request: Request, tenant_id: uuid.UUID, company_id: uuid.UUID) -> Response:
        query = ValuationQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        as_of = query.validated_data["as_of"]
        if as_of > dt.datetime.now(dt.UTC).date():
            raise APIError(code="INVALID_FILTER", message="as_of must not be in the future.")
        with company_read_scope(
            _scope(request, tenant_id, company_id),
            permission="inventory.reconcile",
            module="inventory",
        ):
            require_permission(company_id, "accounting.ledger.view")
            result = valuation_reconciliation(
                company_id, as_of=as_of, limit=query.validated_data["limit"]
            )
        return Response(result)
