import datetime as dt
import logging
import uuid
from decimal import Decimal

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
from apps.inventory.stock_commands import (
    create_lot,
    document_detail,
    post_document,
    rebuild_positions,
    release_stock,
    reserve_stock,
    save_document,
    void_document,
)
from apps.inventory.stock_queries import reconciliation, stock_list
from common.access.scopes import company_read_scope
from common.api.errors import APIError, Conflict
from common.api.headers import require_idempotency_key, require_revision


class StockLineSerializer(serializers.Serializer):
    item_id = serializers.UUIDField()
    lot_id = serializers.UUIDField(required=False, allow_null=True)
    from_warehouse_id = serializers.UUIDField(required=False, allow_null=True)
    to_warehouse_id = serializers.UUIDField(required=False, allow_null=True)
    quantity = serializers.DecimalField(
        max_digits=20, decimal_places=6, min_value=Decimal("0.000001")
    )
    unit_cost_company = serializers.DecimalField(
        max_digits=20, decimal_places=6, min_value=Decimal(0), required=False
    )
    sales_invoice_line_id = serializers.UUIDField(required=False, allow_null=True)


class StockDocumentSerializer(serializers.Serializer):
    document_no = serializers.CharField(max_length=100, validators=[_non_blank])
    document_kind = serializers.ChoiceField(
        choices=["receipt", "shipment", "transfer", "adjustment_in", "adjustment_out"]
    )
    document_date = serializers.DateField()
    reason = serializers.CharField(max_length=2000, default="", allow_blank=True)
    lines = StockLineSerializer(many=True, allow_empty=False, max_length=100)


class StockPostSerializer(serializers.Serializer):
    journal_id = serializers.UUIDField()
    fiscal_period_id = serializers.UUIDField()
    offset_account_id = serializers.UUIDField(required=False)
    approve_loss = serializers.BooleanField(default=False)


class ReservationSerializer(serializers.Serializer):
    line_id = serializers.UUIDField()
    document_revision = serializers.IntegerField(min_value=1)
    reservation_key = serializers.CharField(max_length=100, validators=[_non_blank])
    quantity = serializers.DecimalField(
        max_digits=20, decimal_places=6, min_value=Decimal("0.000001")
    )


class LotSerializer(serializers.Serializer):
    item_id = serializers.UUIDField()
    lot_code = serializers.CharField(max_length=100, validators=[_non_blank])
    serial_code = serializers.CharField(
        max_length=100, required=False, allow_null=True, validators=[_non_blank]
    )
    received_on = serializers.DateField(required=False, allow_null=True)
    expires_on = serializers.DateField(required=False, allow_null=True)


class StockAPIView(APIView):
    permission_classes = [IsAuthenticated]

    def handle_exception(self, exc: Exception) -> Response:
        if isinstance(exc, DatabaseError):
            logging.getLogger(__name__).warning("Stock database invariant failed", exc_info=True)
            exc = Conflict(
                "STOCK_WRITE_CONFLICT", "Stock command conflicts with an inventory invariant."
            )
        return super().handle_exception(exc)


class StockCollectionView(StockAPIView):
    resource = "documents"

    @extend_schema(responses=OpenApiTypes.OBJECT)
    def get(self, request: Request, tenant_id: uuid.UUID, company_id: uuid.UUID) -> Response:
        limit = _limit(request)
        try:
            cursor = (
                uuid.UUID(request.query_params["cursor"])
                if request.query_params.get("cursor")
                else None
            )
            as_of = (
                dt.datetime.fromisoformat(request.query_params["as_of"])
                if request.query_params.get("as_of")
                else None
            )
            if as_of is not None and as_of.tzinfo is None:
                raise ValueError("Timezone required")
        except ValueError as exc:
            raise APIError(
                code="INVALID_STOCK_FILTER", message="Invalid cursor or timezone-aware as_of."
            ) from exc
        with company_read_scope(
            _scope(request, tenant_id, company_id), permission="inventory.view", module="inventory"
        ):
            result = stock_list(company_id, self.resource, after=cursor, limit=limit, as_of=as_of)
        return Response(
            {
                "results": result[:limit],
                "next_cursor": result[limit - 1]["id"] if len(result) > limit else None,
                "historical_on_hand_only": bool(as_of and self.resource == "availability"),
            }
        )

    @extend_schema(request=StockDocumentSerializer, responses={201: OpenApiTypes.OBJECT})
    def post(self, request: Request, tenant_id: uuid.UUID, company_id: uuid.UUID) -> Response:
        serializer = StockDocumentSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        result = save_document(
            _scope(request, tenant_id, company_id),
            serializer.validated_data,
            key=require_idempotency_key(request),
            request_id=getattr(request, "request_id", None),
        )
        return Response(result, status=201, headers={"ETag": f'"{result["row_version"]}"'})


class StockReadCollectionView(StockCollectionView):
    http_method_names = ["get", "head", "options"]


