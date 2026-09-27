import uuid
from typing import cast

from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.sales.api.serializers import (
    SalesCreditNoteCreateSerializer,
    SalesInvoiceCreateSerializer,
    SalesInvoicePatchSerializer,
    SalesInvoicePostSerializer,
)
from apps.sales.selectors import list_sales_invoices, sales_invoice_detail
from apps.sales.services import (
    calculate_sales_invoice,
    create_linked_credit_note,
    create_sales_invoice,
    post_sales_invoice,
    update_sales_invoice,
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


class SalesInvoiceCollectionView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(responses=OpenApiTypes.OBJECT, operation_id="sales_invoice_list")
    def get(self, request: Request, tenant_id: uuid.UUID, company_id: uuid.UUID) -> Response:
        invoice_status = request.query_params.get("status")
        if invoice_status is not None and invoice_status not in {"draft", "posted", "void"}:
            raise APIError(code="INVALID_FILTER", message="Unsupported invoice status filter.")
        raw_partner_id = request.query_params.get("partner_id")
        try:
            partner_id = uuid.UUID(raw_partner_id) if raw_partner_id else None
        except ValueError as exc:
            raise APIError(code="INVALID_FILTER", message="partner_id must be a UUID.") from exc
        scope = _scope(request, tenant_id, company_id)
        with company_read_scope(scope, permission="sales.invoice.view", module="sales"):
            rows, next_cursor = list_sales_invoices(
                company_id,
                limit=_limit(request),
                cursor=request.query_params.get("cursor"),
                status=invoice_status,
                partner_id=partner_id,
            )
        return Response({"results": rows, "next_cursor": next_cursor})

    @extend_schema(
        request=SalesInvoiceCreateSerializer,
        responses={201: OpenApiTypes.OBJECT},
        operation_id="sales_invoice_create",
    )
    def post(self, request: Request, tenant_id: uuid.UUID, company_id: uuid.UUID) -> Response:
        serializer = SalesInvoiceCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        result = create_sales_invoice(
            _scope(request, tenant_id, company_id),
            serializer.validated_data,
            request_id=getattr(request, "request_id", None),
        )
        return Response(
            result,
            status=status.HTTP_201_CREATED,
            headers={"ETag": f'"{result["row_version"]}"'},
        )


class SalesInvoiceDetailView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(responses=OpenApiTypes.OBJECT, operation_id="sales_invoice_retrieve")
    def get(
        self, request: Request, tenant_id: uuid.UUID, company_id: uuid.UUID, invoice_id: uuid.UUID
    ) -> Response:
        scope = _scope(request, tenant_id, company_id)
        with company_read_scope(scope, permission="sales.invoice.view", module="sales"):
            result = sales_invoice_detail(company_id, invoice_id)
            if result is None:
                raise ScopeNotFound()
        return Response(result, headers={"ETag": f'"{result["row_version"]}"'})

    @extend_schema(
        request=SalesInvoicePatchSerializer,
        responses=OpenApiTypes.OBJECT,
        operation_id="sales_invoice_update",
    )
    def patch(
        self, request: Request, tenant_id: uuid.UUID, company_id: uuid.UUID, invoice_id: uuid.UUID
    ) -> Response:
        serializer = SalesInvoicePatchSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        result = update_sales_invoice(
            _scope(request, tenant_id, company_id),
            invoice_id,
            serializer.validated_data,
            expected_revision=require_revision(request),
            request_id=getattr(request, "request_id", None),
        )
        return Response(result, headers={"ETag": f'"{result["row_version"]}"'})


class SalesInvoiceCalculateView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        request=None,
        responses=OpenApiTypes.OBJECT,
        operation_id="sales_invoice_calculate",
    )
    def post(
        self, request: Request, tenant_id: uuid.UUID, company_id: uuid.UUID, invoice_id: uuid.UUID
    ) -> Response:
        result = calculate_sales_invoice(
            _scope(request, tenant_id, company_id),
            invoice_id,
            expected_revision=require_revision(request),
            request_id=getattr(request, "request_id", None),
        )
        return Response(result, headers={"ETag": f'"{result["row_version"]}"'})


class SalesInvoicePostView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        request=SalesInvoicePostSerializer,
        responses=OpenApiTypes.OBJECT,
        operation_id="sales_invoice_post",
    )
    def post(
        self,
        request: Request,
        tenant_id: uuid.UUID,
        company_id: uuid.UUID,
        invoice_id: uuid.UUID,
    ) -> Response:
        serializer = SalesInvoicePostSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        result, response_status = post_sales_invoice(
            _scope(request, tenant_id, company_id),
            invoice_id,
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


class SalesCreditNoteCollectionView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        request=SalesCreditNoteCreateSerializer,
        responses={201: OpenApiTypes.OBJECT},
        operation_id="sales_invoice_credit_note_create",
    )
    def post(
        self,
        request: Request,
        tenant_id: uuid.UUID,
        company_id: uuid.UUID,
        invoice_id: uuid.UUID,
    ) -> Response:
        serializer = SalesCreditNoteCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        result = create_linked_credit_note(
            _scope(request, tenant_id, company_id),
            invoice_id,
            serializer.validated_data,
            request_id=getattr(request, "request_id", None),
        )
        return Response(
            result,
            status=status.HTTP_201_CREATED,
            headers={"ETag": f'"{result["row_version"]}"'},
        )
