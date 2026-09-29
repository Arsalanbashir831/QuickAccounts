import uuid
from typing import Any

from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.request import Request
from rest_framework.response import Response

from apps.inventory.api.stock import StockAPIView
from apps.purchasing.api.serializers import _non_blank
from apps.purchasing.api.views import _limit, _scope
from apps.purchasing.claims import claim_detail, command_claim, create_claim, list_claims
from common.access.scopes import company_read_scope
from common.api.errors import APIError
from common.api.headers import require_idempotency_key, require_revision


class SupplierClaimCreateSerializer(serializers.Serializer[dict[str, Any]]):
    claim_no = serializers.CharField(max_length=100, validators=[_non_blank])
    source_bill_line_id = serializers.UUIDField()
    warehouse_id = serializers.UUIDField()
    disposal_case_id = serializers.UUIDField(required=False)
    reason = serializers.CharField(max_length=1000, validators=[_non_blank])


class SupplierClaimCommandSerializer(serializers.Serializer[dict[str, Any]]):
    action = serializers.ChoiceField(choices=["approve", "reject", "settle"])
    approve_supplier_claim = serializers.BooleanField(default=False)
    supplier_credit_id = serializers.UUIDField(required=False)

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        if attrs["action"] == "settle" and "supplier_credit_id" not in attrs:
            raise serializers.ValidationError("Settlement requires a posted supplier credit.")
        return attrs


class SupplierClaimCollectionView(StockAPIView):
    @extend_schema(responses=OpenApiTypes.OBJECT, operation_id="supplier_claim_list")
    def get(self, request: Request, tenant_id: uuid.UUID, company_id: uuid.UUID) -> Response:
        limit = _limit(request)
        try:
            after = (
                uuid.UUID(request.query_params["cursor"])
                if "cursor" in request.query_params
                else None
            )
        except ValueError as exc:
            raise APIError(code="INVALID_CURSOR", message="Invalid claim cursor.") from exc
        with company_read_scope(
            _scope(request, tenant_id, company_id),
            permission="purchasing.bill.view",
            module="purchasing",
        ):
            rows = list_claims(company_id, after, limit + 1)
        return Response(
            {
                "results": rows[:limit],
                "next_cursor": rows[limit - 1]["id"] if len(rows) > limit else None,
            }
        )

    @extend_schema(
        request=SupplierClaimCreateSerializer,
        responses={201: OpenApiTypes.OBJECT},
        operation_id="supplier_claim_create",
    )
    def post(self, request: Request, tenant_id: uuid.UUID, company_id: uuid.UUID) -> Response:
        serializer = SupplierClaimCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return Response(
            create_claim(
                _scope(request, tenant_id, company_id),
                serializer.validated_data,
                key=require_idempotency_key(request),
                request_id=getattr(request, "request_id", None),
            ),
            status=201,
        )


class SupplierClaimDetailView(StockAPIView):
    @extend_schema(responses=OpenApiTypes.OBJECT, operation_id="supplier_claim_detail")
    def get(
        self, request: Request, tenant_id: uuid.UUID, company_id: uuid.UUID, claim_id: uuid.UUID
    ) -> Response:
        with company_read_scope(
            _scope(request, tenant_id, company_id),
            permission="purchasing.bill.view",
            module="purchasing",
        ):
            result = claim_detail(company_id, claim_id)
        return Response(result)

    @extend_schema(
        request=SupplierClaimCommandSerializer,
        responses=OpenApiTypes.OBJECT,
        operation_id="supplier_claim_command",
    )
    def post(
        self, request: Request, tenant_id: uuid.UUID, company_id: uuid.UUID, claim_id: uuid.UUID
    ) -> Response:
        serializer = SupplierClaimCommandSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return Response(
            command_claim(
                _scope(request, tenant_id, company_id),
                claim_id,
                serializer.validated_data,
                revision=require_revision(request),
                key=require_idempotency_key(request),
                request_id=getattr(request, "request_id", None),
            )
        )
