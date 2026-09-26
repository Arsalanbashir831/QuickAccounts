import uuid
from typing import cast

from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.licensing.platform_api.permissions import IsPlatformOperator
from apps.licensing.platform_api.serializers import (
    LicenseIssueSerializer,
    LicenseStatusSerializer,
    PlanPublishSerializer,
)
from apps.licensing.platform_services import (
    change_license_status,
    issue_license,
    publish_plan_version,
)
from common.api.headers import require_idempotency_key


class LicenseIssueView(APIView):
    permission_classes = [IsPlatformOperator]

    @extend_schema(request=LicenseIssueSerializer, responses={201: OpenApiTypes.OBJECT})
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

    @extend_schema(request=LicenseStatusSerializer, responses=OpenApiTypes.OBJECT)
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

    @extend_schema(request=PlanPublishSerializer, responses=OpenApiTypes.OBJECT)
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
