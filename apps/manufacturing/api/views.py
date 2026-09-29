import logging
import uuid
from typing import Any

from django.db import DatabaseError
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.inventory.api.views import _scope
from apps.manufacturing.api.serializers import (
    BomSerializer,
    EmptySerializer,
    ExecutionCancelSerializer,
    ListQuerySerializer,
    MaterialIssueSerializer,
    OrderSerializer,
    OutputSerializer,
    ReasonSerializer,
    ReleaseSerializer,
)
from apps.manufacturing.execution import (
    cancel_execution,
    complete_order,
    issue_materials,
    receive_output,
    release_order,
)
from apps.manufacturing.selectors import bom_detail, list_documents, order_detail
from apps.manufacturing.services import save_bom, save_order, transition_bom
from common.access.scopes import company_read_scope
from common.api.errors import APIError, Conflict
from common.api.headers import require_idempotency_key, require_revision


def _response(result: dict[str, Any], status: int = 200) -> Response:
    return Response(result, status=status, headers={"ETag": f'"{result["row_version"]}"'})


class ManufacturingAPIView(APIView):
    permission_classes = [IsAuthenticated]

    def handle_exception(self, exc: Exception) -> Response:
        if isinstance(exc, DatabaseError):
            cause = getattr(exc, "__cause__", None)
            diagnostic = getattr(cause, "diag", None)
            constraint = getattr(diagnostic, "constraint_name", None)
            message = str(getattr(diagnostic, "message_primary", ""))
            code, _, detail = message.partition(":")
            if code.startswith("MFG_") and code.replace("_", "").isalnum():
                exc = Conflict(code, detail.strip())
            elif constraint == "boms_company_id_output_item_id_revision_key":
                exc = Conflict(
                    "MFG_BOM_REVISION_EXISTS", "This item already has that BOM revision."
                )
            elif constraint == "production_orders_company_id_production_no_key":
                exc = Conflict("MFG_PRODUCTION_NUMBER_EXISTS", "Production number already exists.")
            else:
                logging.getLogger(__name__).warning(
                    "Manufacturing database invariant failed", exc_info=True
                )
                exc = Conflict(
                    "MFG_WRITE_CONFLICT", "Manufacturing command conflicts with an invariant."
                )
        return super().handle_exception(exc)


class BomCollectionView(ManufacturingAPIView):
    @extend_schema(
        operation_id="manufacturing_boms_list",
        parameters=[ListQuerySerializer],
        responses=OpenApiTypes.OBJECT,
    )
    def get(self, request: Request, tenant_id: uuid.UUID, company_id: uuid.UUID) -> Response:
        return _list(request, tenant_id, company_id, "boms")

    @extend_schema(request=BomSerializer, responses={201: OpenApiTypes.OBJECT})
    def post(self, request: Request, tenant_id: uuid.UUID, company_id: uuid.UUID) -> Response:
        serializer = BomSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return _response(
            save_bom(
                _scope(request, tenant_id, company_id),
                serializer.validated_data,
                key=require_idempotency_key(request),
                request_id=getattr(request, "request_id", None),
            ),
            201,
        )


def _list(request: Request, tenant_id: uuid.UUID, company_id: uuid.UUID, resource: str) -> Response:
    query = ListQuerySerializer(data=request.query_params)
    query.is_valid(raise_exception=True)
    values = query.validated_data
    statuses = (
        {"draft", "active", "retired"}
        if resource == "boms"
        else {"planned", "released", "in_progress", "completed", "cancelled"}
    )
    if values.get("status") is not None and values["status"] not in statuses:
        raise APIError(code="INVALID_FILTER", message="Unsupported manufacturing status.")
    permission = "manufacturing.bom.view" if resource == "boms" else "manufacturing.order.view"
    with company_read_scope(
        _scope(request, tenant_id, company_id), permission=permission, module="manufacturing"
    ):
        rows = list_documents(
            company_id,
            resource,
            after=values.get("cursor"),
            limit=values["limit"],
            status=values.get("status"),
            output_item_id=values.get("output_item_id"),
        )
    limit = values["limit"]
    return Response(
        {
            "results": rows[:limit],
            "next_cursor": rows[limit - 1]["id"] if len(rows) > limit else None,
        }
    )


class BomDetailView(ManufacturingAPIView):
    @extend_schema(responses=OpenApiTypes.OBJECT)
    def get(
        self, request: Request, tenant_id: uuid.UUID, company_id: uuid.UUID, bom_id: uuid.UUID
    ) -> Response:
        with company_read_scope(
            _scope(request, tenant_id, company_id),
            permission="manufacturing.bom.view",
            module="manufacturing",
        ):
            result = bom_detail(company_id, bom_id)
        return _response(result)

    @extend_schema(request=BomSerializer, responses=OpenApiTypes.OBJECT)
    def patch(
        self, request: Request, tenant_id: uuid.UUID, company_id: uuid.UUID, bom_id: uuid.UUID
    ) -> Response:
        serializer = BomSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        return _response(
            save_bom(
                _scope(request, tenant_id, company_id),
                serializer.validated_data,
                bom_id=bom_id,
                revision=require_revision(request),
                key=require_idempotency_key(request),
                request_id=getattr(request, "request_id", None),
            )
        )


class BomActivateView(ManufacturingAPIView):
    @extend_schema(request=EmptySerializer, responses=OpenApiTypes.OBJECT)
    def post(
        self, request: Request, tenant_id: uuid.UUID, company_id: uuid.UUID, bom_id: uuid.UUID
    ) -> Response:
        serializer = EmptySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return _response(
            transition_bom(
                _scope(request, tenant_id, company_id),
                bom_id,
                "activate",
                revision=require_revision(request),
                key=require_idempotency_key(request),
                request_id=getattr(request, "request_id", None),
            )
        )


