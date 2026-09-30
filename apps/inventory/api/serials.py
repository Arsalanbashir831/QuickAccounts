import uuid
from typing import Any

from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.request import Request
from rest_framework.response import Response

from apps.inventory.api.stock import StockAPIView
from apps.inventory.api.views import _scope
from apps.inventory.serials import list_serials, serial_detail, serial_history
from common.access.scopes import company_read_scope


class SerialQuerySerializer(serializers.Serializer[dict[str, Any]]):
    serial = serializers.CharField(required=False, max_length=100)
    item_id = serializers.UUIDField(required=False)
    warehouse_id = serializers.UUIDField(required=False)
    source_type = serializers.CharField(required=False, max_length=60)
    source_id = serializers.UUIDField(required=False)
    source_line_id = serializers.UUIDField(required=False)
    after = serializers.UUIDField(required=False)
    limit = serializers.IntegerField(default=50, min_value=1, max_value=200)


class HistoryQuerySerializer(serializers.Serializer[dict[str, Any]]):
    after = serializers.UUIDField(required=False)
    limit = serializers.IntegerField(default=50, min_value=1, max_value=200)


class SerialCollectionView(StockAPIView):
    @extend_schema(
        parameters=[SerialQuerySerializer], responses=OpenApiTypes.OBJECT,
        operation_id="inventory_serial_list",
    )
    def get(self, request: Request, tenant_id: uuid.UUID, company_id: uuid.UUID) -> Response:
        query = SerialQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        with company_read_scope(
            _scope(request, tenant_id, company_id),
            permission="inventory.view",
            module="inventory",
        ):
            return Response(list_serials(company_id, **query.validated_data))


class SerialDetailView(StockAPIView):
    @extend_schema(responses=OpenApiTypes.OBJECT, operation_id="inventory_serial_detail")
    def get(
        self, request: Request, tenant_id: uuid.UUID, company_id: uuid.UUID, serial_id: uuid.UUID
    ) -> Response:
        with company_read_scope(
            _scope(request, tenant_id, company_id),
            permission="inventory.view",
            module="inventory",
        ):
            return Response(serial_detail(company_id, serial_id))


class SerialHistoryView(StockAPIView):
    @extend_schema(
        parameters=[HistoryQuerySerializer], responses=OpenApiTypes.OBJECT,
        operation_id="inventory_serial_history",
    )
    def get(
        self, request: Request, tenant_id: uuid.UUID, company_id: uuid.UUID, serial_id: uuid.UUID
    ) -> Response:
        query = HistoryQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        with company_read_scope(
            _scope(request, tenant_id, company_id),
            permission="inventory.view",
            module="inventory",
        ):
            return Response(serial_history(company_id, serial_id, **query.validated_data))
