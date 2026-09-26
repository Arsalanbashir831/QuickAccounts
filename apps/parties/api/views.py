import uuid
from typing import cast

from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.parties.api.serializers import (
    PartnerAddressCreateSerializer,
    PartnerAddressPatchSerializer,
    PartnerCreateSerializer,
    PartnerPatchSerializer,
    PartnerTaxRegistrationCreateSerializer,
)
from apps.parties.selectors import (
    list_partner_addresses,
    list_partner_tax_registrations,
    list_partners,
    partner_detail,
)
from apps.parties.services import (
    create_partner,
    create_partner_address,
    create_partner_tax_registration,
    update_partner,
    update_partner_address,
)
from common.access.scopes import CompanyScope, company_read_scope
from common.api.errors import APIError, ScopeNotFound
from common.api.headers import require_revision

PARTNER_KINDS = {"customer", "supplier", "both", "other"}
ADDRESS_KINDS = {"billing", "shipping", "registered", "other"}


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


def _filters(request: Request) -> tuple[str | None, str | None, bool | None]:
    q = request.query_params.get("q")
    if q is not None:
        q = q.strip()
        if not q or len(q) > 100:
            raise APIError(
                code="INVALID_FILTER", message="q must contain between 1 and 100 characters."
            )
    kind = request.query_params.get("partner_kind")
    if kind is not None and kind not in PARTNER_KINDS:
        raise APIError(code="INVALID_FILTER", message="Unsupported partner_kind filter.")
    active_value = request.query_params.get("is_active")
    if active_value is None:
        is_active = None
    elif active_value.lower() in {"true", "1"}:
        is_active = True
    elif active_value.lower() in {"false", "0"}:
        is_active = False
    else:
        raise APIError(code="INVALID_FILTER", message="is_active must be true or false.")
    return q, kind, is_active


def _active_filter(request: Request) -> bool | None:
    value = request.query_params.get("is_active")
    if value is None:
        return None
    if value.lower() in {"true", "1"}:
        return True
    if value.lower() in {"false", "0"}:
        return False
    raise APIError(code="INVALID_FILTER", message="is_active must be true or false.")


class PartnerCollectionView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(responses=OpenApiTypes.OBJECT, operation_id="partner_list")
    def get(self, request: Request, tenant_id: uuid.UUID, company_id: uuid.UUID) -> Response:
        q, kind, is_active = _filters(request)
        scope = _scope(request, tenant_id, company_id)
        with company_read_scope(scope, permission="party.view", module="core"):
            rows, next_cursor = list_partners(
                company_id,
                limit=_limit(request),
                cursor=request.query_params.get("cursor"),
                q=q,
                kind=kind,
                is_active=is_active,
            )
        return Response({"results": rows, "next_cursor": next_cursor})

    @extend_schema(
        request=PartnerCreateSerializer,
        responses={201: OpenApiTypes.OBJECT},
        operation_id="partner_create",
    )
    def post(self, request: Request, tenant_id: uuid.UUID, company_id: uuid.UUID) -> Response:
        serializer = PartnerCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        result = create_partner(
            _scope(request, tenant_id, company_id),
            serializer.validated_data,
            request_id=getattr(request, "request_id", None),
        )
        return Response(
            result,
            status=status.HTTP_201_CREATED,
            headers={"ETag": f'"{result["row_version"]}"'},
        )


class PartnerDetailView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(responses=OpenApiTypes.OBJECT, operation_id="partner_retrieve")
    def get(
        self,
        request: Request,
        tenant_id: uuid.UUID,
        company_id: uuid.UUID,
        partner_id: uuid.UUID,
    ) -> Response:
        scope = _scope(request, tenant_id, company_id)
        with company_read_scope(scope, permission="party.view", module="core"):
            result = partner_detail(company_id, partner_id)
            if result is None:
                raise ScopeNotFound()
        return Response(result, headers={"ETag": f'"{result["row_version"]}"'})

    @extend_schema(
        request=PartnerPatchSerializer,
        responses=OpenApiTypes.OBJECT,
        operation_id="partner_update",
    )
    def patch(
        self,
        request: Request,
        tenant_id: uuid.UUID,
        company_id: uuid.UUID,
        partner_id: uuid.UUID,
    ) -> Response:
        serializer = PartnerPatchSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        result = update_partner(
            _scope(request, tenant_id, company_id),
            partner_id,
            serializer.validated_data,
            expected_revision=require_revision(request),
            request_id=getattr(request, "request_id", None),
        )
        return Response(result, headers={"ETag": f'"{result["row_version"]}"'})


