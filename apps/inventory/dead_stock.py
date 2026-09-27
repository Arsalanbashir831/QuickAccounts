"""Current stock inactivity, not FIFO layer age or an impairment assessment."""

import datetime as dt
import uuid
from typing import Any

from django.db import connection
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.inventory.api.views import _limit, _scope
from apps.inventory.selectors import _rows
from common.access.scopes import company_read_scope
from common.api.errors import APIError


def dead_stock(
    company_id: uuid.UUID, *, days: int, as_of: dt.date, after: uuid.UUID | None, limit: int
) -> list[dict[str, Any]]:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT p.id,p.warehouse_id,w.name AS warehouse_name,w.stock_category,
                p.item_id,i.sku,i.name,p.lot_id,p.on_hand_quantity,p.reserved_quantity,
                p.value_company,c.functional_currency AS currency_code,
                activity.last_sale_at,activity.first_receipt_at,
                %(as_of)s::date-coalesce(activity.last_sale_at,activity.first_receipt_at)::date
                    AS inactive_days
            FROM erp.inventory_positions p JOIN erp.warehouses w
                ON w.company_id=p.company_id AND w.id=p.warehouse_id
            JOIN erp.items i ON i.company_id=p.company_id AND i.id=p.item_id
            JOIN erp.companies c ON c.id=p.company_id
            JOIN LATERAL(
                SELECT max(occurred_at) FILTER(WHERE source_type='sales_invoice'
                    AND quantity_delta<0) AS last_sale_at,
                    min(occurred_at) FILTER(WHERE quantity_delta>0) AS first_receipt_at
                FROM erp.stock_movements m WHERE m.company_id=p.company_id
                    AND m.warehouse_id=p.warehouse_id AND m.item_id=p.item_id
                    AND m.lot_id IS NOT DISTINCT FROM p.lot_id
                    AND m.occurred_at<((%(as_of)s::date+1)::timestamp AT TIME ZONE 'UTC')
            ) activity ON true
            WHERE p.company_id=%(company)s AND p.on_hand_quantity>0
                AND (%(after)s::uuid IS NULL OR p.id>%(after)s)
                AND %(as_of)s::date-coalesce(activity.last_sale_at,activity.first_receipt_at)::date
                    >=%(days)s ORDER BY p.id LIMIT %(limit)s
        """,
            {"company": company_id, "days": days, "as_of": as_of, "after": after, "limit": limit},
        )
        return _rows(cursor)


class DeadStockView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(responses=OpenApiTypes.OBJECT, operation_id="inventory_dead_stock_report")
    def get(self, request: Request, tenant_id: uuid.UUID, company_id: uuid.UUID) -> Response:
        try:
            days = int(request.query_params["inactive_days"])
            if not 1 <= days <= 36500:
                raise ValueError
            after = (
                uuid.UUID(request.query_params["cursor"])
                if "cursor" in request.query_params
                else None
            )
        except (KeyError, ValueError) as exc:
            raise APIError(
                code="INVALID_FILTER", message="Supply inactive_days (1–36500) and a valid cursor."
            ) from exc
        limit = _limit(request)
        as_of = dt.datetime.now(dt.UTC).date()
        with company_read_scope(
            _scope(request, tenant_id, company_id), permission="inventory.view", module="inventory"
        ):
            rows = dead_stock(company_id, days=days, as_of=as_of, after=after, limit=limit + 1)
        return Response(
            {
                "as_of": str(as_of),
                "inactivity_threshold_days": days,
                "classification": "current_stock_sales_inactivity",
                "automatic_write_off": False,
                "results": rows[:limit],
                "next_cursor": rows[limit - 1]["id"] if len(rows) > limit else None,
            }
        )
