import datetime as dt
import decimal
import hashlib
import json
import uuid
from typing import Any, cast

from django.db import DatabaseError, connection, transaction

from common.access.scopes import CompanyScope, assert_company_write, bind_and_verify_company
from common.api.errors import APIError, Conflict, PreconditionFailed, ScopeNotFound

D = decimal.Decimal
HEADER_FIELDS = (
    "payment_no",
    "direction",
    "partner_id",
    "cash_account_id",
    "payment_date",
    "currency_code",
    "exchange_rate",
    "amount",
    "payment_method",
    "external_reference",
)


def _rows(sql: str, params: list[Any]) -> list[dict[str, Any]]:
    with connection.cursor() as c:
        c.execute(sql, params)
        names = [col.name for col in c.description]
        return [dict(zip(names, row, strict=True)) for row in c.fetchall()]


def _run(sql: str, params: list[Any]) -> None:
    with connection.cursor() as c:
        c.execute(sql, params)


def _json(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _json(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json(v) for v in value]
    if isinstance(value, (uuid.UUID, D, dt.date)):
        return str(value)
    return value


def _money(value: decimal.Decimal, precision: int) -> decimal.Decimal:
    return value.quantize(D(1).scaleb(-precision), rounding=decimal.ROUND_HALF_UP)


def _tables(direction: str) -> tuple[str, str, str, str]:
    if direction == "receipt":
        return "ar_receipt_allocations", "sales_invoices", "sales_invoice_lines", "sales_invoice_id"
    return (
        "ap_disbursement_allocations",
        "purchase_bills",
        "purchase_bill_lines",
        "purchase_bill_id",
    )


def payment_detail(company_id: uuid.UUID, payment_id: uuid.UUID) -> dict[str, Any]:
    rows = _rows(
        "SELECT * FROM erp.payments WHERE company_id=%s AND id=%s", [company_id, payment_id]
    )
    if not rows:
        raise ScopeNotFound()
    result = rows[0]
    table, _, _, column = _tables(result["direction"])
    result["allocations"] = _rows(
        f"SELECT *,{column} AS document_id FROM erp.{table} "
        "WHERE company_id=%s AND payment_id=%s ORDER BY id",
        [company_id, payment_id],
    )
    result["tax_components"] = _rows(
        "SELECT * FROM erp.payment_tax_components WHERE company_id=%s AND payment_id=%s "
        "ORDER BY id",
        [company_id, payment_id],
    )
    result["allocation_compensations"] = _rows(
        "SELECT * FROM erp.payment_allocation_reversals "
        "WHERE company_id=%s AND reversal_payment_id=%s ORDER BY id",
        [company_id, payment_id],
    )
    result["settlement_total"] = result["amount"] + result["withholding_total"]
    result["allocated_total"] = sum(
        (row["applied_payment_amount"] for row in result["allocations"]), D(0)
    )
    result["unallocated_total"] = (
        result["settlement_total"] - result["allocated_total"]
        if result["reversal_of_payment_id"] is None
        else D(0)
    )
    return cast(dict[str, Any], _json(result))


def list_payments(
    company_id: uuid.UUID, *, limit: int, status: str | None = None, after: uuid.UUID | None = None
) -> list[dict[str, Any]]:
    return cast(
        list[dict[str, Any]],
        _json(
            _rows(
                "SELECT id,payment_no,direction,payment_date,amount,withholding_total,"
                "currency_code,"
                "status,row_version FROM erp.payments WHERE company_id=%s "
                "AND (%s::text IS NULL OR status=%s) AND (%s::uuid IS NULL OR id>%s) "
                "ORDER BY id LIMIT %s",
                [company_id, status, status, after, after, limit],
            )
        ),
    )


def _claim(
    scope: CompanyScope, operation: str, resource: str, key: str, payload: dict[str, Any]
) -> tuple[uuid.UUID, dict[str, Any] | None]:
    digest = hashlib.sha256(json.dumps(_json(payload), sort_keys=True).encode()).digest()
    _run(
        "INSERT INTO erp.api_command_receipts(tenant_id,company_id,actor_user_id,operation,"
        "resource_key,idempotency_key,request_hash,retain_until) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,clock_timestamp()+interval '30 days') "
        "ON CONFLICT DO NOTHING",
        [scope.tenant_id, scope.company_id, scope.user_id, operation, resource, key, digest],
    )
    row = _rows(
        "SELECT * FROM erp.api_command_receipts WHERE tenant_id=%s AND company_id=%s "
        "AND actor_user_id=%s AND actor_service_id IS NULL AND operation=%s "
        "AND resource_key=%s AND idempotency_key=%s FOR UPDATE",
        [scope.tenant_id, scope.company_id, scope.user_id, operation, resource, key],
    )[0]
    if bytes(row["request_hash"]) != digest:
        raise Conflict("IDEMPOTENCY_PAYLOAD_MISMATCH", "The key was used with another payload.")
    body = row["response_body"] if row["status"] == "completed" else None
    if isinstance(body, str):
        body = json.loads(body)
    return row["id"], body


def _complete(receipt_id: uuid.UUID, result: dict[str, Any], status: int = 200) -> None:
    _run(
        "UPDATE erp.api_command_receipts SET status='completed',response_status=%s,"
        "response_body=%s::jsonb,completed_at=clock_timestamp(),result_type='payment',"
        "result_id=%s WHERE id=%s",
        [status, json.dumps(result), result["id"], receipt_id],
    )


def _context(request_id: str | None) -> None:
    _run("SELECT set_config('app.request_id',%s,true)", [request_id or ""])


def _effect(scope: CompanyScope, payment_id: uuid.UUID, action: str) -> None:
    _run(
        "INSERT INTO erp.outbox_events(company_id,event_key,aggregate_type,aggregate_id,"
        "event_type,payload) VALUES (%s,%s,'payment',%s,%s,%s::jsonb)",
        [
            scope.company_id,
            f"{action}:{payment_id}",
            payment_id,
            action,
            json.dumps({"payment_id": str(payment_id)}),
        ],
    )


def _lock(scope: CompanyScope, payment_id: uuid.UUID, revision: int) -> dict[str, Any]:
    rows = _rows(
        "SELECT * FROM erp.payments WHERE company_id=%s AND id=%s FOR UPDATE",
        [scope.company_id, payment_id],
    )
    if not rows:
        raise ScopeNotFound()
    payment = rows[0]
    if payment["status"] != "draft":
        raise Conflict("PAYMENT_NOT_DRAFT", "Only draft payments can be changed or posted.")
    if payment["row_version"] != revision:
        raise PreconditionFailed(payment["row_version"])
    return payment


def _validate_header(company_id: uuid.UUID, header: dict[str, Any]) -> int:
    currency = _rows(
        "SELECT minor_units FROM erp.currencies WHERE code=%s AND is_active",
        [header["currency_code"]],
    )
    if not currency:
        raise APIError(code="INVALID_CURRENCY", message="The currency is unavailable.")
    precision = currency[0]["minor_units"]
    if _money(header["amount"], precision) != header["amount"]:
        raise Conflict("CURRENCY_PRECISION_INVALID", "Cash exceeds the currency precision.")
    company = _rows("SELECT functional_currency FROM erp.companies WHERE id=%s", [company_id])[0]
    if header["currency_code"] == company["functional_currency"] and header["exchange_rate"] != 1:
        raise Conflict("EXCHANGE_RATE_MISMATCH", "Functional-currency payments require rate 1.")
    _account(company_id, header["cash_account_id"], {"asset"})
    if header.get("partner_id"):
        kind = "customer" if header["direction"] == "receipt" else "supplier"
        partner = _rows(
            "SELECT id FROM erp.business_partners WHERE company_id=%s AND id=%s AND is_active "
            "AND partner_kind IN (%s,'both')",
            [company_id, header["partner_id"], kind],
        )
        if not partner:
            raise Conflict(
                "INVALID_PAYMENT_PARTNER", "The active partner has an incompatible role."
            )
    return int(precision)


def _account(company_id: uuid.UUID, account_id: uuid.UUID | None, types: set[str]) -> None:
    rows = _rows(
        "SELECT account_type FROM erp.accounts WHERE company_id=%s AND id=%s "
        "AND is_active AND allow_posting FOR SHARE",
        [company_id, account_id],
    )
    if not rows or rows[0]["account_type"] not in types:
        raise Conflict(
            "INVALID_PAYMENT_ACCOUNT", "An active posting account of the right type is required."
        )


def create_payment(
    scope: CompanyScope, data: dict[str, Any], *, request_id: str | None, idempotency_key: str
) -> dict[str, Any]:
    with transaction.atomic(durable=True):
        bind_and_verify_company(scope)
        receipt, replay = _claim(scope, "payment.create", "collection", idempotency_key, data)
        assert_company_write(scope.company_id, "payments", "payments.edit_draft")
        if replay is not None:
            return replay
        _context(request_id)
        _validate_header(scope.company_id, data)
        placeholders = ",".join(["%s"] * len(HEADER_FIELDS))
        row = _rows(
            f"INSERT INTO erp.payments(company_id,{','.join(HEADER_FIELDS)}) "
            f"VALUES (%s,{placeholders}) RETURNING id",
            [scope.company_id, *(data.get(field) for field in HEADER_FIELDS)],
        )[0]
        result = payment_detail(scope.company_id, row["id"])
        _complete(receipt, result, 201)
        return result


def update_payment(
    scope: CompanyScope,
    payment_id: uuid.UUID,
    data: dict[str, Any],
    *,
    expected_revision: int,
    request_id: str | None,
) -> dict[str, Any]:
    with transaction.atomic(durable=True):
        bind_and_verify_company(scope)
        assert_company_write(scope.company_id, "payments", "payments.edit_draft")
        payment = _lock(scope, payment_id, expected_revision)
        _context(request_id)
        _validate_header(scope.company_id, payment | data)
        # Financial header changes invalidate both calculated withholding and allocation proposals.
        if set(data) & {
            "direction",
            "partner_id",
            "payment_date",
            "currency_code",
            "exchange_rate",
            "amount",
        }:
            for table in (
                "ar_receipt_allocations",
                "ap_disbursement_allocations",
                "payment_tax_components",
            ):
                _run(
                    f"DELETE FROM erp.{table} WHERE company_id=%s AND payment_id=%s",
                    [scope.company_id, payment_id],
                )
            _run(
                "UPDATE erp.payments SET withholding_total=0 WHERE company_id=%s AND id=%s",
                [scope.company_id, payment_id],
            )
        fields = [field for field in HEADER_FIELDS if field in data]
        _run(
            f"UPDATE erp.payments SET {','.join(f'{field}=%s' for field in fields)} "
            "WHERE company_id=%s AND id=%s",
            [*(data[field] for field in fields), scope.company_id, payment_id],
        )
        return payment_detail(scope.company_id, payment_id)


def _validate_allocations(
    company_id: uuid.UUID, payment: dict[str, Any], allocations: list[dict[str, Any]]
) -> None:
    table, documents, lines, column = _tables(payment["direction"])
    if len(allocations) > 50 or len({a["document_id"] for a in allocations}) != len(allocations):
        raise Conflict("INVALID_ALLOCATION_COUNT", "At most 50 distinct documents are allowed.")
    precision = _validate_header(company_id, payment)
    total = sum((a["applied_payment_amount"] for a in allocations), D(0))
    if total > payment["amount"] + payment["withholding_total"]:
        raise Conflict("ALLOCATION_EXCEEDS_PAYMENT", "Allocations exceed cash plus withholding.")
    for allocation in sorted(allocations, key=lambda a: str(a["document_id"])):
        doc_id = allocation["document_id"]
        docs = _rows(
            f"SELECT * FROM erp.{documents} WHERE company_id=%s AND id=%s FOR UPDATE",
            [company_id, doc_id],
        )
        if not docs:
            raise Conflict("INVALID_ALLOCATION_TARGET", "The allocation document is unavailable.")
        doc = docs[0]
        partner_column = "partner_id" if payment["direction"] == "receipt" else "supplier_id"
        kind = "invoice" if payment["direction"] == "receipt" else "bill"
        if (
            doc["status"] != "posted"
            or doc["document_kind"] != kind
            or doc[partner_column] != payment["partner_id"]
            or doc["currency_code"] != payment["currency_code"]
            or allocation["allocation_exchange_rate"] != 1
            or allocation["applied_document_amount"] != allocation["applied_payment_amount"]
            or doc["exchange_rate"] != payment["exchange_rate"]
        ):
            raise Conflict(
                "INVALID_ALLOCATION_TARGET",
                "Partner, direction, currency and exchange rate must match the source.",
            )
        document_date = doc["issue_date"] if payment["direction"] == "receipt" else doc["bill_date"]
        if payment["payment_date"] < document_date:
            raise Conflict(
                "PAYMENT_PRECEDES_DOCUMENT", "Payment cannot precede the allocated document."
            )
        amount = allocation["applied_document_amount"]
        if amount <= 0 or _money(amount, precision) != amount:
            raise Conflict("CURRENCY_PRECISION_INVALID", "Allocation precision is invalid.")
        gross = _rows(
            f"SELECT coalesce(sum(gross_amount),0) AS amount FROM erp.{lines} "
            f"WHERE company_id=%s AND {column}=%s",
            [company_id, doc_id],
        )[0]["amount"]
        applied = _rows(
            f"SELECT coalesce(sum(a.applied_document_amount),0) AS amount FROM erp.{table} a "
            "JOIN erp.payments p ON p.company_id=a.company_id AND p.id=a.payment_id "
            f"WHERE a.company_id=%s AND a.{column}=%s AND p.status='posted' "
            "AND NOT EXISTS (SELECT 1 FROM erp.payments r WHERE r.company_id=p.company_id "
            "AND r.reversal_of_payment_id=p.id AND r.status='posted' "
            "AND r.payment_date<=%s)",
            [company_id, doc_id, payment["payment_date"]],
        )[0]["amount"]
        if applied + amount > gross:
            raise Conflict(
                "ALLOCATION_EXCEEDS_REMAINING", "Allocation exceeds the remaining balance."
            )


def replace_allocations(
    scope: CompanyScope,
    payment_id: uuid.UUID,
    allocations: list[dict[str, Any]],
    *,
    expected_revision: int,
    request_id: str | None = None,
) -> dict[str, Any]:
    with transaction.atomic(durable=True):
        bind_and_verify_company(scope)
        assert_company_write(scope.company_id, "payments", "payments.edit_draft")
        payment = _lock(scope, payment_id, expected_revision)
        _context(request_id)
        _validate_allocations(scope.company_id, payment, allocations)
        table, _, _, column = _tables(payment["direction"])
        _run(
            f"DELETE FROM erp.{table} WHERE company_id=%s AND payment_id=%s",
            [scope.company_id, payment_id],
        )
        for item in sorted(allocations, key=lambda a: str(a["document_id"])):
            _run(
                f"INSERT INTO erp.{table}(company_id,payment_id,{column},"
                "applied_document_amount,applied_payment_amount,allocation_exchange_rate) "
                "VALUES (%s,%s,%s,%s,%s,%s)",
                [
                    scope.company_id,
                    payment_id,
                    item["document_id"],
                    item["applied_document_amount"],
                    item["applied_payment_amount"],
                    item["allocation_exchange_rate"],
                ],
            )
        _run(
            "UPDATE erp.payments SET updated_at=clock_timestamp() WHERE company_id=%s AND id=%s",
            [scope.company_id, payment_id],
        )
        return payment_detail(scope.company_id, payment_id)


def calculate_withholding(
    scope: CompanyScope,
    payment_id: uuid.UUID,
    proposals: list[dict[str, Any]],
    *,
    expected_revision: int,
    request_id: str | None = None,
) -> dict[str, Any]:
    with transaction.atomic(durable=True):
        bind_and_verify_company(scope)
        assert_company_write(scope.company_id, "payments", "payments.edit_draft")
        assert_company_write(scope.company_id, "tax_calculation", "payments.edit_draft")
        payment = _lock(scope, payment_id, expected_revision)
        _context(request_id)
        precision = _validate_header(scope.company_id, payment)
        _run(
            "DELETE FROM erp.payment_tax_components WHERE company_id=%s AND payment_id=%s",
            [scope.company_id, payment_id],
        )
        total = D(0)
        for proposal in proposals:
            components = _rows(
                "SELECT c.id,c.tax_jurisdiction_id,c.rate_schedule_id,"
                "c.withholding_account_id,v.id AS version_id,v.version_code,"
                "v.calculation_method,v.calculation_base,v.rate,v.fixed_amount,s.tax_type_id "
                "FROM erp.tax_codes tc JOIN erp.tax_code_components c "
                "ON c.company_id=tc.company_id AND c.tax_code_id=tc.id "
                "JOIN erp.tax_rate_schedules s ON s.id=c.rate_schedule_id "
                "AND s.jurisdiction_id=c.tax_jurisdiction_id "
                "JOIN erp.tax_types tt ON tt.id=s.tax_type_id "
                "AND tt.jurisdiction_id=s.jurisdiction_id "
                "JOIN erp.tax_rate_versions v ON v.rate_schedule_id=s.id "
                "AND v.jurisdiction_id=s.jurisdiction_id "
                "WHERE tc.company_id=%s AND tc.id=%s "
                "AND tc.is_active AND tc.tax_scope='withholding' "
                "AND s.is_active AND tt.is_active AND tt.calculation_stage='payment' "
                "AND tt.tax_direction IN ('withheld','bidirectional') "
                "AND v.valid_from<=%s AND (v.valid_to IS NULL OR v.valid_to>%s) "
                "ORDER BY c.sequence_no",
                [
                    scope.company_id,
                    proposal["tax_code_id"],
                    payment["payment_date"],
                    payment["payment_date"],
                ],
            )
            if not components:
                raise Conflict(
                    "WITHHOLDING_CONFIGURATION_INVALID", "No effective withholding components."
                )
            for component in components:
                account_types = (
                    {"asset", "receivable"}
                    if payment["direction"] == "receipt"
                    else {"liability", "payable"}
                )
                _account(scope.company_id, component["withholding_account_id"], account_types)
                base = proposal["taxable_base_amount"]
                method = component["calculation_method"]
                if method == "percentage":
                    tax = _money(base * component["rate"] / 100, precision)
                elif method == "fixed":
                    tax = _money(component["fixed_amount"], precision)
                else:
                    raise Conflict(
                        "WITHHOLDING_METHOD_UNSUPPORTED", "The configured method is unsupported."
                    )
                total += tax
                _run(
                    "INSERT INTO erp.payment_tax_components(company_id,payment_id,tax_code_id,"
                    "tax_code_component_id,tax_jurisdiction_id,rate_schedule_id,tax_rate_version_id,"
                    "taxable_base_amount,rate_snapshot,tax_amount,tax_treatment,"
                    "withholding_account_id_snapshot,rate_version_code_snapshot,"
                    "calculation_method_snapshot,rounding_precision_snapshot,"
                    "currency_code_snapshot,calculation_base_snapshot,fixed_amount_snapshot,"
                    "rounding_method_snapshot,account_role_snapshot,polarity_snapshot) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    [
                        scope.company_id,
                        payment_id,
                        proposal["tax_code_id"],
                        component["id"],
                        component["tax_jurisdiction_id"],
                        component["rate_schedule_id"],
                        component["version_id"],
                        base,
                        component["rate"],
                        tax,
                        proposal["tax_treatment"],
                        component["withholding_account_id"],
                        component["version_code"],
                        method,
                        precision,
                        payment["currency_code"],
                        component["calculation_base"],
                        component["fixed_amount"],
                        "half_up",
                        "withholding",
                        "debit" if payment["direction"] == "receipt" else "credit",
                    ],
                )
        _run(
            "UPDATE erp.payments SET withholding_total=%s WHERE company_id=%s AND id=%s",
            [total, scope.company_id, payment_id],
        )
        return payment_detail(scope.company_id, payment_id)


