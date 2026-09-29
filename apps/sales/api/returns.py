import logging
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

from apps.inventory.return_stock import dispose_return_stock
from apps.sales import return_services as services
from apps.sales.api.serializers import _non_blank
from apps.sales.api.views import _limit, _scope
from apps.sales.return_selectors import invoice_return_summary, list_returns, return_detail
from common.access.scopes import company_read_scope
from common.api.errors import APIError, Conflict
from common.api.headers import require_idempotency_key, require_revision


class ReturnLineSerializer(serializers.Serializer):
    sales_invoice_line_id = serializers.UUIDField()
    quantity = serializers.DecimalField(
        max_digits=20, decimal_places=6, min_value=Decimal(0), default=Decimal(0)
    )
    credit_amount = serializers.DecimalField(
        max_digits=20, decimal_places=6, min_value=Decimal("0.000001"), required=False
    )


class ReturnCreateSerializer(serializers.Serializer):
    return_no = serializers.CharField(max_length=100, validators=[_non_blank])
    kind = serializers.ChoiceField(choices=["return", "cashback"])
    return_date = serializers.DateField()
    reason = serializers.CharField(max_length=1000, validators=[_non_blank])
    lines = ReturnLineSerializer(many=True, min_length=1, max_length=50)


class ReturnPatchSerializer(serializers.Serializer):
    return_date = serializers.DateField(required=False)
    reason = serializers.CharField(max_length=1000, validators=[_non_blank], required=False)
    lines = ReturnLineSerializer(many=True, min_length=1, max_length=50, required=False)

    def validate(self, attrs: dict[str, object]) -> dict[str, object]:
        if not attrs:
            raise serializers.ValidationError("At least one change is required.")
        return attrs


class InspectionLineSerializer(serializers.Serializer):
    line_id = serializers.UUIDField()
    received_quantity = serializers.DecimalField(
        max_digits=20, decimal_places=6, min_value=Decimal("0.000001")
    )
    condition = serializers.ChoiceField(choices=["resellable", "damaged", "unusable"])
    disposition = serializers.ChoiceField(
        choices=["restock", "quarantine", "damaged", "supplier_return", "write_off"]
    )
    warehouse_id = serializers.UUIDField()
    loss_account_id = serializers.UUIDField(required=False)
    approve_write_off = serializers.BooleanField(default=False)


class InspectionSerializer(serializers.Serializer):
    lines = InspectionLineSerializer(many=True, min_length=1, max_length=50)


class ReturnPostSerializer(serializers.Serializer):
    fiscal_period_id = serializers.UUIDField()
    journal_id = serializers.UUIDField()


class ReturnRefundSerializer(ReturnPostSerializer):
    receipt_payment_id = serializers.UUIDField()
    cash_account_id = serializers.UUIDField()
    amount = serializers.DecimalField(
        max_digits=20, decimal_places=6, min_value=Decimal("0.000001")
    )
    refund_date = serializers.DateField()
    refund_method = serializers.ChoiceField(choices=["cash", "bank_transfer", "card", "other"])
    external_reference = serializers.CharField(max_length=255, required=False, allow_blank=True)


class ReplacementSerializer(ReturnPostSerializer):
    replacement_invoice_id = serializers.UUIDField()
    replacement_revision = serializers.IntegerField(min_value=1)
    warehouse_id = serializers.UUIDField(required=False)


class StockDispositionSerializer(ReturnPostSerializer):
    repair_job_id = serializers.UUIDField(required=False)
    line_id = serializers.UUIDField()
    from_warehouse_id = serializers.UUIDField()
    to_warehouse_id = serializers.UUIDField(required=False)
    quantity = serializers.DecimalField(
        max_digits=20, decimal_places=6, min_value=Decimal("0.000001")
    )
    action_date = serializers.DateField()
    reason = serializers.CharField(max_length=1000, validators=[_non_blank])
    loss_account_id = serializers.UUIDField(required=False)
    approve_write_off = serializers.BooleanField(default=False)


