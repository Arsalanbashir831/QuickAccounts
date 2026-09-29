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
from apps.inventory.custody import intake_detail, list_intakes, receive_intake, transition_intake
from common.access.scopes import company_read_scope
from common.api.errors import APIError
from common.api.headers import require_idempotency_key, require_revision


class IntakeCreateSerializer(serializers.Serializer[dict[str, Any]]):
    intake_no = serializers.CharField(max_length=100, validators=[_non_blank])
    item_id = serializers.UUIDField()
    warehouse_id = serializers.UUIDField()
    quantity = serializers.DecimalField(
        max_digits=20, decimal_places=6, min_value=Decimal("0.000001")
    )
    received_date = serializers.DateField()
    reason = serializers.CharField(max_length=1000, validators=[_non_blank])
    customer_reference = serializers.CharField(max_length=255, required=False)
    batch_serial_reference = serializers.CharField(max_length=255, required=False)


class IntakeTransitionSerializer(serializers.Serializer[dict[str, Any]]):
    status = serializers.ChoiceField(choices=["inspected", "matched", "rejected"])
    condition = serializers.ChoiceField(
        choices=["resellable", "damaged", "unusable"], required=False
    )
    requested_resolution = serializers.ChoiceField(
        choices=["bill_back", "replacement", "credit"], required=False
    )
    inspection_notes = serializers.CharField(
        max_length=1000, validators=[_non_blank], required=False
    )
    sales_return_line_id = serializers.UUIDField(required=False)
    return_revision = serializers.IntegerField(min_value=1, required=False)

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        if (
            attrs["status"] == "matched"
            and not {"sales_return_line_id", "return_revision"} <= attrs.keys()
        ):
            raise serializers.ValidationError("Matching requires a return line and its revision.")
        if attrs["status"] != "matched" and "sales_return_line_id" in attrs:
            raise serializers.ValidationError("Only matching can link a verified sale.")
        return attrs


class IntakeCollectionView(StockAPIView):
    @extend_schema(responses=OpenApiTypes.OBJECT, operation_id="return_custody_list")
    def get(self, request: Request, tenant_id: uuid.UUID, company_id: uuid.UUID) -> Response:
        try:
            after = (
                uuid.UUID(request.query_params["cursor"])
                if "cursor" in request.query_params
                else None
            )
            warehouse = (
                uuid.UUID(request.query_params["warehouse_id"])
                if "warehouse_id" in request.query_params
                else None
            )
        except ValueError as exc:
            raise APIError(
                code="INVALID_FILTER", message="Invalid custody cursor/warehouse."
            ) from exc
        limit = _limit(request)
        with company_read_scope(
            _scope(request, tenant_id, company_id), permission="inventory.view", module="inventory"
        ):
            rows = list_intakes(company_id, after, limit + 1, warehouse)
        return Response(
            {
                "results": rows[:limit],
                "owned_inventory_effect": False,
                "next_cursor": rows[limit - 1]["id"] if len(rows) > limit else None,
            }
        )

    @extend_schema(
        request=IntakeCreateSerializer,
        responses={201: OpenApiTypes.OBJECT},
        operation_id="return_custody_receive",
    )
    def post(self, request: Request, tenant_id: uuid.UUID, company_id: uuid.UUID) -> Response:
        serializer = IntakeCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        result = receive_intake(
            _scope(request, tenant_id, company_id),
            serializer.validated_data,
            key=require_idempotency_key(request),
            request_id=getattr(request, "request_id", None),
        )
        return Response(result, status=201)


class IntakeDetailView(StockAPIView):
    @extend_schema(responses=OpenApiTypes.OBJECT, operation_id="return_custody_detail")
    def get(
        self, request: Request, tenant_id: uuid.UUID, company_id: uuid.UUID, intake_id: uuid.UUID
    ) -> Response:
        with company_read_scope(
            _scope(request, tenant_id, company_id), permission="inventory.view", module="inventory"
        ):
            result = intake_detail(company_id, intake_id)
        return Response(result)

    @extend_schema(
        request=IntakeTransitionSerializer,
        responses=OpenApiTypes.OBJECT,
        operation_id="return_custody_transition",
    )
    def post(
        self, request: Request, tenant_id: uuid.UUID, company_id: uuid.UUID, intake_id: uuid.UUID
    ) -> Response:
        serializer = IntakeTransitionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        result = transition_intake(
            _scope(request, tenant_id, company_id),
            intake_id,
            serializer.validated_data,
            revision=require_revision(request),
            key=require_idempotency_key(request),
            request_id=getattr(request, "request_id", None),
        )
        return Response(result)
