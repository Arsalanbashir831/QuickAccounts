import uuid
from decimal import Decimal
from typing import Any

from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.request import Request
from rest_framework.response import Response

from apps.inventory.api.stock import StockAPIView
from apps.sales.api.serializers import SalesInvoiceAddressSerializer, _non_blank
from apps.sales.api.views import _limit, _scope
from apps.sales.orders import (
    catalog,
    command_order,
    confirm_delivery,
    order_detail,
    save_channel,
    save_order,
)
from common.access.scopes import company_read_scope
from common.api.errors import APIError
from common.api.headers import require_idempotency_key, require_revision


class ChannelSerializer(serializers.Serializer[dict[str, Any]]):
    code = serializers.CharField(max_length=100, validators=[_non_blank])
    name = serializers.CharField(max_length=255, validators=[_non_blank])
    channel_kind = serializers.ChoiceField(
        choices=["retail", "wholesale", "webstore", "marketplace", "other"]
    )
    is_active = serializers.BooleanField(default=True)


class OrderLineSerializer(serializers.Serializer[dict[str, Any]]):
    item_id = serializers.UUIDField()
    description = serializers.CharField(max_length=1000, validators=[_non_blank])
    quantity = serializers.DecimalField(
        max_digits=20, decimal_places=6, min_value=Decimal("0.000001")
    )
    unit_price = serializers.DecimalField(max_digits=20, decimal_places=6, min_value=Decimal(0))
    discount_percent = serializers.DecimalField(
        max_digits=9,
        decimal_places=6,
        min_value=Decimal(0),
        max_value=Decimal(100),
        default=Decimal(0),
    )
    tax_code_id = serializers.UUIDField(required=False, allow_null=True)


class SalesOrderSerializer(serializers.Serializer[dict[str, Any]]):
    order_no = serializers.CharField(max_length=100, validators=[_non_blank])
    partner_id = serializers.UUIDField()
    channel_id = serializers.UUIDField()
    order_date = serializers.DateField()
    currency_code = serializers.RegexField(r"^[A-Z]{3}$")
    external_ref = serializers.CharField(max_length=255, required=False, allow_null=True)
    lines: serializers.ListSerializer[Any] = serializers.ListSerializer(
        child=OrderLineSerializer(), min_length=1, max_length=50
    )


class OrderCommandSerializer(serializers.Serializer[dict[str, Any]]):
    action = serializers.ChoiceField(choices=["confirm", "cancel", "invoice"])
    invoice_no = serializers.CharField(max_length=100, validators=[_non_blank], required=False)
    issue_date = serializers.DateField(required=False)
    tax_point_date = serializers.DateField(required=False)
    due_date = serializers.DateField(required=False, allow_null=True)
    tax_jurisdiction_id = serializers.UUIDField(required=False, allow_null=True)
    exchange_rate = serializers.DecimalField(
        max_digits=20, decimal_places=10, min_value=Decimal("0.0000000001"), default=Decimal(1)
    )
    addresses = SalesInvoiceAddressSerializer(many=True, required=False, default=list)

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        if attrs["action"] == "invoice" and not {"invoice_no", "issue_date"} <= attrs.keys():
            raise serializers.ValidationError("Invoice number and issue date are required.")
        return attrs


class DeliverySerializer(serializers.Serializer[dict[str, Any]]):
    delivered_date = serializers.DateField()
    delivery_reference = serializers.CharField(max_length=255, validators=[_non_blank])
    received_by = serializers.CharField(max_length=255, validators=[_non_blank])
    notes = serializers.CharField(max_length=2000, validators=[_non_blank])


class OrderCollectionView(StockAPIView):
    @extend_schema(responses=OpenApiTypes.OBJECT, operation_id="sales_orders_list")
    def get(self, request: Request, tenant_id: uuid.UUID, company_id: uuid.UUID) -> Response:
        limit = _limit(request)
        try:
            after = (
                uuid.UUID(request.query_params["cursor"])
                if "cursor" in request.query_params
                else None
            )
        except ValueError as exc:
            raise APIError(code="INVALID_CURSOR", message="Invalid order cursor.") from exc
        with company_read_scope(
            _scope(request, tenant_id, company_id), permission="sales.order.manage", module="sales"
        ):
            rows = catalog(company_id, "orders", after, limit + 1)
        return Response(
            {
                "results": rows[:limit],
                "next_cursor": rows[limit - 1]["id"] if len(rows) > limit else None,
            }
        )

    @extend_schema(
        request=SalesOrderSerializer,
        responses={201: OpenApiTypes.OBJECT},
        operation_id="sales_order_create",
    )
    def post(self, request: Request, tenant_id: uuid.UUID, company_id: uuid.UUID) -> Response:
        serializer = SalesOrderSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        result = save_order(
            _scope(request, tenant_id, company_id),
            serializer.validated_data,
            order=None,
            revision=None,
            key=require_idempotency_key(request),
            request_id=getattr(request, "request_id", None),
        )
        return Response(result, status=201)