def _posting_period(
    company_id: uuid.UUID, period_id: uuid.UUID, journal_id: uuid.UUID, date: dt.date
) -> None:
    periods = _rows(
        "SELECT * FROM erp.fiscal_periods WHERE company_id=%s AND id=%s FOR SHARE",
        [company_id, period_id],
    )
    if not periods or periods[0]["state"] != "open":
        raise Conflict("PERIOD_NOT_OPEN", "An open fiscal period is required.")
    if not periods[0]["starts_on"] <= date <= periods[0]["ends_on"]:
        raise Conflict("DATE_OUTSIDE_PERIOD", "The posting date is outside the fiscal period.")
    if not _rows(
        "SELECT id FROM erp.journals WHERE company_id=%s AND id=%s AND is_active FOR SHARE",
        [company_id, journal_id],
    ):
        raise Conflict("INVALID_PAYMENT_JOURNAL", "An active company journal is required.")


def _validate_tax(company_id: uuid.UUID, payment: dict[str, Any]) -> None:
    invalid = _rows(
        "SELECT c.id FROM erp.payment_tax_components c "
        "JOIN erp.tax_codes tc ON tc.company_id=c.company_id AND tc.id=c.tax_code_id "
        "JOIN erp.tax_code_components cc ON cc.company_id=c.company_id "
        "AND cc.id=c.tax_code_component_id "
        "JOIN erp.tax_rate_versions v ON v.id=c.tax_rate_version_id "
        "JOIN erp.tax_rate_schedules s ON s.id=c.rate_schedule_id "
        "JOIN erp.tax_types tt ON tt.id=s.tax_type_id "
        "WHERE c.company_id=%s AND c.payment_id=%s AND (NOT tc.is_active "
        "OR NOT s.is_active OR NOT tt.is_active OR tc.tax_scope<>'withholding' "
        "OR tt.calculation_stage<>'payment' "
        "OR tt.tax_direction NOT IN ('withheld','bidirectional') "
        "OR v.valid_from>%s OR (v.valid_to IS NOT NULL AND v.valid_to<=%s) "
        "OR c.rate_snapshot<>v.rate OR c.rate_version_code_snapshot<>v.version_code "
        "OR c.calculation_method_snapshot<>v.calculation_method "
        "OR c.calculation_base_snapshot<>v.calculation_base "
        "OR c.withholding_account_id_snapshot IS DISTINCT FROM cc.withholding_account_id)",
        [company_id, payment["id"], payment["payment_date"], payment["payment_date"]],
    )
    if invalid:
        raise Conflict(
            "TAX_SNAPSHOT_STALE", "Recalculate withholding using the effective configuration."
        )
    required = _rows(
        "SELECT DISTINCT s.jurisdiction_id,s.tax_type_id FROM erp.payment_tax_components c "
        "JOIN erp.tax_rate_schedules s ON s.id=c.rate_schedule_id "
        "WHERE c.company_id=%s AND c.payment_id=%s",
        [company_id, payment["id"]],
    )
    for tax in required:
        params = [
            company_id,
            tax["jurisdiction_id"],
            tax["tax_type_id"],
            payment["payment_date"],
            payment["payment_date"],
        ]
        company = _rows(
            "SELECT id FROM erp.company_tax_registrations WHERE company_id=%s "
            "AND jurisdiction_id=%s AND tax_type_id=%s AND valid_from<=%s "
            "AND (valid_to IS NULL OR valid_to>%s) AND is_active",
            params,
        )
        partner = _rows(
            "SELECT id FROM erp.partner_tax_registrations WHERE company_id=%s "
            "AND jurisdiction_id=%s AND tax_type_id=%s AND valid_from<=%s "
            "AND (valid_to IS NULL OR valid_to>%s) AND is_active AND is_verified AND partner_id=%s",
            [*params, payment["partner_id"]],
        )
        if not company or not partner:
            raise Conflict(
                "TAX_REGISTRATION_REQUIRED", "Effective verified tax registrations are required."
            )


