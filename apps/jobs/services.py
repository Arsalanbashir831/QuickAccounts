import csv
import datetime as dt
import io
import json
import uuid
from typing import Any, cast

from django.db import IntegrityError, connection, transaction

from apps.parties.api.serializers import PartnerCreateSerializer
from apps.payments.reports import open_items
from apps.reporting.selectors.accounting import general_ledger, tax_components, trial_balance
from common.access.scopes import (
    CompanyScope,
    assert_company_write,
    bind_and_verify_company,
    require_module_read,
    require_permission,
)
from common.api.errors import APIError, Conflict, ScopeNotFound
from common.db.context import set_local_context

JOB_TYPES = {"partner_export": "reports", "partner_import": "imports",
             "financial_report_export": "reports"}
REPORT_TYPES = {
    "trial_balance", "general_ledger", "tax_components", "receivable_aging",
    "payable_aging", "inventory_valuation",
}


def _require_report_access(company_id: uuid.UUID, kind: str) -> None:
    require_permission(company_id, "reports.view")
    if kind == "inventory_valuation":
        require_module_read(company_id, "inventory")
        require_permission(company_id, "inventory.reconcile")
        require_permission(company_id, "accounting.ledger.view")
    elif kind in {"receivable_aging", "payable_aging"}:
        require_module_read(company_id, "payments")
        require_permission(company_id, "payments.view")
    else:
        require_module_read(company_id, "accounting")
        if kind == "general_ledger":
            require_permission(company_id, "accounting.ledger.view")
        if kind == "tax_components":
            require_module_read(company_id, "tax_calculation")


def _row(sql: str, params: list[object]) -> tuple[Any, ...] | None:
    with connection.cursor() as cursor:
        cursor.execute(sql, params)  # type: ignore[arg-type]
        return cast(tuple[Any, ...] | None, cursor.fetchone())