class OrderDetailView(StockAPIView):
    @extend_schema(responses=OpenApiTypes.OBJECT, operation_id="sales_order_detail")
    def get(
        self, request: Request, tenant_id: uuid.UUID, company_id: uuid.UUID, order_id: uuid.UUID
    ) -> Response:
        with company_read_scope(
            _scope(request, tenant_id, company_id), permission="sales.order.manage", module="sales"
        ):
            result = order_detail(company_id, order_id)
        return Response(result)

    @extend_schema(
        request=SalesOrderSerializer,
        responses=OpenApiTypes.OBJECT,
        operation_id="sales_order_update",
    )
    def patch(
        self, request: Request, tenant_id: uuid.UUID, company_id: uuid.UUID, order_id: uuid.UUID
    ) -> Response:
        serializer = SalesOrderSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        if not serializer.validated_data:
            raise APIError(code="EMPTY_ORDER_UPDATE", message="Supply an order field.")
        return Response(
            save_order(
                _scope(request, tenant_id, company_id),
                serializer.validated_data,
                order=order_id,
                revision=require_revision(request),
                key=require_idempotency_key(request),
                request_id=getattr(request, "request_id", None),
            )
        )

    @extend_schema(
        request=OrderCommandSerializer,
        responses=OpenApiTypes.OBJECT,
        operation_id="sales_order_command",
    )
    def post(
        self, request: Request, tenant_id: uuid.UUID, company_id: uuid.UUID, order_id: uuid.UUID
    ) -> Response:
        serializer = OrderCommandSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return Response(
            command_order(
                _scope(request, tenant_id, company_id),
                order_id,
                serializer.validated_data,
                revision=require_revision(request),
                key=require_idempotency_key(request),
                request_id=getattr(request, "request_id", None),
            )
        )


class ChannelCollectionView(StockAPIView):
    @extend_schema(responses=OpenApiTypes.OBJECT, operation_id="sales_channels_list")
    def get(self, request: Request, tenant_id: uuid.UUID, company_id: uuid.UUID) -> Response:
        limit = _limit(request)
        try:
            after = (
                uuid.UUID(request.query_params["cursor"])
                if "cursor" in request.query_params
                else None
            )
        except ValueError as exc:
            raise APIError(code="INVALID_CURSOR", message="Invalid channel cursor.") from exc
        with company_read_scope(
            _scope(request, tenant_id, company_id), permission="sales.order.manage", module="sales"
        ):
            rows = catalog(company_id, "channels", after, limit + 1)
        return Response(
            {
                "results": rows[:limit],
                "next_cursor": rows[limit - 1]["id"] if len(rows) > limit else None,
            }
        )

    @extend_schema(
        request=ChannelSerializer,
        responses={201: OpenApiTypes.OBJECT},
        operation_id="sales_channel_create",
    )
    def post(self, request: Request, tenant_id: uuid.UUID, company_id: uuid.UUID) -> Response:
        serializer = ChannelSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return Response(
            save_channel(
                _scope(request, tenant_id, company_id),
                serializer.validated_data,
                channel=None,
                revision=None,
                key=require_idempotency_key(request),
                request_id=getattr(request, "request_id", None),
            ),
            status=201,
        )


class ChannelDetailView(StockAPIView):
    @extend_schema(
        request=ChannelSerializer,
        responses=OpenApiTypes.OBJECT,
        operation_id="sales_channel_update",
    )
    def patch(
        self, request: Request, tenant_id: uuid.UUID, company_id: uuid.UUID, channel_id: uuid.UUID
    ) -> Response:
        serializer = ChannelSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        if not serializer.validated_data:
            raise APIError(code="EMPTY_CHANNEL_UPDATE", message="Supply a channel field.")
        return Response(
            save_channel(
                _scope(request, tenant_id, company_id),
                serializer.validated_data,
                channel=channel_id,
                revision=require_revision(request),
                key=require_idempotency_key(request),
                request_id=getattr(request, "request_id", None),
            )
        )


class DeliveryView(StockAPIView):
    @extend_schema(
        request=DeliverySerializer,
        responses=OpenApiTypes.OBJECT,
        operation_id="sales_delivery_confirm",
    )
    def post(
        self, request: Request, tenant_id: uuid.UUID, company_id: uuid.UUID, invoice_id: uuid.UUID
    ) -> Response:
        serializer = DeliverySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return Response(
            confirm_delivery(
                _scope(request, tenant_id, company_id),
                invoice_id,
                serializer.validated_data,
                revision=require_revision(request),
                key=require_idempotency_key(request),
                request_id=getattr(request, "request_id", None),
            )
        )