def _journal(
    scope: CompanyScope,
    payment: dict[str, Any],
    period_id: uuid.UUID,
    journal_id: uuid.UUID,
    key: str,
    reversal_entry: uuid.UUID | None = None,
) -> uuid.UUID:
    number = _rows(
        "SELECT erp.allocate_document_number(%s,%s,'PAYMENT_JOURNAL') AS number",
        [scope.company_id, period_id],
    )[0]["number"]
    entry_id = uuid.uuid4()
    _run(
        "INSERT INTO erp.journal_entries(id,company_id,journal_id,fiscal_period_id,"
        "entry_number,entry_date,source_type,source_id,idempotency_key,created_by,"
        "reversal_of_entry_id) VALUES (%s,%s,%s,%s,%s,%s,'payment',%s,%s,%s,%s)",
        [
            entry_id,
            scope.company_id,
            journal_id,
            period_id,
            number,
            payment["payment_date"],
            payment["id"],
            f"payment:{payment['id']}:{key}",
            scope.user_id,
            reversal_entry,
        ],
    )
    return entry_id


def _line(
    company_id: uuid.UUID,
    entry_id: uuid.UUID,
    index: int,
    account_id: uuid.UUID,
    amount: decimal.Decimal,
    debit: bool,
    payment: dict[str, Any],
    precision: int,
    functional_amount: decimal.Decimal | None = None,
) -> None:
    if amount == 0:
        return
    functional = (
        _money(amount * payment["exchange_rate"], precision)
        if functional_amount is None
        else functional_amount
    )
    _run(
        "INSERT INTO erp.journal_lines(company_id,journal_entry_id,line_no,account_id,"
        "business_partner_id,description,transaction_currency,exchange_rate,"
        "transaction_debit,transaction_credit,debit_amount,credit_amount) "
        "VALUES (%s,%s,%s,%s,%s,'Payment settlement',%s,%s,%s,%s,%s,%s)",
        [
            company_id,
            entry_id,
            index,
            account_id,
            payment["partner_id"],
            payment["currency_code"],
            payment["exchange_rate"],
            amount if debit else 0,
            0 if debit else amount,
            functional if debit else 0,
            0 if debit else functional,
        ],
    )