def _decoded(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def _job(company_id: uuid.UUID, job_id: uuid.UUID) -> dict[str, Any]:
    row = _row(
        "SELECT id,job_type,job_key,status,progress,attempt_count,max_attempts,"
        "run_after,result_metadata,last_error_code,last_error_message,created_at,finished_at "
        "FROM erp.background_jobs WHERE company_id=%s AND id=%s",
        [company_id, job_id],
    )
    if row is None:
        raise ScopeNotFound()
    keys = (
        "id",
        "job_type",
        "job_key",
        "status",
        "progress",
        "attempt_count",
        "max_attempts",
        "run_after",
        "result_metadata",
        "last_error_code",
        "last_error_message",
        "created_at",
        "finished_at",
    )
    result = dict(zip(keys, row, strict=True))
    result["result_metadata"] = _decoded(result["result_metadata"])
    return result


def submit_job(
    scope: CompanyScope, job_type: str, job_key: str, rows: list[dict[str, Any]] | None,
    *, report: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if job_type not in JOB_TYPES or not 1 <= len(job_key) <= 200:
        raise APIError(code="INVALID_JOB", message="Unsupported job type or key.")
    if job_type == "partner_import" and (not rows or len(rows) > 100):
        raise APIError(code="INVALID_IMPORT", message="Supply between 1 and 100 partner rows.")
    if job_type == "partner_export" and rows is not None:
        raise APIError(code="INVALID_EXPORT", message="Exports do not accept rows.")
    if job_type != "financial_report_export" and report is not None:
        raise APIError(code="INVALID_REPORT", message="This job does not accept report filters.")
    if job_type == "financial_report_export":
        if rows is not None or not isinstance(report, dict) or set(report) not in (
            {"type", "date_from", "date_to"},
            {"type", "date_from", "date_to", "account_id"},
        ) or report.get("type") not in REPORT_TYPES:
            raise APIError(code="INVALID_REPORT", message="Unsupported report filters.")
        try:
            date_from = dt.date.fromisoformat(report["date_from"])
            date_to = dt.date.fromisoformat(report["date_to"])
            if date_from > date_to:
                raise ValueError
            if report["type"] == "general_ledger":
                uuid.UUID(report["account_id"])
            elif "account_id" in report:
                raise ValueError
        except (KeyError, TypeError, ValueError) as exc:
            raise APIError(
                code="INVALID_REPORT", message="Invalid report date or account."
            ) from exc
    parameters = {"rows": rows} if rows is not None else ({"report": report} if report else {})
    with transaction.atomic():
        bind_and_verify_company(scope)
        if job_type == "partner_import":
            assert_company_write(scope.company_id, "core", "party.manage")
        elif job_type == "financial_report_export":
            assert report is not None
            _require_report_access(scope.company_id, str(report["type"]))
            require_permission(scope.company_id, "party.view")
        else:
            require_module_read(scope.company_id, "core")
            require_permission(scope.company_id, "party.view")
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO erp.background_jobs(tenant_id,company_id,requester_user_id,job_type,"
                "queue_name,job_key,parameters) VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb) "
                "ON CONFLICT(tenant_id,company_id,job_type,job_key) DO NOTHING RETURNING id",
                [
                    scope.tenant_id,
                    scope.company_id,
                    scope.user_id,
                    job_type,
                    JOB_TYPES[job_type],
                    job_key,
                    json.dumps(parameters),
                ],
            )
            inserted = cursor.fetchone()
            if inserted is None:
                existing = _row(
                    "SELECT id,requester_user_id,parameters FROM erp.background_jobs "
                    "WHERE tenant_id=%s AND company_id=%s AND job_type=%s AND job_key=%s",
                    [scope.tenant_id, scope.company_id, job_type, job_key],
                )
                assert existing is not None
                if str(existing[1]) != str(scope.user_id) or json.dumps(
                    _decoded(existing[2]), sort_keys=True
                ) != json.dumps(parameters, sort_keys=True):
                    raise Conflict("JOB_KEY_REUSED", "The job key belongs to a different request.")
                job_id = existing[0]
            else:
                job_id = inserted[0]
        return _job(scope.company_id, job_id)


def list_jobs(scope: CompanyScope, limit: int = 50) -> list[dict[str, Any]]:
    with transaction.atomic():
        bind_and_verify_company(scope)
        require_module_read(scope.company_id, "core")
        require_permission(scope.company_id, "party.view")
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT id FROM erp.background_jobs WHERE company_id=%s AND job_type IN "
                "('partner_export','partner_import','financial_report_export') "
                "ORDER BY created_at DESC,id DESC LIMIT %s",
                [scope.company_id, limit],
            )
            return [_job(scope.company_id, row[0]) for row in cursor.fetchall()]


def job_status(scope: CompanyScope) -> dict[str, Any]:
    with transaction.atomic():
        bind_and_verify_company(scope)
        require_module_read(scope.company_id, "core")
        require_permission(scope.company_id, "party.view")
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT status,count(*) FROM erp.background_jobs WHERE company_id=%s "
                "AND job_type IN ('partner_export','partner_import','financial_report_export') "
                "GROUP BY status",
                [scope.company_id],
            )
            counts = {status: count for status, count in cursor.fetchall()}
            cursor.execute(
                "SELECT count(*) FROM erp.outbox_events "
                "WHERE company_id=%s AND delivered_at IS NULL",
                [scope.company_id],
            )
            pending_events = cursor.fetchone()[0]
            cursor.execute(
                "SELECT count(*) FROM erp.internal_event_deliveries WHERE company_id=%s",
                [scope.company_id],
            )
            delivered_events = cursor.fetchone()[0]
        return {
            "jobs": counts,
            "outbox_pending": pending_events,
            "internal_deliveries": delivered_events,
        }


def job_detail(scope: CompanyScope, job_id: uuid.UUID) -> dict[str, Any]:
    with transaction.atomic():
        bind_and_verify_company(scope)
        require_module_read(scope.company_id, "core")
        require_permission(scope.company_id, "party.view")
        return _job(scope.company_id, job_id)


def cancel_job(scope: CompanyScope, job_id: uuid.UUID) -> dict[str, Any]:
    with transaction.atomic():
        bind_and_verify_company(scope)
        require_module_read(scope.company_id, "core")
        require_permission(scope.company_id, "party.manage")
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE erp.background_jobs SET status='cancelled',finished_at=clock_timestamp() "
                "WHERE company_id=%s AND id=%s AND status='queued' RETURNING id",
                [scope.company_id, job_id],
            )
            if cursor.fetchone() is None:
                raise Conflict("JOB_NOT_CANCELLABLE", "Only queued jobs can be cancelled.")
        return _job(scope.company_id, job_id)


def artifact(scope: CompanyScope, job_id: uuid.UUID) -> tuple[str, bytes]:
    with transaction.atomic():
        bind_and_verify_company(scope)
        require_module_read(scope.company_id, "core")
        require_permission(scope.company_id, "party.view")
        row = _row(
            "SELECT filename,content FROM erp.job_artifacts WHERE company_id=%s AND job_id=%s",
            [scope.company_id, job_id],
        )
        if row is None:
            raise ScopeNotFound()
        return str(row[0]), bytes(row[1])


