import uuid
from typing import Any, cast

from django.http import HttpResponse
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework import serializers, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.jobs.services import artifact, cancel_job, job_detail, list_jobs, submit_job
from common.access.scopes import CompanyScope
from common.api.errors import APIError


def _scope(request: Request, tenant_id: uuid.UUID, company_id: uuid.UUID) -> CompanyScope:
    return CompanyScope(tenant_id, company_id, cast(uuid.UUID, request.user.pk))


class JobSubmissionSerializer(serializers.Serializer[dict[str, Any]]):
    job_type = serializers.ChoiceField(choices=["partner_export", "partner_import"])
    job_key = serializers.CharField(max_length=200, allow_blank=False)
    rows = serializers.ListField(child=serializers.DictField(), required=False, max_length=100)


class JobCollectionView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(responses=OpenApiTypes.OBJECT, operation_id="internal_jobs_list")
    def get(self, request: Request, tenant_id: uuid.UUID, company_id: uuid.UUID) -> Response:
        return Response({"results": list_jobs(_scope(request, tenant_id, company_id))})

    @extend_schema(
        request=JobSubmissionSerializer,
        responses=OpenApiTypes.OBJECT,
        operation_id="internal_jobs_submit",
    )
    def post(self, request: Request, tenant_id: uuid.UUID, company_id: uuid.UUID) -> Response:
        data = JobSubmissionSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        values = data.validated_data
        result = submit_job(
            _scope(request, tenant_id, company_id),
            values["job_type"],
            values["job_key"],
            values.get("rows"),
        )
        return Response(result, status=status.HTTP_202_ACCEPTED)


class JobDetailView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(responses=OpenApiTypes.OBJECT, operation_id="internal_job_detail")
    def get(
        self, request: Request, tenant_id: uuid.UUID, company_id: uuid.UUID, job_id: uuid.UUID
    ) -> Response:
        return Response(job_detail(_scope(request, tenant_id, company_id), job_id))


class JobCancelView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(responses=OpenApiTypes.OBJECT, operation_id="internal_job_cancel")
    def post(
        self, request: Request, tenant_id: uuid.UUID, company_id: uuid.UUID, job_id: uuid.UUID
    ) -> Response:
        if request.data:
            raise APIError(code="INVALID_JOB", message="Cancellation accepts no request body.")
        return Response(cancel_job(_scope(request, tenant_id, company_id), job_id))


class JobArtifactView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(responses=OpenApiTypes.BINARY, operation_id="internal_job_artifact")
    def get(
        self, request: Request, tenant_id: uuid.UUID, company_id: uuid.UUID, job_id: uuid.UUID
    ) -> HttpResponse:
        filename, content = artifact(_scope(request, tenant_id, company_id), job_id)
        response = HttpResponse(content, content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = f'attachment; filename="{filename}"'
        response["X-Content-Type-Options"] = "nosniff"
        return response