def post_payment(
    scope: CompanyScope,
    payment_id: uuid.UUID,
    *,
    fiscal_period_id: uuid.UUID,
    journal_id: uuid.UUID,
    expected_revision: int,
    idempotency_key: str,
    request_id: str | None = None,
) -> dict[str, Any]:
    try:
        with transaction.atomic(durable=True):
            bind_and_verify_company(scope)
            receipt, replay = _claim(
                scope,
                "payment.post",
                str(payment_id),
                idempotency_key,
                {
                    "fiscal_period_id": fiscal_period_id,
                    "journal_id": journal_id,
                    "revision": expected_revision,
                },
            )
            assert_company_write(scope.company_id, "payments", "payments.post")
            if replay is not None:
                return replay
            _context(request_id)
            payment = _lock(scope, payment_id, expected_revision)
            if payment["partner_id"] is None:
                raise Conflict(
                    "PAYMENT_PARTNER_REQUIRED", "A partner is required for AR/AP settlement."
                )
            _posting_period(scope.company_id, fiscal_period_id, journal_id, payment["payment_date"])
            table, _, _, column = _tables(payment["direction"])
            allocations = _rows(
                f"SELECT *,{column} AS document_id FROM erp.{table} "
                "WHERE company_id=%s AND payment_id=%s ORDER BY id FOR UPDATE",
                [scope.company_id, payment_id],
            )
            _validate_allocations(scope.company_id, payment, allocations)
            taxes = _rows(
                "SELECT * FROM erp.payment_tax_components WHERE company_id=%s "
                "AND payment_id=%s ORDER BY id FOR UPDATE",
                [scope.company_id, payment_id],
            )
            if sum((tax["tax_amount"] for tax in taxes), D(0)) != payment["withholding_total"]:
                raise Conflict("WITHHOLDING_TOTAL_MISMATCH", "Tax components do not reconcile.")
            if taxes:
                assert_company_write(scope.company_id, "tax_calculation", "payments.post")
                _validate_tax(scope.company_id, payment)
            control_type = "receivable" if payment["direction"] == "receipt" else "payable"
            event = "sales_invoice" if payment["direction"] == "receipt" else "purchase_bill"
            role = (
                "accounts_receivable" if payment["direction"] == "receipt" else "accounts_payable"
            )
            rules = _rows(
                "SELECT account_id FROM erp.accounting_posting_rules WHERE company_id=%s "
                "AND event_code=%s AND role_code=%s",
                [scope.company_id, event, role],
            )
            if not rules:
                raise Conflict(
                    "PAYMENT_POSTING_RULE_MISSING", "A settlement control-account rule is required."
                )
            control_id = rules[0]["account_id"]
            _account(scope.company_id, control_id, {control_type})
            # The historical document control account must match the configured settlement account.
            _, documents, _, _ = _tables(payment["direction"])
            for allocation in allocations:
                account = _rows(
                    "SELECT l.account_id FROM erp.journal_lines l JOIN erp.accounts a "
                    "ON a.company_id=l.company_id AND a.id=l.account_id "
                    f"JOIN erp.{documents} d ON d.company_id=l.company_id "
                    "AND d.journal_entry_id=l.journal_entry_id "
                    "WHERE d.company_id=%s AND d.id=%s AND a.account_type=%s",
                    [scope.company_id, allocation["document_id"], control_type],
                )
                if len(account) != 1 or account[0]["account_id"] != control_id:
                    raise Conflict(
                        "SETTLEMENT_ACCOUNT_MISMATCH", "The source control account differs."
                    )
            precision = _rows(
                "SELECT fc.minor_units FROM erp.companies c JOIN erp.currencies fc "
                "ON fc.code=c.functional_currency WHERE c.id=%s",
                [scope.company_id],
            )[0]["minor_units"]
            entry_id = _journal(scope, payment, fiscal_period_id, journal_id, idempotency_key)
            receipt_direction = payment["direction"] == "receipt"
            _line(
                scope.company_id,
                entry_id,
                1,
                payment["cash_account_id"],
                payment["amount"],
                receipt_direction,
                payment,
                precision,
            )
            _line(
                scope.company_id,
                entry_id,
                2,
                control_id,
                payment["amount"] + payment["withholding_total"],
                not receipt_direction,
                payment,
                precision,
                functional_amount=(
                    _money(payment["amount"] * payment["exchange_rate"], precision)
                    + sum(
                        (
                            _money(tax["tax_amount"] * payment["exchange_rate"], precision)
                            for tax in taxes
                        ),
                        D(0),
                    )
                ),
            )
            for index, tax in enumerate(taxes, 3):
                types = {"asset", "receivable"} if receipt_direction else {"liability", "payable"}
                _account(scope.company_id, tax["withholding_account_id_snapshot"], types)
                _line(
                    scope.company_id,
                    entry_id,
                    index,
                    tax["withholding_account_id_snapshot"],
                    tax["tax_amount"],
                    receipt_direction,
                    payment,
                    precision,
                )
            _run("SELECT erp.post_journal_entry(%s,%s)", [scope.company_id, entry_id])
            _run(
                "UPDATE erp.payments SET status='posted',journal_entry_id=%s,"
                "posted_at=clock_timestamp() WHERE company_id=%s AND id=%s",
                [entry_id, scope.company_id, payment_id],
            )
            _effect(scope, payment_id, "payment.posted")
            result = payment_detail(scope.company_id, payment_id)
            _complete(receipt, result)
            return result
    except DatabaseError as exc:
        raise Conflict(
            "PAYMENT_POSTING_CONFLICT", "Posting failed a database financial invariant."
        ) from exc


