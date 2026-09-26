import uuid

from django.db import connection, transaction
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from common.api.errors import ScopeNotFound
from common.db.context import set_local_context


def _dict_rows(cursor: object) -> list[dict[str, object]]:
    names = [column.name for column in cursor.description]  # type: ignore[attr-defined]
    return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]  # type: ignore[attr-defined]


class TenantListView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(responses=OpenApiTypes.OBJECT, operation_id="tenant_list")
    def get(self, request: Request) -> Response:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT tenant_id, tenant_name, tenant_role "
                "FROM identity.discover_user_tenants(%s)",
                [request.user.id],
            )
            rows = _dict_rows(cursor)
        return Response({"results": rows})


class CompanyListView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(responses=OpenApiTypes.OBJECT, operation_id="tenant_company_list")
    def get(self, request: Request, tenant_id: uuid.UUID) -> Response:
        with transaction.atomic():
            set_local_context(tenant_id=tenant_id, user_id=request.user.id)
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT c.id, c.code, c.legal_name, c.functional_currency,
                           c.timezone_name, c.is_active
                    FROM erp.companies c
                    JOIN identity.company_memberships cm
                      ON cm.tenant_id = c.tenant_id AND cm.company_id = c.id
                    JOIN identity.tenant_memberships tm
                      ON tm.tenant_id = c.tenant_id AND tm.user_id = cm.user_id
                    WHERE c.tenant_id = %s AND cm.user_id = %s
                      AND cm.is_active AND tm.is_active
                    ORDER BY c.legal_name, c.id
                    """,
                    [tenant_id, request.user.id],
                )
                rows = _dict_rows(cursor)
            if not rows:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT 1 FROM identity.tenant_memberships "
                        "WHERE tenant_id = %s AND user_id = %s AND is_active",
                        [tenant_id, request.user.id],
                    )
                    if cursor.fetchone() is None:
                        raise ScopeNotFound()
        return Response({"results": rows})