def relay_internal_events(scope: CompanyScope, limit: int = 100) -> int:
    if not 1 <= limit <= 100:
        raise ValueError("limit must be 1..100")
    with transaction.atomic():
        bind_and_verify_company(scope)
        require_module_read(scope.company_id, "core")
        require_permission(scope.company_id, "party.view")
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT id,event_type,aggregate_type,aggregate_id,payload "
                "FROM erp.outbox_events WHERE company_id=%s AND delivered_at IS NULL "
                "ORDER BY occurred_at,id FOR UPDATE SKIP LOCKED LIMIT %s",
                [scope.company_id, limit],
            )
            events = cursor.fetchall()
            for event_id, event_type, aggregate_type, aggregate_id, payload in events:
                cursor.execute(
                    "INSERT INTO erp.internal_event_deliveries(company_id,outbox_event_id,"
                    "event_type,aggregate_type,aggregate_id,payload) "
                    "VALUES (%s,%s,%s,%s,%s,%s::jsonb) ON CONFLICT(outbox_event_id) DO NOTHING",
                    [
                        scope.company_id,
                        event_id,
                        event_type,
                        aggregate_type,
                        aggregate_id,
                        json.dumps(_decoded(payload)),
                    ],
                )
                cursor.execute(
                    "UPDATE erp.outbox_events SET delivered_at=clock_timestamp(),"
                    "claim_token=NULL,claim_expires_at=NULL,last_error=NULL "
                    "WHERE company_id=%s AND id=%s AND delivered_at IS NULL",
                    [scope.company_id, event_id],
                )
        return len(events)


def _export_partners(
    company_id: uuid.UUID, job_id: uuid.UUID, tenant_id: uuid.UUID
) -> dict[str, Any]:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT partner_code,display_name,partner_kind,email,phone,is_active "
            "FROM erp.business_partners WHERE company_id=%s ORDER BY partner_code,id LIMIT 10001",
            [company_id],
        )
        records = cursor.fetchall()
        if len(records) > 10000:
            raise APIError(code="EXPORT_TOO_LARGE", message="Partner export exceeds 10,000 rows.")
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow(
            ["partner_code", "display_name", "partner_kind", "email", "phone", "is_active"]
        )
        for record in records:
            writer.writerow(
                "'" + value
                if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@"))
                else value
                for value in record
            )
        content = output.getvalue().encode("utf-8")
        if len(content) > 2097152:
            raise APIError(code="EXPORT_TOO_LARGE", message="Partner export exceeds 2 MiB.")
        cursor.execute(
            "INSERT INTO erp.job_artifacts(tenant_id,company_id,job_id,"
            "content_type,filename,content) "
            "VALUES (%s,%s,%s,'text/csv','partners.csv',%s)",
            [tenant_id, company_id, job_id, content],
        )
    return {"row_count": len(records), "artifact": True}


