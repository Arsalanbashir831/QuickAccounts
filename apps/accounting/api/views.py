import datetime as dt
import uuid
from typing import Any, cast

from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.serializers import Serializer
from rest_framework.views import APIView

from apps.accounting.api.serializers import (
    AccountPatchSerializer,
    AccountSerializer,
    DimensionTypeSerializer,
    DimensionValueSerializer,
    JournalEntryCreateSerializer,
    JournalEntryPatchSerializer,
    JournalSerializer,
    PeriodCommandSerializer,
    PeriodSerializer,
    PostingRuleSerializer,
    ReversalSerializer,
)
from apps.accounting.selectors.accounting import (
    account_detail,
    entry_detail,
    ledger,
    list_config,
    list_entries,
)
from apps.accounting.services.accounting import (
    change_period_state,
    create_config,
    create_entry,
    post_entry,
    reverse_entry,
    update_account,
    update_entry,
)
from common.access.scopes import CompanyScope, company_read_scope
from common.api.errors import APIError, ScopeNotFound
from common.api.headers import require_idempotency_key, require_revision

SERIALIZERS: dict[str, type[Serializer[Any]]] = {
    "accounts": AccountSerializer,
    "journals": JournalSerializer,
    "periods": PeriodSerializer,
    "dimension-types": DimensionTypeSerializer,
    "dimension-values": DimensionValueSerializer,
    "posting-rules": PostingRuleSerializer,
}


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


class ConfigCollectionView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(responses=OpenApiTypes.OBJECT)
    def get(
        self,
        request: Request,
        tenant_id: uuid.UUID,
        company_id: uuid.UUID,
        resource: str,
    ) -> Response:
        scope = _scope(request, tenant_id, company_id)
        with company_read_scope(scope, permission="accounting.view"):
            rows = list_config(company_id, resource, _limit(request))
        return Response({"results": rows})

    @extend_schema(
        request=OpenApiTypes.OBJECT,
        responses={201: OpenApiTypes.OBJECT},
    )
    def post(
        self,
        request: Request,
        tenant_id: uuid.UUID,
        company_id: uuid.UUID,
        resource: str,
    ) -> Response:
        serializer = SERIALIZERS[resource](data=request.data)
        serializer.is_valid(raise_exception=True)
        created_id = create_config(
            _scope(request, tenant_id, company_id), resource, serializer.validated_data
        )
        return Response({"id": str(created_id)}, status=status.HTTP_201_CREATED)


class AccountDetailView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(responses=OpenApiTypes.OBJECT, operation_id="accounting_account_retrieve")
    def get(
        self,
        request: Request,
        tenant_id: uuid.UUID,
        company_id: uuid.UUID,
        account_id: uuid.UUID,
    ) -> Response:
        scope = _scope(request, tenant_id, company_id)
        with company_read_scope(scope, permission="accounting.view"):
            account = account_detail(company_id, account_id)
            if account is None:
                raise ScopeNotFound()
        return Response(account)

    @extend_schema(
        request=AccountPatchSerializer,
        responses=OpenApiTypes.OBJECT,
        operation_id="accounting_account_update",
    )
    def patch(
        self,
        request: Request,
        tenant_id: uuid.UUID,
        company_id: uuid.UUID,
        account_id: uuid.UUID,
    ) -> Response:
        serializer = AccountPatchSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        scope = _scope(request, tenant_id, company_id)
        update_account(scope, account_id, serializer.validated_data)
        with company_read_scope(scope, permission="accounting.view"):
            account = account_detail(company_id, account_id)
        return Response(account)


class PeriodCommandView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        request=PeriodCommandSerializer,
        responses=OpenApiTypes.OBJECT,
    )
    def post(
        self,
        request: Request,
        tenant_id: uuid.UUID,
        company_id: uuid.UUID,
        period_id: uuid.UUID,
        action: str,
    ) -> Response:
        serializer = PeriodCommandSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        body, response_status = change_period_state(
            _scope(request, tenant_id, company_id),
            period_id=period_id,
            action=action,
            reason=serializer.validated_data["reason"],
            expected_revision=require_revision(request),
            idempotency_key=require_idempotency_key(request),
            request_id=getattr(request, "request_id", None),
        )
        return Response(
            body,
            status=response_status,
            headers={"ETag": f'"{body["row_version"]}"'},
        )


