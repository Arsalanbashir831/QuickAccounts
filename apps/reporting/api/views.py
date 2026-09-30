import datetime as dt
import uuid
from typing import cast

from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.reporting.selectors.accounting import (
    general_ledger,
    period_activity,
    tax_components,
    trial_balance,
)
from common.access.scopes import CompanyScope, company_read_scope
from common.api.errors import APIError


def _scope(request: Request, tenant_id: uuid.UUID, company_id: uuid.UUID) -> CompanyScope:
    return CompanyScope(tenant_id, company_id, cast(uuid.UUID, request.user.pk))


def _date(request: Request, name: str) -> dt.date:
    value = request.query_params.get(name)
    if not value:
        raise APIError(code="MISSING_FILTER", message=f"{name} is required.")
    try:
        return dt.date.fromisoformat(value)
    except ValueError as exc:
        raise APIError(code="INVALID_FILTER", message=f"{name} must be an ISO date.") from exc


def _page(request: Request) -> tuple[int, uuid.UUID | None]:
    try:
        limit = int(request.query_params.get("limit", "200"))
        after = (
            uuid.UUID(request.query_params["cursor"])
            if "cursor" in request.query_params else None
        )
    except ValueError as exc:
        raise APIError(code="INVALID_FILTER", message="Invalid report limit or cursor.") from exc
    if not 1 <= limit <= 200:
        raise APIError(code="INVALID_LIMIT", message="limit must be between 1 and 200.")
    return limit, after


class TrialBalanceView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(responses=OpenApiTypes.OBJECT, operation_id="report_trial_balance")
    def get(self, request: Request, tenant_id: uuid.UUID, company_id: uuid.UUID) -> Response:
        date_from = _date(request, "date_from")
        date_to = _date(request, "date_to")
        if date_from > date_to:
            raise APIError(code="INVALID_FILTER", message="date_from must not exceed date_to.")
        scope = _scope(request, tenant_id, company_id)
        limit, after = _page(request)
        with company_read_scope(scope, permission="reports.view"):
            rows = trial_balance(company_id, date_from=date_from, date_to=date_to,
                                 limit=limit + 1, after=after)
        return Response({"date_from": str(date_from), "date_to": str(date_to),
                         "results": rows[:limit],
                         "next_cursor": str(rows[limit - 1]["account_id"])
                         if len(rows) > limit else None})


class PeriodActivityView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(responses=OpenApiTypes.OBJECT, operation_id="report_period_activity")
    def get(self, request: Request, tenant_id: uuid.UUID, company_id: uuid.UUID) -> Response:
        raw_period = request.query_params.get("period_id")
        try:
            period_id = uuid.UUID(raw_period) if raw_period else None
        except ValueError as exc:
            raise APIError(code="INVALID_FILTER", message="period_id must be a UUID.") from exc
        if period_id is None:
            raise APIError(code="MISSING_FILTER", message="period_id is required.")
        scope = _scope(request, tenant_id, company_id)
        with company_read_scope(scope, permission="reports.view"):
            rows = period_activity(company_id, period_id)
        return Response({"period_id": str(period_id), "results": rows})


class GeneralLedgerView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(responses=OpenApiTypes.OBJECT, operation_id="report_general_ledger")
    def get(self, request: Request, tenant_id: uuid.UUID, company_id: uuid.UUID) -> Response:
        raw_account = request.query_params.get("account_id")
        try:
            account_id = uuid.UUID(raw_account) if raw_account else None
        except ValueError as exc:
            raise APIError(code="INVALID_FILTER", message="account_id must be a UUID.") from exc
        if account_id is None:
            raise APIError(code="MISSING_FILTER", message="account_id is required.")
        date_from = _date(request, "date_from")
        date_to = _date(request, "date_to")
        if date_from > date_to:
            raise APIError(code="INVALID_FILTER", message="date_from must not exceed date_to.")
        try:
            limit = int(request.query_params.get("limit", "200"))
        except ValueError as exc:
            raise APIError(code="INVALID_LIMIT", message="limit must be an integer.") from exc
        if not 1 <= limit <= 200:
            raise APIError(code="INVALID_LIMIT", message="limit must be between 1 and 200.")
        cursor_params = [request.query_params.get(key) for key in
                         ("cursor_date", "cursor_entry", "cursor_line")]
        if any(value is not None for value in cursor_params) and not all(
            value is not None for value in cursor_params
        ):
            raise APIError(code="INVALID_CURSOR", message="All ledger cursor fields are required.")
        try:
            cursor_date = dt.date.fromisoformat(cursor_params[0]) if cursor_params[0] else None
            cursor_entry = uuid.UUID(cursor_params[1]) if cursor_params[1] else None
            cursor_line = int(cursor_params[2]) if cursor_params[2] else None
        except ValueError as exc:
            raise APIError(code="INVALID_CURSOR", message="Invalid ledger cursor.") from exc
        scope = _scope(request, tenant_id, company_id)
        with company_read_scope(scope, permission="accounting.ledger.view"):
            rows = general_ledger(
                company_id,
                account_id=account_id,
                date_from=date_from,
                date_to=date_to,
                limit=limit + 1,
                after_date=cursor_date,
                after_entry=cursor_entry,
                after_line=cursor_line,
            )
        last = rows[limit - 1] if len(rows) > limit else None
        return Response(
            {
                "account_id": str(account_id),
                "date_from": str(date_from),
                "date_to": str(date_to),
                "results": rows[:limit],
                "next_cursor": {"cursor_date": str(last["entry_date"]),
                                "cursor_entry": str(last["entry_id"]),
                                "cursor_line": last["line_no"]} if last else None,
            }
        )


class TaxComponentsView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(responses=OpenApiTypes.OBJECT, operation_id="report_tax_components")
    def get(self, request: Request, tenant_id: uuid.UUID, company_id: uuid.UUID) -> Response:
        date_from = _date(request, "date_from")
        date_to = _date(request, "date_to")
        if date_from > date_to:
            raise APIError(code="INVALID_FILTER", message="date_from must not exceed date_to.")
        limit, after = _page(request)
        with company_read_scope(_scope(request, tenant_id, company_id),
                                permission="reports.view", module="tax_calculation"):
            rows = tax_components(company_id, date_from=date_from, date_to=date_to,
                                  limit=limit + 1, after=after)
        return Response({"date_from": str(date_from), "date_to": str(date_to),
                         "results": rows[:limit],
                         "next_cursor": str(rows[limit - 1]["id"])
                         if len(rows) > limit else None})
