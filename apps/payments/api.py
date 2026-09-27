import datetime as dt
import uuid
from typing import cast

from django.db import DatabaseError
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.payments import services
from apps.payments.reports import open_items
from apps.payments.serializers import (
    PaymentAllocationsSerializer,
    PaymentCreateSerializer,
    PaymentPatchSerializer,
    PaymentPostSerializer,
    PaymentReverseSerializer,
    PaymentWithholdingSerializer,
)
from common.access.scopes import CompanyScope, company_read_scope
from common.api.errors import APIError, Conflict
from common.api.headers import require_idempotency_key, require_revision


def _scope(request: Request, tenant_id: uuid.UUID, company_id: uuid.UUID) -> CompanyScope:
    return CompanyScope(
        tenant_id=tenant_id, company_id=company_id, user_id=cast(uuid.UUID, request.user.pk)
    )


def _response(body: dict[str, object], status: int = 200) -> Response:
    return Response(body, status=status, headers={"ETag": f'"{body["row_version"]}"'})


def _limit(request: Request) -> int:
    try:
        limit = int(request.query_params.get("limit", 50))
        if not 1 <= limit <= 200:
            raise ValueError
        return limit
    except ValueError as exc:
        raise APIError(code="INVALID_LIMIT", message="Limit must be between 1 and 200.") from exc


class PaymentAPIView(APIView):
    def handle_exception(self, exc: Exception) -> Response:
        if isinstance(exc, DatabaseError):
            exc = Conflict(
                "PAYMENT_WRITE_CONFLICT", "The payment conflicts with database invariants."
            )
        return super().handle_exception(exc)