class StockDocumentView(StockAPIView):
    @extend_schema(responses=OpenApiTypes.OBJECT, operation_id="inventory_stock_document_detail")
    def get(
        self, request: Request, tenant_id: uuid.UUID, company_id: uuid.UUID, document_id: uuid.UUID
    ) -> Response:
        with company_read_scope(
            _scope(request, tenant_id, company_id), permission="inventory.view", module="inventory"
        ):
            result = document_detail(company_id, document_id)
        return Response(result, headers={"ETag": f'"{result["row_version"]}"'})

    @extend_schema(request=StockDocumentSerializer, responses=OpenApiTypes.OBJECT)
    def patch(
        self, request: Request, tenant_id: uuid.UUID, company_id: uuid.UUID, document_id: uuid.UUID
    ) -> Response:
        serializer = StockDocumentSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        if "document_kind" in serializer.validated_data or not serializer.validated_data:
            raise APIError(
                code="INVALID_STOCK_UPDATE", message="Document kind is immutable; supply changes."
            )
        result = save_document(
            _scope(request, tenant_id, company_id),
            serializer.validated_data,
            document_id=document_id,
            revision=require_revision(request),
            key=require_idempotency_key(request),
            request_id=getattr(request, "request_id", None),
        )
        return Response(result, headers={"ETag": f'"{result["row_version"]}"'})


class StockPostView(StockAPIView):
    @extend_schema(request=StockPostSerializer, responses=OpenApiTypes.OBJECT)
    def post(
        self, request: Request, tenant_id: uuid.UUID, company_id: uuid.UUID, document_id: uuid.UUID
    ) -> Response:
        serializer = StockPostSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        result = post_document(
            _scope(request, tenant_id, company_id),
            document_id,
            serializer.validated_data,
            revision=require_revision(request),
            key=require_idempotency_key(request),
            request_id=getattr(request, "request_id", None),
        )
        return Response(result, headers={"ETag": f'"{result["row_version"]}"'})


class ReservationView(StockReadCollectionView):
    resource = "reservations"
    http_method_names = ["get", "post", "head", "options"]

    @extend_schema(request=ReservationSerializer, responses={201: OpenApiTypes.OBJECT})
    def post(self, request: Request, tenant_id: uuid.UUID, company_id: uuid.UUID) -> Response:
        serializer = ReservationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        result = reserve_stock(
            _scope(request, tenant_id, company_id),
            serializer.validated_data,
            key=require_idempotency_key(request),
            request_id=getattr(request, "request_id", None),
        )
        return Response(result, status=201, headers={"ETag": f'"{result["row_version"]}"'})


class ReservationReleaseView(StockAPIView):
    @extend_schema(request=None, responses=OpenApiTypes.OBJECT)
    def post(
        self,
        request: Request,
        tenant_id: uuid.UUID,
        company_id: uuid.UUID,
        reservation_id: uuid.UUID,
    ) -> Response:
        result = release_stock(
            _scope(request, tenant_id, company_id),
            reservation_id,
            revision=require_revision(request),
            key=require_idempotency_key(request),
            request_id=getattr(request, "request_id", None),
        )
        return Response(result, headers={"ETag": f'"{result["row_version"]}"'})


class LotView(StockReadCollectionView):
    resource = "lots"
    http_method_names = ["get", "post", "head", "options"]

    @extend_schema(request=LotSerializer, responses={201: OpenApiTypes.OBJECT})
    def post(self, request: Request, tenant_id: uuid.UUID, company_id: uuid.UUID) -> Response:
        serializer = LotSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        result = create_lot(
            _scope(request, tenant_id, company_id),
            serializer.validated_data,
            key=require_idempotency_key(request),
            request_id=getattr(request, "request_id", None),
        )
        return Response(result, status=201)


class StockReconciliationView(StockAPIView):
    @extend_schema(responses=OpenApiTypes.OBJECT)
    def get(self, request: Request, tenant_id: uuid.UUID, company_id: uuid.UUID) -> Response:
        with company_read_scope(
            _scope(request, tenant_id, company_id),
            permission="inventory.reconcile",
            module="inventory",
        ):
            result = reconciliation(company_id)
        return Response(
            {"matches": not result, "differences": result[:200], "truncated": len(result) > 200}
        )


class StockRebuildView(StockAPIView):
    @extend_schema(request=None, responses=OpenApiTypes.OBJECT)
    def post(self, request: Request, tenant_id: uuid.UUID, company_id: uuid.UUID) -> Response:
        return Response(
            rebuild_positions(
                _scope(request, tenant_id, company_id),
                key=require_idempotency_key(request),
                request_id=getattr(request, "request_id", None),
            )
        )


class StockVoidView(StockAPIView):
    @extend_schema(request=None, responses=OpenApiTypes.OBJECT)
    def post(
        self, request: Request, tenant_id: uuid.UUID, company_id: uuid.UUID, document_id: uuid.UUID
    ) -> Response:
        result = void_document(
            _scope(request, tenant_id, company_id),
            document_id,
            revision=require_revision(request),
            key=require_idempotency_key(request),
            request_id=getattr(request, "request_id", None),
        )
        return Response(result, headers={"ETag": f'"{result["row_version"]}"'})