class BomRetireView(ManufacturingAPIView):
    @extend_schema(request=ReasonSerializer, responses=OpenApiTypes.OBJECT)
    def post(
        self, request: Request, tenant_id: uuid.UUID, company_id: uuid.UUID, bom_id: uuid.UUID
    ) -> Response:
        serializer = ReasonSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return _response(
            transition_bom(
                _scope(request, tenant_id, company_id),
                bom_id,
                "retire",
                reason=serializer.validated_data["reason"],
                revision=require_revision(request),
                key=require_idempotency_key(request),
                request_id=getattr(request, "request_id", None),
            )
        )


class OrderCollectionView(ManufacturingAPIView):
    @extend_schema(
        operation_id="manufacturing_orders_list",
        parameters=[ListQuerySerializer],
        responses=OpenApiTypes.OBJECT,
    )
    def get(self, request: Request, tenant_id: uuid.UUID, company_id: uuid.UUID) -> Response:
        return _list(request, tenant_id, company_id, "orders")

    @extend_schema(request=OrderSerializer, responses={201: OpenApiTypes.OBJECT})
    def post(self, request: Request, tenant_id: uuid.UUID, company_id: uuid.UUID) -> Response:
        serializer = OrderSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return _response(
            save_order(
                _scope(request, tenant_id, company_id),
                serializer.validated_data,
                key=require_idempotency_key(request),
                request_id=getattr(request, "request_id", None),
            ),
            201,
        )


class OrderDetailView(ManufacturingAPIView):
    @extend_schema(responses=OpenApiTypes.OBJECT)
    def get(
        self, request: Request, tenant_id: uuid.UUID, company_id: uuid.UUID, order_id: uuid.UUID
    ) -> Response:
        with company_read_scope(
            _scope(request, tenant_id, company_id),
            permission="manufacturing.order.view",
            module="manufacturing",
        ):
            result = order_detail(company_id, order_id)
        return _response(result)

    @extend_schema(request=OrderSerializer, responses=OpenApiTypes.OBJECT)
    def patch(
        self, request: Request, tenant_id: uuid.UUID, company_id: uuid.UUID, order_id: uuid.UUID
    ) -> Response:
        serializer = OrderSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        return _response(
            save_order(
                _scope(request, tenant_id, company_id),
                serializer.validated_data,
                order_id=order_id,
                revision=require_revision(request),
                key=require_idempotency_key(request),
                request_id=getattr(request, "request_id", None),
            )
        )


class OrderCancelView(ManufacturingAPIView):
    @extend_schema(request=ExecutionCancelSerializer, responses=OpenApiTypes.OBJECT)
    def post(
        self, request: Request, tenant_id: uuid.UUID, company_id: uuid.UUID, order_id: uuid.UUID
    ) -> Response:
        serializer = ExecutionCancelSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return _response(
            cancel_execution(
                _scope(request, tenant_id, company_id),
                order_id,
                serializer.validated_data,
                revision=require_revision(request),
                key=require_idempotency_key(request),
                request_id=getattr(request, "request_id", None),
            )
        )


class OrderReleaseView(ManufacturingAPIView):
    @extend_schema(request=ReleaseSerializer, responses=OpenApiTypes.OBJECT)
    def post(
        self, request: Request, tenant_id: uuid.UUID, company_id: uuid.UUID, order_id: uuid.UUID
    ) -> Response:
        serializer = ReleaseSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return _response(
            release_order(
                _scope(request, tenant_id, company_id),
                order_id,
                serializer.validated_data,
                revision=require_revision(request),
                key=require_idempotency_key(request),
                request_id=getattr(request, "request_id", None),
            )
        )


class OrderMaterialIssueView(ManufacturingAPIView):
    @extend_schema(request=MaterialIssueSerializer, responses=OpenApiTypes.OBJECT)
    def post(
        self, request: Request, tenant_id: uuid.UUID, company_id: uuid.UUID, order_id: uuid.UUID
    ) -> Response:
        serializer = MaterialIssueSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return _response(
            issue_materials(
                _scope(request, tenant_id, company_id),
                order_id,
                serializer.validated_data,
                revision=require_revision(request),
                key=require_idempotency_key(request),
                request_id=getattr(request, "request_id", None),
            )
        )


class OrderOutputView(ManufacturingAPIView):
    @extend_schema(request=OutputSerializer, responses=OpenApiTypes.OBJECT)
    def post(
        self, request: Request, tenant_id: uuid.UUID, company_id: uuid.UUID, order_id: uuid.UUID
    ) -> Response:
        serializer = OutputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return _response(
            receive_output(
                _scope(request, tenant_id, company_id),
                order_id,
                serializer.validated_data,
                revision=require_revision(request),
                key=require_idempotency_key(request),
                request_id=getattr(request, "request_id", None),
            )
        )


class OrderCompleteView(ManufacturingAPIView):
    @extend_schema(request=EmptySerializer, responses=OpenApiTypes.OBJECT)
    def post(
        self, request: Request, tenant_id: uuid.UUID, company_id: uuid.UUID, order_id: uuid.UUID
    ) -> Response:
        serializer = EmptySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return _response(
            complete_order(
                _scope(request, tenant_id, company_id),
                order_id,
                revision=require_revision(request),
                key=require_idempotency_key(request),
                request_id=getattr(request, "request_id", None),
            )
        )