class PartnerAddressCollectionView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(responses=OpenApiTypes.OBJECT, operation_id="partner_address_list")
    def get(
        self,
        request: Request,
        tenant_id: uuid.UUID,
        company_id: uuid.UUID,
        partner_id: uuid.UUID,
    ) -> Response:
        kind = request.query_params.get("address_kind")
        if kind is not None and kind not in ADDRESS_KINDS:
            raise APIError(code="INVALID_FILTER", message="Unsupported address_kind filter.")
        scope = _scope(request, tenant_id, company_id)
        with company_read_scope(scope, permission="party.view", module="core"):
            if partner_detail(company_id, partner_id) is None:
                raise ScopeNotFound()
            rows, next_cursor = list_partner_addresses(
                company_id,
                partner_id,
                limit=_limit(request),
                cursor=request.query_params.get("cursor"),
                kind=kind,
            )
        return Response({"results": rows, "next_cursor": next_cursor})

    @extend_schema(
        request=PartnerAddressCreateSerializer,
        responses={201: OpenApiTypes.OBJECT},
        operation_id="partner_address_create",
    )
    def post(
        self,
        request: Request,
        tenant_id: uuid.UUID,
        company_id: uuid.UUID,
        partner_id: uuid.UUID,
    ) -> Response:
        serializer = PartnerAddressCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        result = create_partner_address(
            _scope(request, tenant_id, company_id),
            partner_id,
            serializer.validated_data,
            request_id=getattr(request, "request_id", None),
        )
        return Response(
            result,
            status=status.HTTP_201_CREATED,
            headers={"ETag": f'"{result["row_version"]}"'},
        )


class PartnerAddressDetailView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        request=PartnerAddressPatchSerializer,
        responses=OpenApiTypes.OBJECT,
        operation_id="partner_address_update",
    )
    def patch(
        self,
        request: Request,
        tenant_id: uuid.UUID,
        company_id: uuid.UUID,
        partner_id: uuid.UUID,
        address_id: uuid.UUID,
    ) -> Response:
        serializer = PartnerAddressPatchSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        result = update_partner_address(
            _scope(request, tenant_id, company_id),
            partner_id,
            address_id,
            serializer.validated_data,
            expected_revision=require_revision(request),
            request_id=getattr(request, "request_id", None),
        )
        return Response(result, headers={"ETag": f'"{result["row_version"]}"'})


class PartnerTaxRegistrationCollectionView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        responses=OpenApiTypes.OBJECT,
        operation_id="partner_tax_registration_list",
    )
    def get(
        self,
        request: Request,
        tenant_id: uuid.UUID,
        company_id: uuid.UUID,
        partner_id: uuid.UUID,
    ) -> Response:
        scope = _scope(request, tenant_id, company_id)
        with company_read_scope(
            scope,
            permission="tax.configuration.manage",
            module="tax_calculation",
        ):
            if partner_detail(company_id, partner_id) is None:
                raise ScopeNotFound()
            rows, next_cursor = list_partner_tax_registrations(
                company_id,
                partner_id,
                limit=_limit(request),
                cursor=request.query_params.get("cursor"),
                is_active=_active_filter(request),
            )
        return Response({"results": rows, "next_cursor": next_cursor})

    @extend_schema(
        request=PartnerTaxRegistrationCreateSerializer,
        responses={201: OpenApiTypes.OBJECT},
        operation_id="partner_tax_registration_create",
    )
    def post(
        self,
        request: Request,
        tenant_id: uuid.UUID,
        company_id: uuid.UUID,
        partner_id: uuid.UUID,
    ) -> Response:
        serializer = PartnerTaxRegistrationCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        result = create_partner_tax_registration(
            _scope(request, tenant_id, company_id),
            partner_id,
            serializer.validated_data,
            request_id=getattr(request, "request_id", None),
        )
        return Response(result, status=status.HTTP_201_CREATED)
