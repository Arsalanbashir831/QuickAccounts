import uuid
from typing import cast

from drf_spectacular.utils import OpenApiParameter, OpenApiTypes, extend_schema
from rest_framework import status
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.licensing.platform_api.permissions import IsPlatformOperator
from apps.licensing.platform_api.serializers import (
    CompanyProvisionSerializer,
    LicenseIssueSerializer,
    LicenseStatusSerializer,
    PlanPublishSerializer,
    TenantProvisionSerializer,
)
from apps.licensing.platform_services import (
    assign_license,
    change_license_status,
    issue_license,
    provision_company,
    provision_tenant,
    publish_plan_version,
)
from common.api.headers import require_idempotency_key

IDEMPOTENCY_HEADER = OpenApiParameter(
    name="Idempotency-Key",
    type=OpenApiTypes.STR,
    location=OpenApiParameter.HEADER,
    required=True,
    description="Unique key for this operator command; exact retries return the recorded result.",
)


class TenantProvisionView(APIView):
    permission_classes = [IsPlatformOperator]

    @extend_schema(
        request=TenantProvisionSerializer,
        responses={201: OpenApiTypes.OBJECT},
        parameters=[IDEMPOTENCY_HEADER],
        description=(
            "Active superuser only. Atomically create a tenant, initial company, "
            "owner account, and both memberships. The owner password is write-only "
            "and must be delivered securely out of band. The reason is audited."
        ),
    )
    def post(self, request: Request) -> Response:
        serializer = TenantProvisionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        body = provision_tenant(
            cast(uuid.UUID, request.user.pk),
            **serializer.validated_data,
            idempotency_key=require_idempotency_key(request),
        )
        return Response(body, status=status.HTTP_201_CREATED)


class LicenseAssignView(APIView):
    permission_classes = [IsPlatformOperator]

    @extend_schema(
        request=LicenseStatusSerializer,
        responses=OpenApiTypes.OBJECT,
        parameters=[IDEMPOTENCY_HEADER],
        description=(
            "Active superuser only. Bind an active license with a current term to "
            "its tenant and product. Reject if another license is already assigned. "
            "The reason and operator are audited. Tenant owners cannot do this."
        ),
    )
    def post(self, request: Request, license_id: uuid.UUID) -> Response:
        serializer = LicenseStatusSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        body = assign_license(
            cast(uuid.UUID, request.user.pk),
            license_id,
            reason=serializer.validated_data["reason"],
            idempotency_key=require_idempotency_key(request),
        )
        return Response(body)


class CompanyProvisionView(APIView):
    permission_classes = [IsPlatformOperator]

    @extend_schema(
        request=CompanyProvisionSerializer,
        responses={201: OpenApiTypes.OBJECT},
        parameters=[IDEMPOTENCY_HEADER],
        description=(
            "Active superuser only. Add a company to an existing tenant and assign "
            "its existing owner. The reason and operator are audited."
        ),
    )
    def post(self, request: Request, tenant_id: uuid.UUID) -> Response:
        serializer = CompanyProvisionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        body = provision_company(
            cast(uuid.UUID, request.user.pk),
            tenant_id,
            **serializer.validated_data,
            idempotency_key=require_idempotency_key(request),
        )
        return Response(body, status=status.HTTP_201_CREATED)


class LicenseIssueView(APIView):
    permission_classes = [IsPlatformOperator]

    @extend_schema(
        request=LicenseIssueSerializer,
        responses={201: OpenApiTypes.OBJECT},
        parameters=[IDEMPOTENCY_HEADER],
        description=(
            "Active superuser only. Issue a license from a published plan version. "
            "The credential is returned once; assignment is a separate operator command. "
            "The reason and operator are audited."
        ),
    )
    def post(self, request: Request) -> Response:
        serializer = LicenseIssueSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        body = issue_license(
            cast(uuid.UUID, request.user.pk),
            tenant_id=serializer.validated_data["tenant_id"],
            plan_version_id=serializer.validated_data["plan_version_id"],
            reason=serializer.validated_data["reason"],
            idempotency_key=require_idempotency_key(request),
        )
        return Response(body, status=status.HTTP_201_CREATED)


class LicenseStatusView(APIView):
    permission_classes = [IsPlatformOperator]

    @extend_schema(
        request=LicenseStatusSerializer,
        responses=OpenApiTypes.OBJECT,
        parameters=[IDEMPOTENCY_HEADER],
        description="Active superuser only. Change license status and audit the reason.",
    )
    def post(self, request: Request, license_id: uuid.UUID, action: str) -> Response:
        serializer = LicenseStatusSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        body = change_license_status(
            cast(uuid.UUID, request.user.pk),
            license_id,
            action=action,
            reason=serializer.validated_data["reason"],
            idempotency_key=require_idempotency_key(request),
        )
        return Response(body)


class PlanPublishView(APIView):
    permission_classes = [IsPlatformOperator]

    @extend_schema(
        request=PlanPublishSerializer,
        responses=OpenApiTypes.OBJECT,
        parameters=[IDEMPOTENCY_HEADER],
        description=(
            "Active superuser only. Publish an existing draft plan version so it can "
            "be used for license issuance. The reason and operator are audited."
        ),
    )
    def post(self, request: Request, plan_version_id: uuid.UUID) -> Response:
        serializer = PlanPublishSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        body = publish_plan_version(
            cast(uuid.UUID, request.user.pk),
            plan_version_id,
            reason=serializer.validated_data["reason"],
            idempotency_key=require_idempotency_key(request),
        )
        return Response(body)