class ReturnStockDispositionView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        request=StockDispositionSerializer,
        responses=OpenApiTypes.OBJECT,
        operation_id="sales_return_stock_dispose",
    )
    def post(
        self, request: Request, tenant_id: uuid.UUID, company_id: uuid.UUID, return_id: uuid.UUID
    ) -> Response:
        serializer = StockDispositionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return Response(
            dispose_return_stock(
                _scope(request, tenant_id, company_id),
                return_id,
                serializer.validated_data,
                revision=require_revision(request),
                key=require_idempotency_key(request),
                request_id=getattr(request, "request_id", None),
            )
        )

    def handle_exception(self, exc: Exception) -> Response:
        if isinstance(exc, DatabaseError):
            exc = Conflict(
                "RETURN_STOCK_CONFLICT", "Stock disposition failed a database invariant."
            )
        return super().handle_exception(exc)


def _response(result: dict[str, Any], status: int = 200) -> Response:
    return Response(result, status=status, headers={"ETag": f'"{result["row_version"]}"'})


class ReturnAPIView(APIView):
    permission_classes = [IsAuthenticated]

    def handle_exception(self, exc: Exception) -> Response:
        if isinstance(exc, DatabaseError):
            logging.getLogger(__name__).warning("Return database invariant failed", exc_info=True)
            exc = Conflict(
                "RETURN_WRITE_CONFLICT",
                "The command failed a database return/settlement invariant.",
            )
        return super().handle_exception(exc)


class ReturnCollectionView(ReturnAPIView):
    @extend_schema(responses=OpenApiTypes.OBJECT, operation_id="sales_return_list")
    def get(
        self, request: Request, tenant_id: uuid.UUID, company_id: uuid.UUID, invoice_id: uuid.UUID
    ) -> Response:
        raw = request.query_params.get("cursor")
        try:
            after = uuid.UUID(raw) if raw else None
        except ValueError as exc:
            raise APIError(code="INVALID_CURSOR", message="Invalid return cursor.") from exc
        limit = _limit(request)
        with company_read_scope(
            _scope(request, tenant_id, company_id), permission="sales.return.view", module="sales"
        ):
            rows = list_returns(company_id, invoice_id, after=after, limit=limit + 1)
        return Response(
            {
                "results": rows[:limit],
                "next_cursor": rows[limit - 1]["id"] if len(rows) > limit else None,
            }
        )

    @extend_schema(
        request=ReturnCreateSerializer,
        responses={201: OpenApiTypes.OBJECT},
        operation_id="sales_return_create",
    )
    def post(
        self, request: Request, tenant_id: uuid.UUID, company_id: uuid.UUID, invoice_id: uuid.UUID
    ) -> Response:
        serializer = ReturnCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return _response(
            services.create_return(
                _scope(request, tenant_id, company_id),
                invoice_id,
                serializer.validated_data,
                key=require_idempotency_key(request),
                request_id=getattr(request, "request_id", None),
            ),
            201,
        )


class ReturnDetailView(ReturnAPIView):
    @extend_schema(responses=OpenApiTypes.OBJECT, operation_id="sales_return_retrieve")
    def get(
        self, request: Request, tenant_id: uuid.UUID, company_id: uuid.UUID, return_id: uuid.UUID
    ) -> Response:
        with company_read_scope(
            _scope(request, tenant_id, company_id), permission="sales.return.view", module="sales"
        ):
            result = return_detail(company_id, return_id)
        return _response(result)

    @extend_schema(
        request=ReturnPatchSerializer,
        responses=OpenApiTypes.OBJECT,
        operation_id="sales_return_update",
    )
    def patch(
        self, request: Request, tenant_id: uuid.UUID, company_id: uuid.UUID, return_id: uuid.UUID
    ) -> Response:
        serializer = ReturnPatchSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return _response(
            services.update_return(
                _scope(request, tenant_id, company_id),
                return_id,
                serializer.validated_data,
                revision=require_revision(request),
                request_id=getattr(request, "request_id", None),
            )
        )


class ReturnSummaryView(ReturnAPIView):
    @extend_schema(responses=OpenApiTypes.OBJECT, operation_id="sales_return_eligibility")
    def get(
        self, request: Request, tenant_id: uuid.UUID, company_id: uuid.UUID, invoice_id: uuid.UUID
    ) -> Response:
        with company_read_scope(
            _scope(request, tenant_id, company_id), permission="sales.return.view", module="sales"
        ):
            result = invoice_return_summary(company_id, invoice_id)
        return Response(result)