def _safe_csv(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def _export_financial_report(
    company_id: uuid.UUID, job_id: uuid.UUID, tenant_id: uuid.UUID, report: dict[str, str]
) -> dict[str, Any]:
    date_from = dt.date.fromisoformat(report["date_from"])
    date_to = dt.date.fromisoformat(report["date_to"])
    kind = report["type"]
    if kind == "trial_balance":
        records = trial_balance(company_id, date_from=date_from, date_to=date_to, limit=10001)
    elif kind == "general_ledger":
        records = general_ledger(company_id, account_id=uuid.UUID(report["account_id"]),
                                 date_from=date_from, date_to=date_to, limit=10001)
    elif kind == "tax_components":
        records = tax_components(company_id, date_from=date_from, date_to=date_to, limit=10001)
    elif kind in {"receivable_aging", "payable_aging"}:
        direction = "receipt" if kind == "receivable_aging" else "disbursement"
        records = []
        page_cursor: uuid.UUID | None = None
        while len(records) <= 10000:
            page = open_items(company_id, direction, cutoff=date_to, limit=200,
                              after=page_cursor, aging=True)
            records.extend(page)
            if len(page) < 200:
                break
            page_cursor = uuid.UUID(str(page[-1]["id"]))
    else:
        cutoff = dt.datetime.combine(date_to + dt.timedelta(days=1), dt.time(), dt.UTC)
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT warehouse_id,item_id,lot_id,sum(quantity_delta) quantity,"
                "sum(value_delta_company) value_company "
                "FROM erp.stock_movements WHERE company_id=%s AND occurred_at<%s "
                "GROUP BY warehouse_id,item_id,lot_id "
                "HAVING sum(quantity_delta)<>0 OR sum(value_delta_company)<>0 "
                "ORDER BY warehouse_id,item_id,lot_id LIMIT 10001",
                [company_id, cutoff],
            )
            names = [column.name for column in cursor.description]
            records = [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]
    if len(records) > 10000:
        raise APIError(code="EXPORT_TOO_LARGE", message="Report export exceeds 10,000 rows.")
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    aging_columns = ["id", "document_no", "partner_id", "currency_code",
                     "document_date", "due_date", "item_kind", "gross_total",
                     "applied_amount", "open_amount", "days_overdue", "aging_bucket"]
    columns = {
        "trial_balance": ["account_id", "account_code", "account_name", "account_type",
                          "normal_balance", "currency_code", "opening_net_debit",
                          "period_debits", "period_credits", "closing_net_debit"],
        "general_ledger": ["entry_id", "entry_number", "entry_date", "entry_description",
                           "line_id", "line_no", "description", "transaction_currency",
                           "exchange_rate", "transaction_debit", "transaction_credit",
                           "debit_amount", "credit_amount", "opening_balance",
                           "running_balance"],
        "tax_components": ["id", "source_type", "document_id", "document_no",
                           "document_date", "tax_jurisdiction_id", "tax_rate_version_id",
                           "tax_code_component_id", "taxable_base_amount", "rate_snapshot",
                           "tax_amount", "recoverable_amount", "tax_treatment"],
        "receivable_aging": aging_columns,
        "payable_aging": aging_columns,
        "inventory_valuation": ["warehouse_id", "item_id", "lot_id", "quantity",
                                "value_company"],
    }[kind]
    writer.writerow(columns)
    for row in records:
        writer.writerow([_safe_csv(row.get(column)) for column in columns])
    content = output.getvalue().encode("utf-8")
    if len(content) > 2097152:
        raise APIError(code="EXPORT_TOO_LARGE", message="Report export exceeds 2 MiB.")
    with connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO erp.job_artifacts(tenant_id,company_id,job_id,"
            "content_type,filename,content) "
            "VALUES (%s,%s,%s,'text/csv',%s,%s)",
            [tenant_id, company_id, job_id, f"{kind}.csv", content],
        )
    return {"row_count": len(records), "artifact": True, "report_type": kind}


def _import_partners(company_id: uuid.UUID, rows: list[dict[str, Any]]) -> dict[str, Any]:
    outcomes: list[dict[str, Any]] = []
    with connection.cursor() as cursor:
        for index, raw in enumerate(rows, start=1):
            serializer = PartnerCreateSerializer(data=raw)
            if not serializer.is_valid():
                outcomes.append({"row": index, "status": "invalid", "errors": serializer.errors})
                continue
            data = serializer.validated_data
            try:
                with transaction.atomic():
                    cursor.execute(
                        "INSERT INTO erp.business_partners(company_id,partner_code,display_name,"
                        "legal_name,partner_kind,email,phone,is_active) "
                        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                        [
                            company_id,
                            data["partner_code"],
                            data["display_name"],
                            data.get("legal_name"),
                            data["partner_kind"],
                            data.get("email"),
                            data.get("phone"),
                            data["is_active"],
                        ],
                    )
                outcomes.append({"row": index, "status": "created"})
            except IntegrityError:
                outcomes.append({"row": index, "status": "conflict"})
    return {
        "row_count": len(rows),
        "created_count": sum(x["status"] == "created" for x in outcomes),
        "outcomes": outcomes,
    }