class EntryCollectionView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(responses=OpenApiTypes.OBJECT, operation_id="accounting_entry_list")
    def get(self, request: Request, tenant_id: uuid.UUID, company_id: uuid.UUID) -> Response:
        entry_status = request.query_params.get("status")
        if entry_status not in {None, "draft", "posted", "void"}:
            raise APIError(code="INVALID_FILTER", message="Unsupported entry status filter.")
        scope = _scope(request, tenant_id, company_id)
        with company_read_scope(scope, permission="accounting.view"):
            rows, next_cursor = list_entries(
                company_id,
                limit=_limit(request),
                cursor=request.query_params.get("cursor"),
                status=entry_status,
            )
        return Response({"results": rows, "next_cursor": next_cursor})

    @extend_schema(
        request=JournalEntryCreateSerializer,
        responses={201: OpenApiTypes.OBJECT},
        operation_id="accounting_entry_create",
    )
    def post(self, request: Request, tenant_id: uuid.UUID, company_id: uuid.UUID) -> Response:
        serializer = JournalEntryCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        scope = _scope(request, tenant_id, company_id)
        entry_id = create_entry(scope, serializer.validated_data)
        with company_read_scope(scope, permission="accounting.view"):
            body = entry_detail(company_id, entry_id)
        return Response(body, status=status.HTTP_201_CREATED, headers={"ETag": '"1"'})


class EntryDetailView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(responses=OpenApiTypes.OBJECT, operation_id="accounting_entry_retrieve")
    def get(
        self,
        request: Request,
        tenant_id: uuid.UUID,
        company_id: uuid.UUID,
        entry_id: uuid.UUID,
    ) -> Response:
        scope = _scope(request, tenant_id, company_id)
        with company_read_scope(scope, permission="accounting.view"):
            body = entry_detail(company_id, entry_id)
            if body is None:
                raise ScopeNotFound()
        return Response(body, headers={"ETag": f'"{body["row_version"]}"'})

    @extend_schema(
        request=JournalEntryPatchSerializer,
        responses=OpenApiTypes.OBJECT,
        operation_id="accounting_entry_update",
    )
    def patch(
        self,
        request: Request,
        tenant_id: uuid.UUID,
        company_id: uuid.UUID,
        entry_id: uuid.UUID,
    ) -> Response:
        expected_revision = require_revision(request)
        serializer = JournalEntryPatchSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        scope = _scope(request, tenant_id, company_id)
        revision = update_entry(scope, entry_id, expected_revision, serializer.validated_data)
        with company_read_scope(scope, permission="accounting.view"):
            body = entry_detail(company_id, entry_id)
        return Response(body, headers={"ETag": f'"{revision}"'})


class EntryPostView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        request=None,
        responses=OpenApiTypes.OBJECT,
        operation_id="accounting_entry_post",
    )
    def post(
        self,
        request: Request,
        tenant_id: uuid.UUID,
        company_id: uuid.UUID,
        entry_id: uuid.UUID,
    ) -> Response:
        body, response_status = post_entry(
            _scope(request, tenant_id, company_id),
            entry_id=entry_id,
            expected_revision=require_revision(request),
            idempotency_key=require_idempotency_key(request),
            request_id=getattr(request, "request_id", None),
        )
        return Response(body, status=response_status)


class EntryReverseView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        request=ReversalSerializer,
        responses={201: OpenApiTypes.OBJECT},
        operation_id="accounting_entry_reverse",
    )
    def post(
        self,
        request: Request,
        tenant_id: uuid.UUID,
        company_id: uuid.UUID,
        entry_id: uuid.UUID,
    ) -> Response:
        serializer = ReversalSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        body, response_status = reverse_entry(
            _scope(request, tenant_id, company_id),
            entry_id=entry_id,
            command=serializer.validated_data,
            idempotency_key=require_idempotency_key(request),
            request_id=getattr(request, "request_id", None),
        )
        return Response(body, status=response_status)


def _date_param(request: Request, key: str) -> dt.date | None:
    value = request.query_params.get(key)
    if value is None:
        return None
    try:
        return dt.date.fromisoformat(value)
    except ValueError as exc:
        raise APIError(code="INVALID_FILTER", message=f"{key} must be an ISO date.") from exc


class LedgerView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(responses=OpenApiTypes.OBJECT, operation_id="accounting_ledger_list")
    def get(self, request: Request, tenant_id: uuid.UUID, company_id: uuid.UUID) -> Response:
        raw_account = request.query_params.get("account_id")
        try:
            account_id = uuid.UUID(raw_account) if raw_account else None
        except ValueError as exc:
            raise APIError(code="INVALID_FILTER", message="account_id must be a UUID.") from exc
        scope = _scope(request, tenant_id, company_id)
        with company_read_scope(scope, permission="accounting.ledger.view"):
            rows = ledger(
                company_id,
                account_id=account_id,
                date_from=_date_param(request, "date_from"),
                date_to=_date_param(request, "date_to"),
                limit=_limit(request),
            )
        return Response({"results": rows})