class PaymentCollectionView(PaymentAPIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(responses=OpenApiTypes.OBJECT, operation_id="payment_list")
    def get(self, request: Request, tenant_id: uuid.UUID, company_id: uuid.UUID) -> Response:
        status = request.query_params.get("status")
        if status is not None and status not in {"draft", "posted", "void"}:
            raise APIError(code="INVALID_FILTER", message="Unsupported payment status.")
        try:
            after = (
                uuid.UUID(request.query_params["cursor"])
                if "cursor" in request.query_params
                else None
            )
        except ValueError as exc:
            raise APIError(code="INVALID_CURSOR", message="Invalid cursor.") from exc
        limit = _limit(request)
        with company_read_scope(
            _scope(request, tenant_id, company_id), permission="payments.view", module="payments"
        ):
            rows = services.list_payments(company_id, limit=limit + 1, status=status, after=after)
        return Response(
            {
                "results": rows[:limit],
                "next_cursor": rows[limit - 1]["id"] if len(rows) > limit else None,
            }
        )

    @extend_schema(request=PaymentCreateSerializer, responses={201: OpenApiTypes.OBJECT})
    def post(self, request: Request, tenant_id: uuid.UUID, company_id: uuid.UUID) -> Response:
        serializer = PaymentCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        result = services.create_payment(
            _scope(request, tenant_id, company_id),
            serializer.validated_data,
            request_id=getattr(request, "request_id", None),
            idempotency_key=require_idempotency_key(request),
        )
        return _response(result, 201)


class PaymentDetailView(PaymentAPIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(responses=OpenApiTypes.OBJECT, operation_id="payment_retrieve")
    def get(
        self, request: Request, tenant_id: uuid.UUID, company_id: uuid.UUID, payment_id: uuid.UUID
    ) -> Response:
        with company_read_scope(
            _scope(request, tenant_id, company_id), permission="payments.view", module="payments"
        ):
            body = services.payment_detail(company_id, payment_id)
        return _response(body)

    @extend_schema(request=PaymentPatchSerializer, responses=OpenApiTypes.OBJECT)
    def patch(
        self, request: Request, tenant_id: uuid.UUID, company_id: uuid.UUID, payment_id: uuid.UUID
    ) -> Response:
        serializer = PaymentPatchSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        return _response(
            services.update_payment(
                _scope(request, tenant_id, company_id),
                payment_id,
                serializer.validated_data,
                expected_revision=require_revision(request),
                request_id=getattr(request, "request_id", None),
            )
        )


class PaymentAllocationView(PaymentAPIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(responses=OpenApiTypes.OBJECT)
    def get(
        self, request: Request, tenant_id: uuid.UUID, company_id: uuid.UUID, payment_id: uuid.UUID
    ) -> Response:
        with company_read_scope(
            _scope(request, tenant_id, company_id), permission="payments.view", module="payments"
        ):
            body = services.payment_detail(company_id, payment_id)
        return Response({"results": body["allocations"]})

    @extend_schema(request=PaymentAllocationsSerializer, responses=OpenApiTypes.OBJECT)
    def put(
        self, request: Request, tenant_id: uuid.UUID, company_id: uuid.UUID, payment_id: uuid.UUID
    ) -> Response:
        serializer = PaymentAllocationsSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return _response(
            services.replace_allocations(
                _scope(request, tenant_id, company_id),
                payment_id,
                serializer.validated_data["allocations"],
                expected_revision=require_revision(request),
                request_id=getattr(request, "request_id", None),
            )
        )


class PaymentWithholdingView(PaymentAPIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(request=PaymentWithholdingSerializer, responses=OpenApiTypes.OBJECT)
    def put(
        self, request: Request, tenant_id: uuid.UUID, company_id: uuid.UUID, payment_id: uuid.UUID
    ) -> Response:
        serializer = PaymentWithholdingSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return _response(
            services.calculate_withholding(
                _scope(request, tenant_id, company_id),
                payment_id,
                serializer.validated_data["components"],
                expected_revision=require_revision(request),
                request_id=getattr(request, "request_id", None),
            )
        )


class PaymentPostView(PaymentAPIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(request=PaymentPostSerializer, responses=OpenApiTypes.OBJECT)
    def post(
        self, request: Request, tenant_id: uuid.UUID, company_id: uuid.UUID, payment_id: uuid.UUID
    ) -> Response:
        serializer = PaymentPostSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return _response(
            services.post_payment(
                _scope(request, tenant_id, company_id),
                payment_id,
                **serializer.validated_data,
                expected_revision=require_revision(request),
                idempotency_key=require_idempotency_key(request),
                request_id=getattr(request, "request_id", None),
            )
        )


class PaymentReverseView(PaymentAPIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(request=PaymentReverseSerializer, responses=OpenApiTypes.OBJECT)
    def post(
        self, request: Request, tenant_id: uuid.UUID, company_id: uuid.UUID, payment_id: uuid.UUID
    ) -> Response:
        serializer = PaymentReverseSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return _response(
            services.reverse_payment(
                _scope(request, tenant_id, company_id),
                payment_id,
                serializer.validated_data,
                expected_revision=require_revision(request),
                idempotency_key=require_idempotency_key(request),
                request_id=getattr(request, "request_id", None),
            )
        )


class OpenItemsView(APIView):
    permission_classes = [IsAuthenticated]
    direction = "receipt"
    aging = False

    @extend_schema(responses=OpenApiTypes.OBJECT)
    def get(self, request: Request, tenant_id: uuid.UUID, company_id: uuid.UUID) -> Response:
        try:
            cutoff = dt.date.fromisoformat(
                request.query_params.get("cutoff", dt.date.today().isoformat())
            )
            after = (
                uuid.UUID(request.query_params["cursor"])
                if "cursor" in request.query_params
                else None
            )
        except ValueError as exc:
            raise APIError(code="INVALID_FILTER", message="Invalid cutoff or cursor.") from exc
        with company_read_scope(_scope(request, tenant_id, company_id), permission="reports.view"):
            rows = open_items(
                company_id,
                self.direction,
                cutoff=cutoff,
                limit=_limit(request),
                after=after,
                aging=self.aging,
            )
        return Response(
            {
                "cutoff": cutoff.isoformat(),
                "results": rows,
                "next_cursor": rows[-1]["id"] if len(rows) == _limit(request) else None,
            }
        )
