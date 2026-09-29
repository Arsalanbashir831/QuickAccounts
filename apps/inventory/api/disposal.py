import uuid
from decimal import Decimal
from typing import Any

from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.request import Request
from rest_framework.response import Response

from apps.inventory.api.serializers import _non_blank
from apps.inventory.api.stock import StockAPIView
from apps.inventory.api.views import _limit, _scope
from apps.inventory.disposal_cases import case_detail, create_case, list_cases, transition_case
from common.access.scopes import company_read_scope
from common.api.errors import APIError
from common.api.headers import require_idempotency_key, require_revision


class DisposalCreateSerializer(serializers.Serializer[dict[str, Any]]):
    case_no = serializers.CharField(max_length=100, validators=[_non_blank])
    item_id = serializers.UUIDField()
    warehouse_id = serializers.UUIDField()
    quantity = serializers.DecimalField(
        max_digits=20, decimal_places=6, min_value=Decimal("0.000001")
    )
    reason = serializers.CharField(max_length=1000, validators=[_non_blank])
    method = serializers.ChoiceField(
        choices=["repair_later", "supplier_claim", "liquidation", "write_off"]
    )
    sales_return_line_id = serializers.UUIDField(required=False)


class DisposalTransitionSerializer(serializers.Serializer[dict[str, Any]]):
    action = serializers.ChoiceField(choices=["approve", "execute", "void", "link-sale"])
    approve_loss = serializers.BooleanField(default=False)
    approve_quality_release = serializers.BooleanField(default=False)
    qc_notes = serializers.CharField(max_length=1000, validators=[_non_blank], required=False)
    to_warehouse_id = serializers.UUIDField(required=False)
    repair_job_id = serializers.UUIDField(required=False)
    journal_id = serializers.UUIDField(required=False)
    fiscal_period_id = serializers.UUIDField(required=False)
    loss_account_id = serializers.UUIDField(required=False)
    action_date = serializers.DateField(required=False)
    sales_invoice_id = serializers.UUIDField(required=False)

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        if (
            attrs["action"] == "execute"
            and not {"action_date", "fiscal_period_id", "journal_id"} <= attrs.keys()
        ):
            raise serializers.ValidationError("Execution requires date, period and journal.")
        if attrs["action"] == "link-sale" and "sales_invoice_id" not in attrs:
            raise serializers.ValidationError("A posted clearance invoice is required.")
        return attrs


class DisposalCollectionView(StockAPIView):
    @extend_schema(responses=OpenApiTypes.OBJECT, operation_id="stock_disposal_list")
    def get(self, request: Request, tenant_id: uuid.UUID, company_id: uuid.UUID) -> Response:
        try:
            after = (
                uuid.UUID(request.query_params["cursor"])
                if "cursor" in request.query_params
                else None
            )
        except ValueError as exc:
            raise APIError(code="INVALID_CURSOR", message="Invalid disposal cursor.") from exc
        limit = _limit(request)
        with company_read_scope(
            _scope(request, tenant_id, company_id), permission="inventory.view", module="inventory"
        ):
            rows = list_cases(company_id, after, limit + 1)
        return Response(
            {
                "results": rows[:limit],
                "next_cursor": rows[limit - 1]["id"] if len(rows) > limit else None,
            }
        )

    @extend_schema(
        request=DisposalCreateSerializer,
        responses={201: OpenApiTypes.OBJECT},
        operation_id="stock_disposal_create",
    )
    def post(self, request: Request, tenant_id: uuid.UUID, company_id: uuid.UUID) -> Response:
        serializer = DisposalCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return Response(
            create_case(
                _scope(request, tenant_id, company_id),
                serializer.validated_data,
                key=require_idempotency_key(request),
                request_id=getattr(request, "request_id", None),
            ),
            status=201,
        )


class DisposalDetailView(StockAPIView):
    @extend_schema(responses=OpenApiTypes.OBJECT, operation_id="stock_disposal_detail")
    def get(
        self, request: Request, tenant_id: uuid.UUID, company_id: uuid.UUID, case_id: uuid.UUID
    ) -> Response:
        with company_read_scope(
            _scope(request, tenant_id, company_id), permission="inventory.view", module="inventory"
        ):
            result = case_detail(company_id, case_id)
        return Response(result)

    @extend_schema(
        request=DisposalTransitionSerializer,
        responses=OpenApiTypes.OBJECT,
        operation_id="stock_disposal_transition",
    )
    def post(
        self, request: Request, tenant_id: uuid.UUID, company_id: uuid.UUID, case_id: uuid.UUID
    ) -> Response:
        serializer = DisposalTransitionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return Response(
            transition_case(
                _scope(request, tenant_id, company_id),
                case_id,
                serializer.validated_data,
                revision=require_revision(request),
                key=require_idempotency_key(request),
                request_id=getattr(request, "request_id", None),
            )
        )
