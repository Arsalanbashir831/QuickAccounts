import uuid
from typing import cast

from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.inventory.api.serializers import (
    ItemAccountingProfileSerializer,
    ItemCreateSerializer,
    ItemPatchSerializer,
)
from apps.inventory.selectors import (
    item_accounting_profile_detail,
    item_detail,
    list_items,
    list_uoms,
)
from apps.inventory.services import create_item, put_item_accounting_profile, update_item
from common.access.scopes import CompanyScope, company_read_scope
from common.api.errors import APIError, ScopeNotFound
from common.api.headers import require_revision

ITEM_KINDS = {"stock", "service", "non_stock"}


def _scope(request: Request, tenant_id: uuid.UUID, company_id: uuid.UUID) -> CompanyScope:
    return CompanyScope(
        tenant_id=tenant_id,
        company_id=company_id,
        user_id=cast(uuid.UUID, request.user.pk),
    )


def _limit(request: Request) -> int:
    raw = request.query_params.get("limit", "50")
    try:
        value = int(raw)
    except ValueError as exc:
        raise APIError(code="INVALID_LIMIT", message="Limit must be an integer.") from exc
    if not 1 <= value <= 200:
        raise APIError(code="INVALID_LIMIT", message="Limit must be between 1 and 200.")
    return value


def _active_filter(request: Request) -> bool | None:
    value = request.query_params.get("is_active")
    if value is None:
        return None
    if value.lower() in {"true", "1"}:
        return True
    if value.lower() in {"false", "0"}:
        return False
    raise APIError(code="INVALID_FILTER", message="is_active must be true or false.")


class UnitOfMeasureCollectionView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(responses=OpenApiTypes.OBJECT, operation_id="reference_uom_list")
    def get(self, request: Request, tenant_id: uuid.UUID, company_id: uuid.UUID) -> Response:
        scope = _scope(request, tenant_id, company_id)
        with company_read_scope(scope, permission="item.view", module="core"):
            rows, next_cursor = list_uoms(
                limit=_limit(request), cursor=request.query_params.get("cursor")
            )
        return Response({"results": rows, "next_cursor": next_cursor})


class ItemCollectionView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(responses=OpenApiTypes.OBJECT, operation_id="inventory_item_list")
    def get(self, request: Request, tenant_id: uuid.UUID, company_id: uuid.UUID) -> Response:
        q = request.query_params.get("q")
        if q is not None:
            q = q.strip()
            if not q or len(q) > 100:
                raise APIError(
                    code="INVALID_FILTER",
                    message="q must contain between 1 and 100 characters.",
                )
        kind = request.query_params.get("item_kind")
        if kind is not None and kind not in ITEM_KINDS:
            raise APIError(code="INVALID_FILTER", message="Unsupported item_kind filter.")
        scope = _scope(request, tenant_id, company_id)
        with company_read_scope(scope, permission="item.view", module="core"):
            rows, next_cursor = list_items(
                company_id,
                limit=_limit(request),
                cursor=request.query_params.get("cursor"),
                q=q,
                kind=kind,
                is_active=_active_filter(request),
            )
        return Response({"results": rows, "next_cursor": next_cursor})

    @extend_schema(
        request=ItemCreateSerializer,
        responses={201: OpenApiTypes.OBJECT},
        operation_id="inventory_item_create",
    )
    def post(self, request: Request, tenant_id: uuid.UUID, company_id: uuid.UUID) -> Response:
        serializer = ItemCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        result = create_item(
            _scope(request, tenant_id, company_id),
            serializer.validated_data,
            request_id=getattr(request, "request_id", None),
        )
        return Response(
            result,
            status=status.HTTP_201_CREATED,
            headers={"ETag": f'"{result["row_version"]}"'},
        )


class ItemDetailView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(responses=OpenApiTypes.OBJECT, operation_id="inventory_item_retrieve")
    def get(
        self,
        request: Request,
        tenant_id: uuid.UUID,
        company_id: uuid.UUID,
        item_id: uuid.UUID,
    ) -> Response:
        scope = _scope(request, tenant_id, company_id)
        with company_read_scope(scope, permission="item.view", module="core"):
            result = item_detail(company_id, item_id)
            if result is None:
                raise ScopeNotFound()
        return Response(result, headers={"ETag": f'"{result["row_version"]}"'})

    @extend_schema(
        request=ItemPatchSerializer,
        responses=OpenApiTypes.OBJECT,
        operation_id="inventory_item_update",
    )
    def patch(
        self,
        request: Request,
        tenant_id: uuid.UUID,
        company_id: uuid.UUID,
        item_id: uuid.UUID,
    ) -> Response:
        serializer = ItemPatchSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        result = update_item(
            _scope(request, tenant_id, company_id),
            item_id,
            serializer.validated_data,
            expected_revision=require_revision(request),
            request_id=getattr(request, "request_id", None),
        )
        return Response(result, headers={"ETag": f'"{result["row_version"]}"'})


class ItemAccountingProfileView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        responses=OpenApiTypes.OBJECT,
        operation_id="inventory_item_accounting_profile_retrieve",
    )
    def get(
        self,
        request: Request,
        tenant_id: uuid.UUID,
        company_id: uuid.UUID,
        item_id: uuid.UUID,
    ) -> Response:
        scope = _scope(request, tenant_id, company_id)
        with company_read_scope(scope, permission="accounting.setup.manage", module="accounting"):
            if item_detail(company_id, item_id) is None:
                raise ScopeNotFound()
            result = item_accounting_profile_detail(company_id, item_id)
            if result is None:
                raise APIError(
                    code="ITEM_ACCOUNTING_PROFILE_NOT_FOUND",
                    message="The item does not have an accounting profile.",
                    status_code=404,
                )
        return Response(result)

    @extend_schema(
        request=ItemAccountingProfileSerializer,
        responses=OpenApiTypes.OBJECT,
        operation_id="inventory_item_accounting_profile_replace",
    )
    def put(
        self,
        request: Request,
        tenant_id: uuid.UUID,
        company_id: uuid.UUID,
        item_id: uuid.UUID,
    ) -> Response:
        serializer = ItemAccountingProfileSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        result = put_item_accounting_profile(
            _scope(request, tenant_id, company_id),
            item_id,
            serializer.validated_data,
            request_id=getattr(request, "request_id", None),
        )
        return Response(result)