class ReturnVoidView(ReturnAPIView):
    @extend_schema(request=None, responses=OpenApiTypes.OBJECT, operation_id="sales_return_void")
    def post(
        self, request: Request, tenant_id: uuid.UUID, company_id: uuid.UUID, return_id: uuid.UUID
    ) -> Response:
        return _response(
            services.void_return(
                _scope(request, tenant_id, company_id),
                return_id,
                revision=require_revision(request),
                key=require_idempotency_key(request),
                request_id=getattr(request, "request_id", None),
            )
        )


class ReturnCreditApplicationSerializer(serializers.Serializer):
    target_invoice_id = serializers.UUIDField(required=False)
    effective_date = serializers.DateField(required=False)


class ReturnApplyCreditView(ReturnAPIView):
    @extend_schema(
        request=ReturnCreditApplicationSerializer,
        responses=OpenApiTypes.OBJECT,
        operation_id="sales_return_apply_credit",
    )
    def post(
        self, request: Request, tenant_id: uuid.UUID, company_id: uuid.UUID, return_id: uuid.UUID
    ) -> Response:
        serializer = ReturnCreditApplicationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return _response(
            services.apply_return_credit(
                _scope(request, tenant_id, company_id),
                return_id,
                revision=require_revision(request),
                key=require_idempotency_key(request),
                request_id=getattr(request, "request_id", None),
                **serializer.validated_data,
            )
        )


class ReturnInspectView(ReturnAPIView):
    @extend_schema(
        request=InspectionSerializer,
        responses=OpenApiTypes.OBJECT,
        operation_id="sales_return_inspect",
    )
    def post(
        self, request: Request, tenant_id: uuid.UUID, company_id: uuid.UUID, return_id: uuid.UUID
    ) -> Response:
        serializer = InspectionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return _response(
            services.inspect_return(
                _scope(request, tenant_id, company_id),
                return_id,
                serializer.validated_data,
                revision=require_revision(request),
                key=require_idempotency_key(request),
                request_id=getattr(request, "request_id", None),
            )
        )


class ReturnPostView(ReturnAPIView):
    @extend_schema(
        request=ReturnPostSerializer,
        responses=OpenApiTypes.OBJECT,
        operation_id="sales_return_post",
    )
    def post(
        self, request: Request, tenant_id: uuid.UUID, company_id: uuid.UUID, return_id: uuid.UUID
    ) -> Response:
        serializer = ReturnPostSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return _response(
            services.post_return(
                _scope(request, tenant_id, company_id),
                return_id,
                serializer.validated_data,
                revision=require_revision(request),
                key=require_idempotency_key(request),
                request_id=getattr(request, "request_id", None),
            )
        )


class ReturnRefundView(ReturnAPIView):
    @extend_schema(
        request=ReturnRefundSerializer,
        responses=OpenApiTypes.OBJECT,
        operation_id="sales_return_refund",
    )
    def post(
        self, request: Request, tenant_id: uuid.UUID, company_id: uuid.UUID, return_id: uuid.UUID
    ) -> Response:
        serializer = ReturnRefundSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return _response(
            services.refund_return(
                _scope(request, tenant_id, company_id),
                return_id,
                serializer.validated_data,
                revision=require_revision(request),
                key=require_idempotency_key(request),
                request_id=getattr(request, "request_id", None),
            )
        )


class ReturnReplacementView(ReturnAPIView):
    @extend_schema(
        request=ReplacementSerializer,
        responses=OpenApiTypes.OBJECT,
        operation_id="sales_return_replace",
    )
    def post(
        self, request: Request, tenant_id: uuid.UUID, company_id: uuid.UUID, return_id: uuid.UUID
    ) -> Response:
        serializer = ReplacementSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return _response(
            services.replace_return(
                _scope(request, tenant_id, company_id),
                return_id,
                serializer.validated_data,
                revision=require_revision(request),
                key=require_idempotency_key(request),
                request_id=getattr(request, "request_id", None),
            )
        )