def _process_claim(tenant_id: uuid.UUID, job_id: uuid.UUID, token: uuid.UUID) -> None:
    with transaction.atomic():
        row = _row(
            "SELECT company_id,requester_user_id,job_type,parameters FROM erp.background_jobs "
            "WHERE tenant_id=%s AND id=%s AND status='running' AND claim_token=%s "
            "AND claim_expires_at>clock_timestamp() FOR UPDATE",
            [tenant_id, job_id, token],
        )
        if row is None:
            return
        company_id, user_id, job_type, raw_parameters = row
        parameters = _decoded(raw_parameters)
        if company_id is None or user_id is None:
            raise APIError(code="JOB_SCOPE_INVALID", message="Job has no company requester.")
        scope = CompanyScope(tenant_id, company_id, user_id)
        bind_and_verify_company(scope)
        if job_type == "partner_import":
            assert_company_write(company_id, "core", "party.manage")
            result = _import_partners(company_id, cast(list[dict[str, Any]], parameters["rows"]))
        elif job_type == "partner_export":
            require_module_read(company_id, "core")
            require_permission(company_id, "party.view")
            result = _export_partners(company_id, job_id, tenant_id)
        elif job_type == "financial_report_export":
            report = cast(dict[str, str], parameters["report"])
            _require_report_access(company_id, report["type"])
            require_permission(company_id, "party.view")
            result = _export_financial_report(company_id, job_id, tenant_id, report)
        else:
            raise APIError(code="UNKNOWN_JOB", message="Unsupported internal job type.")
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE erp.background_jobs SET status='succeeded',progress=100,"
                "result_metadata=%s::jsonb,finished_at=clock_timestamp(),claim_token=NULL,"
                "claim_expires_at=NULL WHERE tenant_id=%s AND id=%s AND claim_token=%s",
                [json.dumps(result), tenant_id, job_id, token],
            )


def run_jobs_once(
    tenant_id: uuid.UUID, actor_user_id: uuid.UUID, queue: str, limit: int = 10
) -> int:
    if queue not in {"reports", "imports"} or not 1 <= limit <= 100:
        raise ValueError("unsupported queue or limit")
    with transaction.atomic():
        set_local_context(tenant_id=tenant_id, user_id=actor_user_id)
        active = _row(
            "SELECT 1 FROM identity.tenant_memberships tm JOIN identity.users u "
            "ON u.id=tm.user_id WHERE tm.tenant_id=%s AND tm.user_id=%s "
            "AND tm.is_active AND u.is_active",
            [tenant_id, actor_user_id],
        )
        if active is None:
            raise ScopeNotFound()
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE erp.background_jobs SET status='failed',finished_at=clock_timestamp(),"
                "claim_token=NULL,claim_expires_at=NULL,last_error_code='ATTEMPTS_EXHAUSTED' "
                "WHERE tenant_id=%s AND queue_name=%s AND status='running' "
                "AND claim_expires_at<clock_timestamp() AND attempt_count>=max_attempts",
                [tenant_id, queue],
            )
            cursor.execute(
                "WITH selected AS (SELECT id FROM erp.background_jobs "
                "WHERE tenant_id=%s AND queue_name=%s "
                "AND job_type IN ('partner_import','partner_export','financial_report_export') "
                "AND attempt_count<max_attempts AND "
                "((status='queued' AND run_after<=clock_timestamp()) OR "
                "(status='running' AND claim_expires_at<clock_timestamp())) "
                "ORDER BY run_after,id FOR UPDATE SKIP LOCKED LIMIT %s) "
                "UPDATE erp.background_jobs j SET status='running',claim_token=uuidv7(),"
                "claim_expires_at=clock_timestamp()+interval '120 seconds',"
                "heartbeat_at=clock_timestamp(),attempt_count=j.attempt_count+1,"
                "started_at=coalesce(j.started_at,clock_timestamp()),finished_at=NULL "
                "FROM selected s WHERE j.id=s.id "
                "RETURNING j.id,j.claim_token,j.requester_user_id",
                [tenant_id, queue, limit],
            )
            claimed = cursor.fetchall()
    for job_id, token, requester in claimed:
        try:
            with transaction.atomic():
                set_local_context(tenant_id=tenant_id, user_id=requester)
                _process_claim(tenant_id, job_id, token)
        except Exception as exc:
            with transaction.atomic():
                set_local_context(tenant_id=tenant_id, user_id=actor_user_id)
                with connection.cursor() as cursor:
                    cursor.execute(
                        "UPDATE erp.background_jobs SET "
                        "status=CASE WHEN attempt_count>=max_attempts "
                        "THEN 'failed' ELSE 'queued' END,"
                        "run_after=clock_timestamp()+make_interval(secs=>LEAST(300,attempt_count*10)),"
                        "finished_at=CASE WHEN attempt_count>=max_attempts "
                        "THEN clock_timestamp() ELSE NULL END,"
                        "claim_token=NULL,claim_expires_at=NULL,"
                        "last_error_code='JOB_FAILED',last_error_message=%s "
                        "WHERE tenant_id=%s AND id=%s AND claim_token=%s",
                        [type(exc).__name__[:100], tenant_id, job_id, token],
                    )
    return len(claimed)