def reverse_payment(
    scope: CompanyScope,
    payment_id: uuid.UUID,
    data: dict[str, Any],
    *,
    expected_revision: int,
    idempotency_key: str,
    request_id: str | None = None,
) -> dict[str, Any]:
    with transaction.atomic(durable=True):
        bind_and_verify_company(scope)
        receipt, replay = _claim(
            scope,
            "payment.reverse",
            str(payment_id),
            idempotency_key,
            data | {"revision": expected_revision},
        )
        assert_company_write(scope.company_id, "payments", "payments.reverse")
        if replay is not None:
            return replay
        _context(request_id)
        originals = _rows(
            "SELECT * FROM erp.payments WHERE company_id=%s AND id=%s FOR UPDATE",
            [scope.company_id, payment_id],
        )
        if not originals:
            raise ScopeNotFound()
        original = originals[0]
        if original["status"] != "posted" or original["reversal_of_payment_id"] is not None:
            raise Conflict(
                "PAYMENT_NOT_REVERSIBLE", "Only original posted payments can be reversed."
            )
        if original["row_version"] != expected_revision:
            raise PreconditionFailed(original["row_version"])
        if _rows(
            "SELECT id FROM erp.payments WHERE company_id=%s AND reversal_of_payment_id=%s",
            [scope.company_id, payment_id],
        ):
            raise Conflict("PAYMENT_ALREADY_REVERSED", "The payment already has a reversal.")
        if data["reversal_date"] < original["payment_date"]:
            raise Conflict("INVALID_REVERSAL_DATE", "Reversal cannot precede the payment date.")
        _posting_period(
            scope.company_id, data["fiscal_period_id"], data["journal_id"], data["reversal_date"]
        )
        table, documents, _, column = _tables(original["direction"])
        allocations = _rows(
            f"SELECT * FROM erp.{table} WHERE company_id=%s AND payment_id=%s "
            f"ORDER BY {column} FOR UPDATE",
            [scope.company_id, payment_id],
        )
        for allocation in allocations:
            _rows(
                f"SELECT id FROM erp.{documents} WHERE company_id=%s AND id=%s FOR UPDATE",
                [scope.company_id, allocation[column]],
            )
        reversal_id = uuid.uuid4()
        reversal = original | {"id": reversal_id, "payment_date": data["reversal_date"]}
        _run(
            "INSERT INTO erp.payments(id,company_id,payment_no,direction,partner_id,"
            "cash_account_id,payment_date,currency_code,exchange_rate,amount,payment_method,"
            "reversal_of_payment_id,reversal_reason) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            [
                reversal_id,
                scope.company_id,
                f"REV-{reversal_id}",
                "disbursement" if original["direction"] == "receipt" else "receipt",
                original["partner_id"],
                original["cash_account_id"],
                data["reversal_date"],
                original["currency_code"],
                original["exchange_rate"],
                original["amount"],
                original["payment_method"],
                payment_id,
                data["reason"],
            ],
        )
        tax_fields = (
            "tax_code_id,tax_code_component_id,tax_jurisdiction_id,rate_schedule_id,"
            "tax_rate_version_id,taxable_base_amount,rate_snapshot,tax_amount,tax_treatment,"
            "withholding_account_id_snapshot,rate_version_code_snapshot,"
            "calculation_method_snapshot,calculation_base_snapshot,fixed_amount_snapshot,"
            "rounding_method_snapshot,rounding_precision_snapshot,currency_code_snapshot,"
            "account_role_snapshot"
        )
        _run(
            f"INSERT INTO erp.payment_tax_components(company_id,payment_id,{tax_fields},"
            "polarity_snapshot) "
            f"SELECT company_id,%s,{tax_fields},"
            "CASE polarity_snapshot WHEN 'debit' THEN 'credit' ELSE 'debit' END "
            "FROM erp.payment_tax_components WHERE company_id=%s AND payment_id=%s",
            [reversal_id, scope.company_id, payment_id],
        )
        _run(
            "UPDATE erp.payments SET withholding_total=%s WHERE company_id=%s AND id=%s",
            [original["withholding_total"], scope.company_id, reversal_id],
        )
        entry_id = _journal(
            scope,
            reversal,
            data["fiscal_period_id"],
            data["journal_id"],
            idempotency_key,
            original["journal_entry_id"],
        )
        _run(
            "INSERT INTO erp.journal_lines(company_id,journal_entry_id,line_no,account_id,"
            "business_partner_id,description,transaction_currency,exchange_rate,"
            "transaction_debit,transaction_credit,debit_amount,credit_amount) "
            "SELECT company_id,%s,line_no,account_id,business_partner_id,'Payment reversal',"
            "transaction_currency,exchange_rate,transaction_credit,transaction_debit,"
            "credit_amount,debit_amount FROM erp.journal_lines "
            "WHERE company_id=%s AND journal_entry_id=%s",
            [entry_id, scope.company_id, original["journal_entry_id"]],
        )
        for allocation in allocations:
            _run(
                "INSERT INTO erp.payment_allocation_reversals(company_id,reversal_payment_id,"
                "ar_allocation_id,ap_allocation_id,document_amount,payment_amount) "
                "VALUES (%s,%s,%s,%s,%s,%s)",
                [
                    scope.company_id,
                    reversal_id,
                    allocation["id"] if original["direction"] == "receipt" else None,
                    allocation["id"] if original["direction"] == "disbursement" else None,
                    allocation["applied_document_amount"],
                    allocation["applied_payment_amount"],
                ],
            )
        _run("SELECT erp.post_journal_entry(%s,%s)", [scope.company_id, entry_id])
        _run(
            "UPDATE erp.payments SET status='posted',journal_entry_id=%s,"
            "posted_at=clock_timestamp() WHERE company_id=%s AND id=%s",
            [entry_id, scope.company_id, reversal_id],
        )
        _effect(scope, reversal_id, "payment.reversed")
        result = payment_detail(scope.company_id, reversal_id)
        _complete(receipt, result)
        return result
