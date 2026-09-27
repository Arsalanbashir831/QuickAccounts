import uuid
from typing import cast

from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.purchasing.api.serializers import (
    PurchaseBillCreateSerializer,
    PurchaseBillPatchSerializer,
    PurchaseBillPostSerializer,
    SupplierCreditCreateSerializer,
)
from apps.purchasing.selectors import list_purchase_bills, purchase_bill_detail
from apps.purchasing.services import (
    calculate_purchase_bill,
    create_linked_supplier_credit,
    create_purchase_bill,
    post_purchase_bill,
    update_purchase_bill,
)
from common.access.scopes import CompanyScope, company_read_scope
from common.api.errors import APIError, ScopeNotFound
from common.api.headers import require_idempotency_key, require_revision


def _scope(request: Request, tenant_id: uuid.UUID, company_id: uuid.UUID) -> CompanyScope:
    return CompanyScope(
        tenant_id=tenant_id,
        company_id=company_id,
        user_id=cast(uuid.UUID, request.user.pk),
    )


def _limit(request: Request) -> int:
    try:
        value = int(request.query_params.get("limit", "50"))
    except ValueError as exc:
        raise APIError(code="INVALID_LIMIT", message="Limit must be an integer.") from exc
    if not 1 <= value <= 200:
        raise APIError(code="INVALID_LIMIT", message="Limit must be between 1 and 200.")
    return value


class PurchaseBillCollectionView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(responses=OpenApiTypes.OBJECT, operation_id="purchase_bill_list")
    def get(self, request: Request, tenant_id: uuid.UUID, company_id: uuid.UUID) -> Response:
        bill_status = request.query_params.get("status")
        if bill_status is not None and bill_status not in {"draft", "posted", "void"}:
            raise APIError(code="INVALID_FILTER", message="Unsupported bill status filter.")
        raw_supplier_id = request.query_params.get("supplier_id")
        try:
            supplier_id = uuid.UUID(raw_supplier_id) if raw_supplier_id else None
        except ValueError as exc:
            raise APIError(code="INVALID_FILTER", message="supplier_id must be a UUID.") from exc
        scope = _scope(request, tenant_id, company_id)
        with company_read_scope(scope, permission="purchasing.bill.view", module="purchasing"):
            rows, next_cursor = list_purchase_bills(
                company_id,
                limit=_limit(request),
                cursor=request.query_params.get("cursor"),
                status=bill_status,
                supplier_id=supplier_id,
            )
        return Response({"results": rows, "next_cursor": next_cursor})

    @extend_schema(
        request=PurchaseBillCreateSerializer,
        responses={201: OpenApiTypes.OBJECT},
        operation_id="purchase_bill_create",
    )
    def post(self, request: Request, tenant_id: uuid.UUID, company_id: uuid.UUID) -> Response:
        serializer = PurchaseBillCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        result = create_purchase_bill(
            _scope(request, tenant_id, company_id),
            serializer.validated_data,
            request_id=getattr(request, "request_id", None),
        )
        return Response(
            result,
            status=status.HTTP_201_CREATED,
            headers={"ETag": f'"{result["row_version"]}"'},
        )


class PurchaseBillDetailView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(responses=OpenApiTypes.OBJECT, operation_id="purchase_bill_retrieve")
    def get(
        self, request: Request, tenant_id: uuid.UUID, company_id: uuid.UUID, bill_id: uuid.UUID
    ) -> Response:
        scope = _scope(request, tenant_id, company_id)
        with company_read_scope(scope, permission="purchasing.bill.view", module="purchasing"):
            result = purchase_bill_detail(company_id, bill_id)
            if result is None:
                raise ScopeNotFound()
        return Response(result, headers={"ETag": f'"{result["row_version"]}"'})

    @extend_schema(
        request=PurchaseBillPatchSerializer,
        responses=OpenApiTypes.OBJECT,
        operation_id="purchase_bill_update",
    )
    def patch(
        self, request: Request, tenant_id: uuid.UUID, company_id: uuid.UUID, bill_id: uuid.UUID
    ) -> Response:
        serializer = PurchaseBillPatchSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        result = update_purchase_bill(
            _scope(request, tenant_id, company_id),
            bill_id,
            serializer.validated_data,
            expected_revision=require_revision(request),
            request_id=getattr(request, "request_id", None),
        )
        return Response(result, headers={"ETag": f'"{result["row_version"]}"'})


class PurchaseBillCalculateView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        request=None,
        responses=OpenApiTypes.OBJECT,
        operation_id="purchase_bill_calculate",
    )
    def post(
        self, request: Request, tenant_id: uuid.UUID, company_id: uuid.UUID, bill_id: uuid.UUID
    ) -> Response:
        result = calculate_purchase_bill(
            _scope(request, tenant_id, company_id),
            bill_id,
            expected_revision=require_revision(request),
            request_id=getattr(request, "request_id", None),
        )
        return Response(result, headers={"ETag": f'"{result["row_version"]}"'})


class PurchaseBillPostView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        request=PurchaseBillPostSerializer,
        responses=OpenApiTypes.OBJECT,
        operation_id="purchase_bill_post",
    )
    def post(
        self,
        request: Request,
        tenant_id: uuid.UUID,
        company_id: uuid.UUID,
        bill_id: uuid.UUID,
    ) -> Response:
        serializer = PurchaseBillPostSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        result, response_status = post_purchase_bill(
            _scope(request, tenant_id, company_id),
            bill_id,
            serializer.validated_data,
            expected_revision=require_revision(request),
            idempotency_key=require_idempotency_key(request),
            request_id=getattr(request, "request_id", None),
        )
        return Response(
            result,
            status=response_status,
            headers={"ETag": f'"{result["row_version"]}"'},
        )


class SupplierCreditCollectionView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        request=SupplierCreditCreateSerializer,
        responses={201: OpenApiTypes.OBJECT},
        operation_id="purchase_bill_supplier_credit_create",
    )
    def post(
        self,
        request: Request,
        tenant_id: uuid.UUID,
        company_id: uuid.UUID,
        bill_id: uuid.UUID,
    ) -> Response:
        serializer = SupplierCreditCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        result = create_linked_supplier_credit(
            _scope(request, tenant_id, company_id),
            bill_id,
            serializer.validated_data,
            request_id=getattr(request, "request_id", None),
        )
        return Response(
            result,
            status=status.HTTP_201_CREATED,
            headers={"ETag": f'"{result["row_version"]}"'},
        )
