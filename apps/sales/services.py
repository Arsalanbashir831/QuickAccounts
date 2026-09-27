import datetime as dt
import decimal
import hashlib
import json
import uuid
from dataclasses import dataclass
from typing import Any, cast

from django.db import DatabaseError, IntegrityError, connection, transaction

from apps.sales.selectors import sales_invoice_detail
from common.access.scopes import (
    CompanyScope,
    assert_company_write,
    bind_and_verify_company,
    require_permission,
)
from common.api.errors import APIError, Conflict, PreconditionFailed, ScopeNotFound

ROUNDING_METHOD = "half_up"
MAX_LINES = 50


def _execute(sql: str, params: list[Any]) -> tuple[Any, ...] | None:
    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        return cast(tuple[Any, ...] | None, cursor.fetchone())


def _constraint_name(exc: IntegrityError) -> str | None:
    diagnostic = getattr(getattr(exc, "__cause__", None), "diag", None)
    return cast(str | None, getattr(diagnostic, "constraint_name", None))


def _set_context(request_id: str | None, action: str) -> None:
    with connection.cursor() as cursor:
        cursor.execute("SELECT set_config('app.request_id', %s, true)", [request_id or ""])
        cursor.execute("SELECT set_config('app.sales_invoice_action', %s, true)", [action])


def _money_quantizer(minor_units: int) -> decimal.Decimal:
    return decimal.Decimal(1).scaleb(-minor_units)


def _money(value: decimal.Decimal, minor_units: int) -> decimal.Decimal:
    return value.quantize(_money_quantizer(minor_units), rounding=decimal.ROUND_HALF_UP)


def _validate_header(
    company_id: uuid.UUID,
    *,
    partner_id: uuid.UUID,
    currency_code: str,
    tax_jurisdiction_id: uuid.UUID | None,
    issue_date: dt.date,
    due_date: dt.date | None,
) -> int:
    if due_date is not None and due_date < issue_date:
        raise APIError(code="INVALID_DUE_DATE", message="Due date must not precede issue date.")
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT 1 FROM erp.business_partners
            WHERE company_id = %s AND id = %s AND is_active
              AND partner_kind IN ('customer', 'both')
            """,
            [company_id, partner_id],
        )
        if cursor.fetchone() is None:
            raise APIError(
                code="INVALID_INVOICE_CUSTOMER",
                message="The invoice customer is unavailable or is not a customer.",
            )
        cursor.execute(
            "SELECT minor_units FROM erp.currencies WHERE code = %s AND is_active",
            [currency_code],
        )
        currency = cursor.fetchone()
        if currency is None:
            raise APIError(code="INVALID_CURRENCY", message="The invoice currency is unavailable.")
        if tax_jurisdiction_id is not None:
            cursor.execute(
                "SELECT 1 FROM erp.tax_jurisdictions WHERE id = %s AND is_active",
                [tax_jurisdiction_id],
            )
            if cursor.fetchone() is None:
                raise APIError(
                    code="INVALID_TAX_JURISDICTION",
                    message="The tax jurisdiction is unavailable.",
                )
    return int(currency[0])


def _validate_lines(company_id: uuid.UUID, lines: list[dict[str, Any]]) -> None:
    if not 1 <= len(lines) <= MAX_LINES:
        raise APIError(
            code="INVALID_INVOICE_LINE_COUNT",
            message=f"An invoice must contain between 1 and {MAX_LINES} lines.",
        )
    item_ids = [line["item_id"] for line in lines if line.get("item_id")]
    account_ids = [line["line_account_id"] for line in lines if line.get("line_account_id")]
    with connection.cursor() as cursor:
        if item_ids:
            cursor.execute(
                """
                SELECT id FROM erp.items
                WHERE company_id = %s AND id = ANY(%s::uuid[]) AND is_active
                """,
                [company_id, item_ids],
            )
            found = {row[0] for row in cursor.fetchall()}
            if found != set(item_ids):
                raise APIError(
                    code="INVALID_INVOICE_ITEM", message="An invoice item is unavailable."
                )
        if account_ids:
            cursor.execute(
                """
                SELECT id FROM erp.accounts
                WHERE company_id = %s AND id = ANY(%s::uuid[])
                  AND is_active AND allow_posting AND account_type = 'revenue'
                """,
                [company_id, account_ids],
            )
            found = {row[0] for row in cursor.fetchall()}
            if found != set(account_ids):
                raise APIError(
                    code="INVALID_INVOICE_LINE_ACCOUNT",
                    message=(
                        "An invoice line account is unavailable or is not a posting "
                        "revenue account."
                    ),
                )


def _address_rows(
    company_id: uuid.UUID,
    partner_id: uuid.UUID,
    addresses: list[dict[str, Any]],
) -> list[tuple[object, ...]]:
    result: list[tuple[object, ...]] = []
    with connection.cursor() as cursor:
        for address in addresses:
            cursor.execute(
                """
                SELECT line_1, line_2, city, region, postal_code, country_code
                FROM erp.partner_addresses
                WHERE company_id = %s AND partner_id = %s AND id = %s
                  AND address_kind = %s
                """,
                [company_id, partner_id, address["source_address_id"], address["address_kind"]],
            )
            source = cursor.fetchone()
            if source is None:
                raise APIError(
                    code="INVALID_INVOICE_ADDRESS",
                    message="An invoice address is unavailable or does not match its kind.",
                )
            row: tuple[object, ...] = (
                address["address_kind"],
                address["recipient_name"],
                address.get("tax_registration_no") or None,
                *source,
            )
            result.append(row)
    return result


def _base_amount(line: dict[str, Any], minor_units: int) -> decimal.Decimal:
    extended = cast(decimal.Decimal, line["quantity"]) * cast(decimal.Decimal, line["unit_price"])
    value = extended - cast(decimal.Decimal, line["discount_amount"])
    if value < 0:
        raise APIError(
            code="INVALID_LINE_DISCOUNT",
            message="A line discount cannot exceed its extended price.",
        )
    return _money(value, minor_units)


def _insert_lines(
    company_id: uuid.UUID,
    invoice_id: uuid.UUID,
    lines: list[dict[str, Any]],
    minor_units: int,
) -> None:
    _validate_lines(company_id, lines)
    with connection.cursor() as cursor:
        for line_no, line in enumerate(lines, start=1):
            amount = _base_amount(line, minor_units)
            cursor.execute(
                """
                INSERT INTO erp.sales_invoice_lines(
                    company_id, sales_invoice_id, line_no, item_id, line_account_id,
                    description, quantity, unit_price, discount_amount, net_amount,
                    tax_code_id, tax_amount, gross_amount
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,0,%s)
                """,
                [
                    company_id,
                    invoice_id,
                    line_no,
                    line.get("item_id"),
                    line.get("line_account_id"),
                    line["description"],
                    line["quantity"],
                    line["unit_price"],
                    line["discount_amount"],
                    amount,
                    line.get("tax_code_id"),
                    amount,
                ],
            )


def _insert_addresses(
    company_id: uuid.UUID,
    invoice_id: uuid.UUID,
    rows: list[tuple[object, ...]],
) -> None:
    with connection.cursor() as cursor:
        for row in rows:
            params: list[Any] = [company_id, invoice_id, *row]
            cursor.execute(
                """
                INSERT INTO erp.sales_invoice_address_snapshots(
                    company_id, sales_invoice_id, address_kind, recipient_name,
                    tax_registration_no, line_1, line_2, city, region,
                    postal_code, country_code
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                params,
            )


def _write_conflict(exc: IntegrityError) -> Conflict:
    constraint = _constraint_name(exc)
    if constraint == "sales_invoices_company_id_invoice_no_key":
        return Conflict(
            "INVOICE_NUMBER_EXISTS",
            "A sales invoice with this number already exists in the company.",
            {"field": "invoice_no"},
        )
    if constraint == "uq_sales_invoice_line_credited_once":
        return Conflict(
            "INVOICE_LINE_ALREADY_CREDITED",
            "A selected source line already has a linked credit correction.",
        )
    return Conflict(
        "SALES_INVOICE_CONFLICT",
        "The sales invoice conflicts with existing data.",
        {"constraint": constraint},
    )


def create_sales_invoice(
    scope: CompanyScope, data: dict[str, Any], *, request_id: str | None
) -> dict[str, Any]:
    try:
        with transaction.atomic(durable=True):
            bind_and_verify_company(scope)
            assert_company_write(scope.company_id, "sales", "sales.invoice.edit_draft")
            minor_units = _validate_header(
                scope.company_id,
                partner_id=data["partner_id"],
                currency_code=data["currency_code"],
                tax_jurisdiction_id=data.get("tax_jurisdiction_id"),
                issue_date=data["issue_date"],
                due_date=data.get("due_date"),
            )
            _validate_lines(scope.company_id, data["lines"])
            addresses = _address_rows(
                scope.company_id, data["partner_id"], data.get("addresses", [])
            )
            _set_context(request_id, "sales_invoice.created")
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO erp.sales_invoices(
                        company_id, invoice_no, document_kind, partner_id,
                        issue_date, tax_point_date, tax_jurisdiction_id,
                        due_date, currency_code, exchange_rate, status
                    ) VALUES (%s,%s,'invoice',%s,%s,%s,%s,%s,%s,%s,'draft')
                    RETURNING id
                    """,
                    [
                        scope.company_id,
                        data["invoice_no"],
                        data["partner_id"],
                        data["issue_date"],
                        data["tax_point_date"],
                        data.get("tax_jurisdiction_id"),
                        data.get("due_date"),
                        data["currency_code"],
                        data["exchange_rate"],
                    ],
                )
                invoice_id = cast(uuid.UUID, cursor.fetchone()[0])
            _insert_addresses(scope.company_id, invoice_id, addresses)
            _insert_lines(scope.company_id, invoice_id, data["lines"], minor_units)
            result = sales_invoice_detail(scope.company_id, invoice_id)
            assert result is not None
            return result
    except IntegrityError as exc:
        raise _write_conflict(exc) from exc


def _locked_invoice(
    company_id: uuid.UUID, invoice_id: uuid.UUID, expected_revision: int
) -> dict[str, Any]:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT invoice_no, partner_id, issue_date, tax_point_date,
                   tax_jurisdiction_id, due_date, currency_code, exchange_rate,
                   status, row_version, document_kind
            FROM erp.sales_invoices
            WHERE company_id = %s AND id = %s
            FOR UPDATE
            """,
            [company_id, invoice_id],
        )
        row = cursor.fetchone()
    if row is None:
        raise ScopeNotFound()
    if row[8] != "draft":
        raise Conflict("INVOICE_NOT_DRAFT", "Only a draft sales invoice can be changed.")
    if int(row[9]) != expected_revision:
        raise PreconditionFailed(int(row[9]))
    names = (
        "invoice_no",
        "partner_id",
        "issue_date",
        "tax_point_date",
        "tax_jurisdiction_id",
        "due_date",
        "currency_code",
        "exchange_rate",
        "document_kind",
    )
    return dict(zip(names, [*row[:8], row[10]], strict=True))


def update_sales_invoice(
    scope: CompanyScope,
    invoice_id: uuid.UUID,
    data: dict[str, Any],
    *,
    expected_revision: int,
    request_id: str | None,
) -> dict[str, Any]:
    try:
        with transaction.atomic(durable=True):
            bind_and_verify_company(scope)
            assert_company_write(scope.company_id, "sales", "sales.invoice.edit_draft")
            current = _locked_invoice(scope.company_id, invoice_id, expected_revision)
            if current["document_kind"] != "invoice":
                raise Conflict(
                    "CREDIT_NOTE_IMMUTABLE",
                    "Linked credit-note lines cannot be edited after creation.",
                )
            merged = {**current, **{key: value for key, value in data.items() if key in current}}
            minor_units = _validate_header(
                scope.company_id,
                partner_id=merged["partner_id"],
                currency_code=merged["currency_code"],
                tax_jurisdiction_id=merged["tax_jurisdiction_id"],
                issue_date=merged["issue_date"],
                due_date=merged["due_date"],
            )
            addresses = None
            if "addresses" in data:
                addresses = _address_rows(scope.company_id, merged["partner_id"], data["addresses"])
            if "lines" in data:
                _validate_lines(scope.company_id, data["lines"])
            _set_context(request_id, "sales_invoice.updated")
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    DELETE FROM erp.sales_invoice_tax_components
                    WHERE company_id=%s AND sales_invoice_id=%s
                    """,
                    [scope.company_id, invoice_id],
                )
                if "lines" in data:
                    cursor.execute(
                        """
                        DELETE FROM erp.sales_invoice_lines
                        WHERE company_id=%s AND sales_invoice_id=%s
                        """,
                        [scope.company_id, invoice_id],
                    )
                    _insert_lines(scope.company_id, invoice_id, data["lines"], minor_units)
                else:
                    cursor.execute(
                        """
                        UPDATE erp.sales_invoice_lines
                        SET net_amount = round(quantity * unit_price - discount_amount, %s),
                            tax_amount = 0,
                            gross_amount = round(quantity * unit_price - discount_amount, %s)
                        WHERE company_id = %s AND sales_invoice_id = %s
                        """,
                        [minor_units, minor_units, scope.company_id, invoice_id],
                    )
                if addresses is not None:
                    cursor.execute(
                        """
                        DELETE FROM erp.sales_invoice_address_snapshots
                        WHERE company_id=%s AND sales_invoice_id=%s
                        """,
                        [scope.company_id, invoice_id],
                    )
                    _insert_addresses(scope.company_id, invoice_id, addresses)
                cursor.execute(
                    """
                    UPDATE erp.sales_invoices
                    SET invoice_no=%s, partner_id=%s, issue_date=%s,
                        tax_point_date=%s, tax_jurisdiction_id=%s, due_date=%s,
                        currency_code=%s, exchange_rate=%s, calculated_at=NULL
                    WHERE company_id=%s AND id=%s
                    """,
                    [
                        merged["invoice_no"],
                        merged["partner_id"],
                        merged["issue_date"],
                        merged["tax_point_date"],
                        merged["tax_jurisdiction_id"],
                        merged["due_date"],
                        merged["currency_code"],
                        merged["exchange_rate"],
                        scope.company_id,
                        invoice_id,
                    ],
                )
            result = sales_invoice_detail(scope.company_id, invoice_id)
            assert result is not None
            return result
    except IntegrityError as exc:
        raise _write_conflict(exc) from exc


def _tax_rules(
    company_id: uuid.UUID,
    tax_code_id: uuid.UUID,
    tax_point_date: dt.date,
    document_jurisdiction_id: uuid.UUID | None,
) -> tuple[bool, list[dict[str, Any]]]:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT tc.tax_jurisdiction_id, tc.tax_scope, tc.is_tax_inclusive, tc.is_active,
                   cc.id, cc.rate_schedule_id, cc.sequence_no,
                   rv.id, rv.version_code, rv.calculation_method,
                   rv.calculation_base, rv.rate, rv.fixed_amount,
                   rv.recovery_percent, tt.calculation_stage, tt.tax_direction
            FROM erp.tax_codes tc
            JOIN erp.tax_code_components cc
              ON cc.company_id=tc.company_id AND cc.tax_code_id=tc.id
             AND cc.tax_jurisdiction_id=tc.tax_jurisdiction_id
            JOIN erp.tax_rate_schedules rs
              ON rs.jurisdiction_id=cc.tax_jurisdiction_id AND rs.id=cc.rate_schedule_id
             AND rs.is_active
            JOIN erp.tax_types tt
              ON tt.jurisdiction_id=rs.jurisdiction_id AND tt.id=rs.tax_type_id
             AND tt.is_active
            LEFT JOIN LATERAL (
                SELECT v.* FROM erp.tax_rate_versions v
                WHERE v.jurisdiction_id=cc.tax_jurisdiction_id
                  AND v.rate_schedule_id=cc.rate_schedule_id
                  AND v.valid_from <= %s
                  AND (v.valid_to IS NULL OR %s < v.valid_to)
                ORDER BY v.valid_from DESC, v.id DESC LIMIT 1
            ) rv ON true
            WHERE tc.company_id=%s AND tc.id=%s
            ORDER BY cc.sequence_no
            """,
            [tax_point_date, tax_point_date, company_id, tax_code_id],
        )
        rows = cursor.fetchall()
    if not rows:
        raise APIError(code="INVALID_TAX_CODE", message="The tax code has no active components.")
    first = rows[0]
    if not first[3] or first[1] not in {"sales", "both"}:
        raise APIError(code="INVALID_TAX_CODE", message="The tax code is not active for sales.")
    if document_jurisdiction_id is None or first[0] != document_jurisdiction_id:
        raise APIError(
            code="TAX_JURISDICTION_MISMATCH",
            message="The invoice and tax code jurisdictions do not match.",
        )
    rules: list[dict[str, Any]] = []
    for row in rows:
        if row[7] is None:
            raise APIError(
                code="TAX_RATE_NOT_EFFECTIVE",
                message="No tax rate version is effective on the invoice tax point date.",
            )
        if row[14] != "line" or row[15] not in {"output", "bidirectional"}:
            raise APIError(
                code="TAX_RULE_UNSUPPORTED",
                message="This draft calculator supports line-stage output tax rules only.",
            )
        if row[9] == "external_rule":
            raise APIError(
                code="TAX_RULE_UNSUPPORTED",
                message="External tax adapters are not available in draft calculation.",
            )
        if row[10] != "net_amount":
            raise APIError(
                code="TAX_RULE_UNSUPPORTED",
                message="The configured calculation base is not supported.",
            )
        rules.append(
            {
                "jurisdiction_id": row[0],
                "component_id": row[4],
                "schedule_id": row[5],
                "sequence_no": row[6],
                "version_id": row[7],
                "version_code": row[8],
                "method": row[9],
                "base": row[10],
                "rate": row[11],
                "fixed_amount": row[12],
                "recovery_percent": row[13],
            }
        )
    if first[2] and any(rule["method"] != "percentage" for rule in rules):
        raise APIError(
            code="TAX_RULE_UNSUPPORTED",
            message="Tax-inclusive fixed or compound rules require a jurisdiction adapter.",
        )
    return bool(first[2]), rules


def calculate_sales_invoice(
    scope: CompanyScope,
    invoice_id: uuid.UUID,
    *,
    expected_revision: int,
    request_id: str | None,
) -> dict[str, Any]:
    with transaction.atomic(durable=True):
        bind_and_verify_company(scope)
        assert_company_write(scope.company_id, "sales", "sales.invoice.edit_draft")
        header = _locked_invoice(scope.company_id, invoice_id, expected_revision)
        if header["document_kind"] != "invoice":
            raise Conflict(
                "CREDIT_NOTE_ALREADY_CALCULATED",
                "Linked credit notes preserve the source invoice tax snapshots.",
            )
        minor_units = _validate_header(
            scope.company_id,
            partner_id=header["partner_id"],
            currency_code=header["currency_code"],
            tax_jurisdiction_id=header["tax_jurisdiction_id"],
            issue_date=header["issue_date"],
            due_date=header["due_date"],
        )
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, quantity, unit_price, discount_amount, tax_code_id
                FROM erp.sales_invoice_lines
                WHERE company_id=%s AND sales_invoice_id=%s ORDER BY line_no
                """,
                [scope.company_id, invoice_id],
            )
            lines = cursor.fetchall()
        if not lines:
            raise APIError(code="INVOICE_LINES_REQUIRED", message="The invoice has no lines.")
        calculated: list[
            tuple[
                uuid.UUID, decimal.Decimal, decimal.Decimal, decimal.Decimal, list[dict[str, Any]]
            ]
        ] = []
        for line_id, quantity, unit_price, discount_amount, tax_code_id in lines:
            entered = _money(quantity * unit_price - discount_amount, minor_units)
            if entered < 0:
                raise APIError(
                    code="INVALID_LINE_DISCOUNT", message="A line discount exceeds its price."
                )
            if tax_code_id is None:
                calculated.append((line_id, entered, decimal.Decimal(0), entered, []))
                continue
            inclusive, rules = _tax_rules(
                scope.company_id,
                tax_code_id,
                header["tax_point_date"],
                header["tax_jurisdiction_id"],
            )
            components: list[dict[str, Any]] = []
            prior_tax = decimal.Decimal(0)
            if inclusive:
                total_rate = sum((rule["rate"] for rule in rules), decimal.Decimal(0))
                net = _money(entered / (decimal.Decimal(1) + total_rate / 100), minor_units)
            else:
                net = entered
            for index, rule in enumerate(rules):
                if rule["method"] == "fixed":
                    taxable_base = net
                    amount = _money(rule["fixed_amount"], minor_units)
                elif rule["method"] == "compound":
                    taxable_base = net + prior_tax
                    amount = _money(taxable_base * rule["rate"] / 100, minor_units)
                else:
                    taxable_base = net
                    amount = _money(taxable_base * rule["rate"] / 100, minor_units)
                if inclusive and index == len(rules) - 1:
                    amount = entered - net - prior_tax
                prior_tax += amount
                components.append({**rule, "taxable_base": taxable_base, "tax_amount": amount})
            calculated.append((line_id, net, prior_tax, net + prior_tax, components))
        _set_context(request_id, "sales_invoice.calculated")
        with connection.cursor() as cursor:
            cursor.execute(
                """
                DELETE FROM erp.sales_invoice_tax_components
                WHERE company_id=%s AND sales_invoice_id=%s
                """,
                [scope.company_id, invoice_id],
            )
            for line_id, net, tax, gross, components in calculated:
                cursor.execute(
                    """
                    UPDATE erp.sales_invoice_lines SET net_amount=%s, tax_amount=%s, gross_amount=%s
                    WHERE company_id=%s AND sales_invoice_id=%s AND id=%s
                    """,
                    [net, tax, gross, scope.company_id, invoice_id, line_id],
                )
                for component in components:
                    cursor.execute(
                        """
                        INSERT INTO erp.sales_invoice_tax_components(
                            company_id, sales_invoice_id, sales_invoice_line_id,
                            tax_code_id, tax_code_component_id, tax_jurisdiction_id,
                            rate_schedule_id, tax_rate_version_id, taxable_base_amount,
                            rate_snapshot, tax_inclusive_snapshot, tax_amount,
                            rate_version_code_snapshot, calculation_method_snapshot,
                            calculation_base_snapshot, fixed_amount_snapshot,
                            recovery_percent_snapshot, rounding_method_snapshot,
                            rounding_precision_snapshot, currency_code_snapshot
                        ) SELECT %s,%s,%s,l.tax_code_id,%s,%s,%s,%s,%s,%s,tc.is_tax_inclusive,%s,
                                 %s,%s,%s,%s,%s,%s,%s,%s
                          FROM erp.sales_invoice_lines l
                          JOIN erp.tax_codes tc
                            ON tc.company_id=l.company_id AND tc.id=l.tax_code_id
                         WHERE l.company_id=%s AND l.sales_invoice_id=%s AND l.id=%s
                        """,
                        [
                            scope.company_id,
                            invoice_id,
                            line_id,
                            component["component_id"],
                            component["jurisdiction_id"],
                            component["schedule_id"],
                            component["version_id"],
                            component["taxable_base"],
                            component["rate"],
                            component["tax_amount"],
                            component["version_code"],
                            component["method"],
                            component["base"],
                            component["fixed_amount"],
                            component["recovery_percent"],
                            ROUNDING_METHOD,
                            minor_units,
                            header["currency_code"],
                            scope.company_id,
                            invoice_id,
                            line_id,
                        ],
                    )
            cursor.execute(
                """
                UPDATE erp.sales_invoices SET calculated_at=clock_timestamp()
                WHERE company_id=%s AND id=%s
                """,
                [scope.company_id, invoice_id],
            )
        result = sales_invoice_detail(scope.company_id, invoice_id)
        assert result is not None
        return result


def _canonical_hash(payload: dict[str, Any]) -> bytes:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).digest()


@dataclass(frozen=True, slots=True)
class _Receipt:
    receipt_id: uuid.UUID
    replay_body: dict[str, Any] | None = None
    replay_status: int | None = None


def _claim_receipt(
    scope: CompanyScope,
    *,
    operation: str,
    resource_key: str,
    idempotency_key: str,
    payload: dict[str, Any],
) -> _Receipt:
    request_hash = _canonical_hash(payload)
    row = _execute(
        """
        INSERT INTO erp.api_command_receipts(
            tenant_id,company_id,actor_user_id,operation,resource_key,
            idempotency_key,request_hash,retain_until
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,clock_timestamp()+interval '30 days')
        ON CONFLICT DO NOTHING RETURNING id
        """,
        [
            scope.tenant_id,
            scope.company_id,
            scope.user_id,
            operation,
            resource_key,
            idempotency_key,
            request_hash,
        ],
    )
    if row is not None:
        return _Receipt(cast(uuid.UUID, row[0]))
    existing = _execute(
        """
        SELECT id,request_hash,status,response_status,response_body
        FROM erp.api_command_receipts
        WHERE tenant_id=%s AND company_id=%s AND actor_user_id=%s
          AND actor_service_id IS NULL AND operation=%s AND resource_key=%s
          AND idempotency_key=%s FOR UPDATE
        """,
        [
            scope.tenant_id,
            scope.company_id,
            scope.user_id,
            operation,
            resource_key,
            idempotency_key,
        ],
    )
    if existing is None:
        raise Conflict("IDEMPOTENCY_CONFLICT", "The command receipt could not be resolved.")
    if bytes(existing[1]) != request_hash:
        raise Conflict(
            "IDEMPOTENCY_PAYLOAD_MISMATCH",
            "This idempotency key was already used with a different payload.",
        )
    if existing[2] != "completed":
        raise Conflict("COMMAND_IN_PROGRESS", "A command with this key is still processing.")
    body = existing[4]
    if isinstance(body, str):
        body = json.loads(body)
    return _Receipt(
        cast(uuid.UUID, existing[0]),
        cast(dict[str, Any], body),
        int(existing[3]),
    )


def _complete_receipt(receipt_id: uuid.UUID, body: dict[str, Any]) -> None:
    _execute(
        """
        UPDATE erp.api_command_receipts
        SET status='completed',response_status=200,response_body=%s::jsonb,
            completed_at=clock_timestamp(),result_type='sales_invoice',result_id=%s
        WHERE id=%s
        RETURNING id
        """,
        [json.dumps(body), body["id"], receipt_id],
    )


def _posting_conflict(exc: DatabaseError) -> Conflict:
    cause = getattr(exc, "__cause__", None)
    constraint = getattr(getattr(cause, "diag", None), "constraint_name", None)
    if constraint == "uq_journal_entry_source_document":
        return Conflict("INVOICE_ALREADY_POSTED", "The invoice already has a posting journal.")
    if constraint == "inventory_positions_on_hand_quantity_check":
        return Conflict("INSUFFICIENT_STOCK", "The invoice would make available stock negative.")
    return Conflict(
        "INVOICE_POSTING_CONFLICT",
        "The invoice could not be posted because a financial invariant was not satisfied.",
        {"constraint": constraint},
    )


def _validate_posting_tax(
    company_id: uuid.UUID,
    invoice_id: uuid.UUID,
    partner_id: uuid.UUID,
    tax_point_date: dt.date,
    document_kind: str,
) -> None:
    if document_kind == "credit_note":
        invalid_credit = _execute(
            """
            SELECT count(*)
            FROM erp.sales_invoice_tax_components c
            JOIN erp.sales_invoice_lines credit_line
              ON credit_line.company_id=c.company_id
             AND credit_line.sales_invoice_id=c.sales_invoice_id
             AND credit_line.id=c.sales_invoice_line_id
            LEFT JOIN erp.sales_invoice_tax_components original
              ON original.company_id=credit_line.company_id
             AND original.sales_invoice_line_id=credit_line.credit_of_invoice_line_id
             AND original.tax_code_component_id=c.tax_code_component_id
            WHERE c.company_id=%s AND c.sales_invoice_id=%s AND (
                original.id IS NULL
                OR c.tax_rate_version_id<>original.tax_rate_version_id
                OR c.taxable_base_amount<>original.taxable_base_amount
                OR c.rate_snapshot<>original.rate_snapshot
                OR c.tax_amount<>original.tax_amount
                OR c.output_tax_account_id_snapshot IS DISTINCT FROM
                   original.output_tax_account_id_snapshot
                OR c.polarity_snapshot<>'debit'
            )
            """,
            [company_id, invoice_id],
        )
        if invalid_credit is not None and int(invalid_credit[0]) > 0:
            raise Conflict(
                "CREDIT_TAX_SNAPSHOT_MISMATCH",
                "Credit tax components must exactly reverse their source-line snapshots.",
            )
        return
    invalid = _execute(
        """
        SELECT count(*)
        FROM erp.sales_invoice_tax_components c
        JOIN erp.tax_rate_versions v
          ON v.jurisdiction_id=c.tax_jurisdiction_id
         AND v.rate_schedule_id=c.rate_schedule_id AND v.id=c.tax_rate_version_id
        JOIN erp.tax_code_components cc
          ON cc.company_id=c.company_id AND cc.tax_code_id=c.tax_code_id
         AND cc.id=c.tax_code_component_id
        WHERE c.company_id=%s AND c.sales_invoice_id=%s AND (
            c.rate_version_code_snapshot<>v.version_code
            OR c.calculation_method_snapshot<>v.calculation_method
            OR c.calculation_base_snapshot<>v.calculation_base
            OR c.rate_snapshot<>v.rate
            OR c.recovery_percent_snapshot<>v.recovery_percent
            OR cc.output_tax_account_id IS NULL
        )
        """,
        [company_id, invoice_id],
    )
    if invalid is not None and int(invalid[0]) > 0:
        raise Conflict(
            "TAX_SNAPSHOT_STALE",
            "Tax configuration changed or lacks an output-tax account; recalculate the draft.",
        )
    registrations = _execute(
        """
        SELECT count(*)
        FROM (
            SELECT DISTINCT rs.jurisdiction_id, rs.tax_type_id
            FROM erp.sales_invoice_tax_components c
            JOIN erp.tax_rate_schedules rs
              ON rs.jurisdiction_id=c.tax_jurisdiction_id AND rs.id=c.rate_schedule_id
            WHERE c.company_id=%s AND c.sales_invoice_id=%s
        ) required
        WHERE NOT EXISTS (
            SELECT 1 FROM erp.company_tax_registrations r
            WHERE r.company_id=%s AND r.jurisdiction_id=required.jurisdiction_id
              AND r.tax_type_id=required.tax_type_id AND r.is_active
              AND r.valid_from<=%s AND (r.valid_to IS NULL OR %s<r.valid_to)
        ) OR NOT EXISTS (
            SELECT 1 FROM erp.partner_tax_registrations r
            WHERE r.company_id=%s AND r.partner_id=%s
              AND r.jurisdiction_id=required.jurisdiction_id
              AND r.tax_type_id=required.tax_type_id AND r.is_active AND r.is_verified
              AND r.valid_from<=%s AND (r.valid_to IS NULL OR %s<r.valid_to)
        )
        """,
        [
            company_id,
            invoice_id,
            company_id,
            tax_point_date,
            tax_point_date,
            company_id,
            partner_id,
            tax_point_date,
            tax_point_date,
        ],
    )
    if registrations is not None and int(registrations[0]) > 0:
        raise Conflict(
            "TAX_REGISTRATION_REQUIRED",
            "Effective verified company and customer tax registrations are required.",
        )
    _execute(
        """
        UPDATE erp.sales_invoice_tax_components c
        SET output_tax_account_id_snapshot=cc.output_tax_account_id,
            account_role_snapshot='output_tax',
            polarity_snapshot=%s
        FROM erp.tax_code_components cc
        WHERE c.company_id=%s AND c.sales_invoice_id=%s
          AND cc.company_id=c.company_id AND cc.tax_code_id=c.tax_code_id
          AND cc.id=c.tax_code_component_id
        RETURNING c.id
        """,
        ["debit" if document_kind == "credit_note" else "credit", company_id, invoice_id],
    )


def _posting_accounts(
    company_id: uuid.UUID,
    invoice_id: uuid.UUID,
    *,
    require_stock: bool,
    document_kind: str,
) -> tuple[uuid.UUID, dict[uuid.UUID, uuid.UUID], dict[uuid.UUID, tuple[uuid.UUID, uuid.UUID]]]:
    receivable = _execute(
        """
        SELECT r.account_id
        FROM erp.accounting_posting_rules r
        JOIN erp.accounts a ON a.company_id=r.company_id AND a.id=r.account_id
        WHERE r.company_id=%s AND r.event_code='sales_invoice'
          AND r.role_code='accounts_receivable'
          AND a.is_active AND a.allow_posting AND a.account_type='receivable'
        """,
        [company_id],
    )
    if receivable is None:
        raise Conflict(
            "SALES_POSTING_RULE_MISSING",
            "An active accounts-receivable posting rule is required.",
        )
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT l.id, l.item_id, l.line_account_id, i.item_kind,
                   p.revenue_account_id, p.inventory_account_id, p.cogs_account_id,
                   l.revenue_account_id_snapshot,l.inventory_account_id_snapshot,
                   l.cogs_account_id_snapshot
            FROM erp.sales_invoice_lines l
            LEFT JOIN erp.items i ON i.company_id=l.company_id AND i.id=l.item_id
            LEFT JOIN erp.item_accounting_profiles p
              ON p.company_id=l.company_id AND p.item_id=l.item_id
            WHERE l.company_id=%s AND l.sales_invoice_id=%s
            ORDER BY l.id
            """,
            [company_id, invoice_id],
        )
        rows = cursor.fetchall()
    revenue: dict[uuid.UUID, uuid.UUID] = {}
    stock: dict[uuid.UUID, tuple[uuid.UUID, uuid.UUID]] = {}
    with connection.cursor() as cursor:
        for row in rows:
            (
                line_id,
                item_id,
                line_account_id,
                item_kind,
                revenue_id,
                inventory_id,
                cogs_id,
                revenue_snapshot,
                inventory_snapshot,
                cogs_snapshot,
            ) = row
            if document_kind == "credit_note":
                account_id = revenue_snapshot
                inventory_id = inventory_snapshot
                cogs_id = cogs_snapshot
            else:
                account_id = line_account_id or revenue_id
            if account_id is None:
                raise Conflict(
                    "ITEM_REVENUE_ACCOUNT_MISSING",
                    "Every item invoice line requires an active revenue-account mapping.",
                )
            allowed = _execute(
                """
                SELECT 1 FROM erp.accounts
                WHERE company_id=%s AND id=%s AND is_active AND allow_posting
                  AND account_type='revenue'
                """,
                [company_id, account_id],
            )
            if allowed is None:
                raise Conflict(
                    "ITEM_REVENUE_ACCOUNT_INVALID",
                    "An invoice revenue account is inactive or incompatible.",
                )
            revenue[line_id] = account_id
            if item_kind == "stock":
                if inventory_id is None or cogs_id is None:
                    raise Conflict(
                        "ITEM_STOCK_ACCOUNTS_MISSING",
                        "Stock lines require inventory and cost-of-sales account mappings.",
                    )
                stock_accounts_valid = _execute(
                    """
                    SELECT count(*) FROM erp.accounts
                    WHERE company_id=%s AND is_active AND allow_posting AND (
                        (id=%s AND account_type='asset')
                        OR (id=%s AND account_type IN ('cost_of_sales','expense'))
                    )
                    """,
                    [company_id, inventory_id, cogs_id],
                )
                if stock_accounts_valid is None or int(stock_accounts_valid[0]) != 2:
                    raise Conflict(
                        "ITEM_STOCK_ACCOUNTS_INVALID",
                        "Inventory or cost-of-sales account mapping is inactive or incompatible.",
                    )
                stock[item_id] = (inventory_id, cogs_id)
            if document_kind == "invoice":
                cursor.execute(
                    """
                    UPDATE erp.sales_invoice_lines
                    SET revenue_account_id_snapshot=%s,
                        inventory_account_id_snapshot=%s,cogs_account_id_snapshot=%s
                    WHERE company_id=%s AND sales_invoice_id=%s AND id=%s
                    """,
                    [account_id, inventory_id, cogs_id, company_id, invoice_id, line_id],
                )
    if require_stock and not stock:
        raise APIError(
            code="WAREHOUSE_NOT_APPLICABLE",
            message="warehouse_id is only accepted when the invoice consumes stock.",
        )
    return cast(uuid.UUID, receivable[0]), revenue, stock


def _journal_line(
    *,
    account_id: uuid.UUID,
    amount: decimal.Decimal,
    currency: str,
    exchange_rate: decimal.Decimal,
    is_debit: bool,
    partner_id: uuid.UUID | None,
    description: str,
    functional_amount: decimal.Decimal,
) -> dict[str, Any]:
    zero = decimal.Decimal(0)
    return {
        "account_id": account_id,
        "business_partner_id": partner_id,
        "description": description,
        "transaction_currency": currency,
        "exchange_rate": exchange_rate,
        "transaction_debit": amount if is_debit else zero,
        "transaction_credit": zero if is_debit else amount,
        "debit_amount": functional_amount if is_debit else zero,
        "credit_amount": zero if is_debit else functional_amount,
    }


def _insert_posting_journal_lines(
    company_id: uuid.UUID,
    entry_id: uuid.UUID,
    lines: list[dict[str, Any]],
) -> None:
    with connection.cursor() as cursor:
        for line_no, line in enumerate(lines, start=1):
            cursor.execute(
                """
                INSERT INTO erp.journal_lines(
                    company_id,journal_entry_id,line_no,account_id,business_partner_id,
                    description,transaction_currency,exchange_rate,
                    transaction_debit,transaction_credit,debit_amount,credit_amount
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                [
                    company_id,
                    entry_id,
                    line_no,
                    line["account_id"],
                    line["business_partner_id"],
                    line["description"],
                    line["transaction_currency"],
                    line["exchange_rate"],
                    line["transaction_debit"],
                    line["transaction_credit"],
                    line["debit_amount"],
                    line["credit_amount"],
                ],
            )


def _stock_effects(
    scope: CompanyScope,
    invoice_id: uuid.UUID,
    warehouse_id: uuid.UUID,
    journal_entry_id: uuid.UUID,
    stock_accounts: dict[uuid.UUID, tuple[uuid.UUID, uuid.UUID]],
    functional_currency: str,
) -> list[dict[str, Any]]:
    warehouse = _execute(
        "SELECT 1 FROM erp.warehouses WHERE company_id=%s AND id=%s "
        "AND is_active AND stock_category='sellable' FOR SHARE",
        [scope.company_id, warehouse_id],
    )
    if warehouse is None:
        raise APIError(code="INVALID_WAREHOUSE", message="The stock warehouse is unavailable.")
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT l.id,l.item_id,l.quantity,i.track_lots,i.track_serials
            FROM erp.sales_invoice_lines l
            JOIN erp.items i ON i.company_id=l.company_id AND i.id=l.item_id
            WHERE l.company_id=%s AND l.sales_invoice_id=%s AND i.item_kind='stock'
            ORDER BY l.item_id,l.id
            """,
            [scope.company_id, invoice_id],
        )
        stock_lines = cursor.fetchall()
    if any(row[3] or row[4] for row in stock_lines):
        raise Conflict(
            "TRACKED_STOCK_FULFILLMENT_REQUIRED",
            "Lot- or serial-tracked items require a typed shipment workflow.",
        )
    quantities: dict[uuid.UUID, decimal.Decimal] = {}
    for _, item_id, quantity, _, _ in stock_lines:
        quantities[item_id] = quantities.get(item_id, decimal.Decimal(0)) + quantity
    unit_costs: dict[uuid.UUID, decimal.Decimal] = {}
    for item_id in sorted(quantities, key=str):
        position = _execute(
            """
            SELECT on_hand_quantity,reserved_quantity,value_company
            FROM erp.inventory_positions
            WHERE company_id=%s AND warehouse_id=%s AND item_id=%s AND lot_id IS NULL
            FOR UPDATE
            """,
            [scope.company_id, warehouse_id, item_id],
        )
        if position is None or position[0] - position[1] < quantities[item_id]:
            raise Conflict("INSUFFICIENT_STOCK", "Available stock is insufficient for the invoice.")
        unit_costs[item_id] = (position[2] / position[0]).quantize(
            decimal.Decimal("0.000001"), rounding=decimal.ROUND_HALF_UP
        )
    cost_by_accounts: dict[tuple[uuid.UUID, uuid.UUID], decimal.Decimal] = {}
    with connection.cursor() as cursor:
        for line_id, item_id, quantity, _, _ in stock_lines:
            unit_cost = unit_costs[item_id]
            value = (quantity * unit_cost).quantize(
                decimal.Decimal("0.000001"), rounding=decimal.ROUND_HALF_UP
            )
            inventory_id, cogs_id = stock_accounts[item_id]
            key = (inventory_id, cogs_id)
            cost_by_accounts[key] = cost_by_accounts.get(key, decimal.Decimal(0)) + value
            cursor.execute(
                """
                INSERT INTO erp.stock_movements(
                    company_id,event_key,occurred_at,warehouse_id,item_id,movement_kind,
                    quantity_delta,unit_cost_company,value_delta_company,source_type,
                    source_id,source_line_id,journal_entry_id
                ) VALUES (%s,%s,clock_timestamp(),%s,%s,'issue',%s,%s,%s,
                          'sales_invoice',%s,%s,%s)
                """,
                [
                    scope.company_id,
                    f"sales_invoice:{invoice_id}:line:{line_id}",
                    warehouse_id,
                    item_id,
                    -quantity,
                    unit_cost,
                    -value,
                    invoice_id,
                    line_id,
                    journal_entry_id,
                ],
            )
    result: list[dict[str, Any]] = []
    for (inventory_id, cogs_id), amount in cost_by_accounts.items():
        result.append(
            _journal_line(
                account_id=cogs_id,
                amount=amount,
                currency=functional_currency,
                exchange_rate=decimal.Decimal(1),
                is_debit=True,
                partner_id=None,
                description="Cost of goods sold",
                functional_amount=amount,
            )
        )
        result.append(
            _journal_line(
                account_id=inventory_id,
                amount=amount,
                currency=functional_currency,
                exchange_rate=decimal.Decimal(1),
                is_debit=False,
                partner_id=None,
                description="Inventory issued",
                functional_amount=amount,
            )
        )
    return result


def post_sales_invoice(
    scope: CompanyScope,
    invoice_id: uuid.UUID,
    command: dict[str, Any],
    *,
    expected_revision: int,
    idempotency_key: str,
    request_id: str | None,
    _nested: bool = False,
) -> tuple[dict[str, Any], int]:
    payload = {
        "invoice_id": str(invoice_id),
        "expected_revision": expected_revision,
        **command,
    }
    try:
        with transaction.atomic(durable=not _nested):
            bind_and_verify_company(scope)
            receipt = _claim_receipt(
                scope,
                operation="sales.invoice.post",
                resource_key=str(invoice_id),
                idempotency_key=idempotency_key,
                payload=payload,
            )
            if receipt.replay_body is not None:
                require_permission(scope.company_id, "sales.invoice.view")
                return receipt.replay_body, receipt.replay_status or 200
            assert_company_write(scope.company_id, "sales", "sales.invoice.post")
            warehouse_id = command.get("warehouse_id")
            inventory_preflight = _execute(
                """
                SELECT i.document_kind, EXISTS (
                    SELECT 1
                    FROM erp.sales_invoice_lines l
                    JOIN erp.items item
                      ON item.company_id=l.company_id AND item.id=l.item_id
                    WHERE l.company_id=i.company_id AND l.sales_invoice_id=i.id
                      AND item.item_kind='stock'
                )
                FROM erp.sales_invoices i
                WHERE i.company_id=%s AND i.id=%s
                """,
                [scope.company_id, invoice_id],
            )
            if inventory_preflight is None:
                raise ScopeNotFound()
            if inventory_preflight[0] == "credit_note" and warehouse_id is not None:
                raise Conflict(
                    "CREDIT_RETURN_WORKFLOW_REQUIRED",
                    "Physical returns must use the typed inventory return workflow.",
                )
            if inventory_preflight[0] == "invoice" and inventory_preflight[1]:
                if warehouse_id is None:
                    raise Conflict(
                        "STOCK_FULFILLMENT_REQUIRED",
                        "A warehouse is required when posting an invoice that consumes stock.",
                    )
                assert_company_write(scope.company_id, "inventory", "inventory.post")
            elif inventory_preflight[0] == "invoice" and warehouse_id is not None:
                # Authorize the supplied inventory intent before acquiring fiscal/source locks.
                assert_company_write(scope.company_id, "inventory", "inventory.post")
            period = _execute(
                """
                SELECT state,starts_on,ends_on
                FROM erp.fiscal_periods
                WHERE company_id=%s AND id=%s FOR SHARE
                """,
                [scope.company_id, command["fiscal_period_id"]],
            )
            if period is None or period[0] != "open":
                raise Conflict("PERIOD_NOT_OPEN", "The invoice fiscal period is not open.")
            invoice = _execute(
                """
                SELECT invoice_no,document_kind,partner_id,issue_date,tax_point_date,
                       currency_code,exchange_rate,status,row_version,calculated_at
                FROM erp.sales_invoices
                WHERE company_id=%s AND id=%s FOR UPDATE
                """,
                [scope.company_id, invoice_id],
            )
            if invoice is None:
                raise ScopeNotFound()
            if invoice[7] != "draft":
                raise Conflict("INVOICE_NOT_DRAFT", "Only a draft invoice can be posted.")
            if int(invoice[8]) != expected_revision:
                raise PreconditionFailed(int(invoice[8]))
            if not (period[1] <= invoice[3] <= period[2]):
                raise Conflict("DATE_OUTSIDE_PERIOD", "Invoice date is outside the fiscal period.")
            if invoice[9] is None:
                raise Conflict(
                    "INVOICE_CALCULATION_REQUIRED",
                    "Calculate the current draft revision before posting.",
                )
            journal = _execute(
                """
                SELECT 1 FROM erp.journals
                WHERE company_id=%s AND id=%s AND is_active AND journal_type='sales'
                FOR SHARE
                """,
                [scope.company_id, command["journal_id"]],
            )
            if journal is None:
                raise APIError(
                    code="INVALID_SALES_JOURNAL",
                    message="The selected active sales journal is unavailable.",
                )
            missing_tax = _execute(
                """
                SELECT count(*) FROM erp.sales_invoice_lines l
                WHERE l.company_id=%s AND l.sales_invoice_id=%s AND l.tax_code_id IS NOT NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM erp.sales_invoice_tax_components c
                      WHERE c.company_id=l.company_id
                        AND c.sales_invoice_id=l.sales_invoice_id
                        AND c.sales_invoice_line_id=l.id
                  )
                """,
                [scope.company_id, invoice_id],
            )
            if missing_tax is not None and int(missing_tax[0]) > 0:
                raise Conflict(
                    "INVOICE_CALCULATION_REQUIRED",
                    "Every taxable line requires calculated tax components.",
                )
            _validate_posting_tax(
                scope.company_id,
                invoice_id,
                invoice[2],
                invoice[4],
                invoice[1],
            )
            receivable_id, revenue_accounts, stock_accounts = _posting_accounts(
                scope.company_id,
                invoice_id,
                require_stock=warehouse_id is not None,
                document_kind=invoice[1],
            )
            if stock_accounts and invoice[1] == "invoice" and warehouse_id is None:
                raise Conflict(
                    "STOCK_FULFILLMENT_REQUIRED",
                    "A warehouse is required when posting an invoice that consumes stock.",
                )
            if invoice[1] == "credit_note" and warehouse_id is not None:
                raise Conflict(
                    "CREDIT_RETURN_WORKFLOW_REQUIRED",
                    "Physical returns must use the typed inventory return workflow.",
                )
            company_currency = _execute(
                """
                SELECT c.functional_currency,fc.minor_units
                FROM erp.companies c JOIN erp.currencies fc ON fc.code=c.functional_currency
                WHERE c.id=%s
                """,
                [scope.company_id],
            )
            assert company_currency is not None
            functional_currency = str(company_currency[0])
            functional_minor_units = int(company_currency[1])
            entry_number = _execute(
                "SELECT erp.allocate_document_number(%s,%s,'SALES_JOURNAL')",
                [scope.company_id, command["fiscal_period_id"]],
            )
            assert entry_number is not None
            final_invoice_no = invoice[0]
            if str(final_invoice_no).startswith("DRAFT-"):
                number = _execute(
                    "SELECT erp.allocate_document_number(%s,%s,'SALES_INVOICE')",
                    [scope.company_id, command["fiscal_period_id"]],
                )
                assert number is not None
                final_invoice_no = number[0]
            entry_id = uuid.uuid4()
            _execute(
                """
                INSERT INTO erp.journal_entries(
                    id,company_id,journal_id,fiscal_period_id,entry_number,entry_date,
                    description,source_type,source_id,idempotency_key,created_by
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,'sales_invoice',%s,%s,%s)
                RETURNING id
                """,
                [
                    entry_id,
                    scope.company_id,
                    command["journal_id"],
                    command["fiscal_period_id"],
                    entry_number[0],
                    invoice[3],
                    f"Sales document {final_invoice_no}",
                    invoice_id,
                    idempotency_key,
                    scope.user_id,
                ],
            )
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT id,net_amount,tax_amount,gross_amount
                    FROM erp.sales_invoice_lines
                    WHERE company_id=%s AND sales_invoice_id=%s ORDER BY line_no
                    """,
                    [scope.company_id, invoice_id],
                )
                document_lines = cursor.fetchall()
                cursor.execute(
                    """
                    SELECT output_tax_account_id_snapshot,sum(tax_amount)
                    FROM erp.sales_invoice_tax_components
                    WHERE company_id=%s AND sales_invoice_id=%s
                    GROUP BY output_tax_account_id_snapshot
                    """,
                    [scope.company_id, invoice_id],
                )
                tax_totals = cursor.fetchall()
            revenue_totals: dict[uuid.UUID, decimal.Decimal] = {}
            gross_total = decimal.Decimal(0)
            for line_id, net, _, gross in document_lines:
                account_id = revenue_accounts[line_id]
                revenue_totals[account_id] = (
                    revenue_totals.get(account_id, decimal.Decimal(0)) + net
                )
                gross_total += gross
            is_credit = invoice[1] == "credit_note"
            transaction_currency = str(invoice[5])
            exchange_rate = cast(decimal.Decimal, invoice[6])
            journal_lines: list[dict[str, Any]] = []
            counter_functional_total = decimal.Decimal(0)
            for account_id, amount in revenue_totals.items():
                functional = _money(amount * exchange_rate, functional_minor_units)
                counter_functional_total += functional
                journal_lines.append(
                    _journal_line(
                        account_id=account_id,
                        amount=amount,
                        currency=transaction_currency,
                        exchange_rate=exchange_rate,
                        is_debit=is_credit,
                        partner_id=invoice[2],
                        description="Sales revenue reversal" if is_credit else "Sales revenue",
                        functional_amount=functional,
                    )
                )
            for tax_account_id, amount in tax_totals:
                if tax_account_id is None:
                    raise Conflict(
                        "OUTPUT_TAX_ACCOUNT_MISSING",
                        "Every calculated tax component requires an output-tax account.",
                    )
                functional = _money(amount * exchange_rate, functional_minor_units)
                counter_functional_total += functional
                journal_lines.append(
                    _journal_line(
                        account_id=tax_account_id,
                        amount=amount,
                        currency=transaction_currency,
                        exchange_rate=exchange_rate,
                        is_debit=is_credit,
                        partner_id=invoice[2],
                        description="Output tax reversal" if is_credit else "Output tax",
                        functional_amount=functional,
                    )
                )
            journal_lines.insert(
                0,
                _journal_line(
                    account_id=receivable_id,
                    amount=gross_total,
                    currency=transaction_currency,
                    exchange_rate=exchange_rate,
                    is_debit=not is_credit,
                    partner_id=invoice[2],
                    description="Accounts receivable",
                    functional_amount=counter_functional_total,
                ),
            )
            if stock_accounts and not is_credit:
                assert warehouse_id is not None
                journal_lines.extend(
                    _stock_effects(
                        scope,
                        invoice_id,
                        warehouse_id,
                        entry_id,
                        stock_accounts,
                        functional_currency,
                    )
                )
            _insert_posting_journal_lines(scope.company_id, entry_id, journal_lines)
            _execute("SELECT erp.post_journal_entry(%s,%s)", [scope.company_id, entry_id])
            _set_context(request_id, "sales_invoice.posted")
            _execute(
                """
                UPDATE erp.sales_invoices
                SET invoice_no=%s,journal_entry_id=%s,status='posted',posted_at=clock_timestamp()
                WHERE company_id=%s AND id=%s
                RETURNING id
                """,
                [final_invoice_no, entry_id, scope.company_id, invoice_id],
            )
            body = sales_invoice_detail(scope.company_id, invoice_id)
            assert body is not None
            _execute(
                """
                INSERT INTO erp.outbox_events(
                    company_id,event_key,aggregate_type,aggregate_id,event_type,payload
                ) VALUES (%s,%s,'sales_invoice',%s,'sales.invoice.posted',%s::jsonb)
                RETURNING id
                """,
                [
                    scope.company_id,
                    f"sales.invoice.posted:{invoice_id}:{receipt.receipt_id}",
                    invoice_id,
                    json.dumps(
                        {
                            "invoice_id": str(invoice_id),
                            "invoice_no": final_invoice_no,
                            "journal_entry_id": str(entry_id),
                        }
                    ),
                ],
            )
            _complete_receipt(receipt.receipt_id, body)
            return body, 200
    except APIError:
        raise
    except DatabaseError as exc:
        raise _posting_conflict(exc) from exc


def create_linked_credit_note(
    scope: CompanyScope,
    source_invoice_id: uuid.UUID,
    data: dict[str, Any],
    *,
    request_id: str | None,
) -> dict[str, Any]:
    try:
        with transaction.atomic(durable=True):
            bind_and_verify_company(scope)
            assert_company_write(scope.company_id, "sales", "sales.invoice.edit_draft")
            source = _execute(
                """
                SELECT partner_id,tax_point_date,tax_jurisdiction_id,currency_code,
                       exchange_rate,status,document_kind
                FROM erp.sales_invoices
                WHERE company_id=%s AND id=%s FOR UPDATE
                """,
                [scope.company_id, source_invoice_id],
            )
            if source is None:
                raise ScopeNotFound()
            if source[5] != "posted" or source[6] != "invoice":
                raise Conflict(
                    "CREDIT_SOURCE_NOT_POSTED_INVOICE",
                    "Credit corrections require a posted original invoice.",
                )
            source_line_ids = data["source_line_ids"]
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT id FROM erp.sales_invoice_lines
                    WHERE company_id=%s AND sales_invoice_id=%s AND id=ANY(%s::uuid[])
                    ORDER BY line_no
                    FOR UPDATE
                    """,
                    [scope.company_id, source_invoice_id, source_line_ids],
                )
                selected = [row[0] for row in cursor.fetchall()]
            if len(selected) != len(source_line_ids):
                raise APIError(
                    code="INVALID_CREDIT_SOURCE_LINE",
                    message="A selected credit source line is unavailable.",
                )
            if (
                _execute(
                    "SELECT 1 FROM erp.sales_return_lines l JOIN erp.sales_returns r "
                    "ON r.company_id=l.company_id AND r.id=l.sales_return_id "
                    "WHERE l.company_id=%s AND l.sales_invoice_line_id=ANY(%s::uuid[]) "
                    "AND r.status='posted' LIMIT 1",
                    [scope.company_id, selected],
                )
                is not None
            ):
                raise Conflict(
                    "INVOICE_LINE_HAS_RETURN_ADJUSTMENTS",
                    "Use linked return adjustments for lines already partially credited.",
                )
            credit_id = uuid.uuid4()
            _set_context(request_id, "sales_invoice.credit_note.created")
            _execute(
                """
                INSERT INTO erp.sales_invoices(
                    id,company_id,invoice_no,document_kind,partner_id,issue_date,
                    tax_point_date,tax_jurisdiction_id,due_date,currency_code,
                    exchange_rate,status,calculated_at,credit_of_invoice_id
                ) VALUES (%s,%s,%s,'credit_note',%s,%s,%s,%s,%s,%s,%s,
                          'draft',clock_timestamp(),%s)
                RETURNING id
                """,
                [
                    credit_id,
                    scope.company_id,
                    data["invoice_no"],
                    source[0],
                    data["issue_date"],
                    source[1],
                    source[2],
                    data["issue_date"],
                    source[3],
                    source[4],
                    source_invoice_id,
                ],
            )
            _execute(
                """
                INSERT INTO erp.sales_invoice_address_snapshots(
                    company_id,sales_invoice_id,address_kind,recipient_name,
                    tax_registration_no,line_1,line_2,city,region,postal_code,country_code
                ) SELECT company_id,%s,address_kind,recipient_name,tax_registration_no,
                         line_1,line_2,city,region,postal_code,country_code
                  FROM erp.sales_invoice_address_snapshots
                 WHERE company_id=%s AND sales_invoice_id=%s
                RETURNING id
                """,
                [credit_id, scope.company_id, source_invoice_id],
            )
            with connection.cursor() as cursor:
                for line_no, source_line_id in enumerate(selected, start=1):
                    cursor.execute(
                        """
                        INSERT INTO erp.sales_invoice_lines(
                            company_id,sales_invoice_id,line_no,item_id,line_account_id,
                            description,quantity,unit_price,discount_amount,net_amount,
                            tax_code_id,tax_amount,gross_amount,credit_of_invoice_line_id,
                            revenue_account_id_snapshot,inventory_account_id_snapshot,
                            cogs_account_id_snapshot
                        ) SELECT company_id,%s,%s,item_id,line_account_id,description,
                                 quantity,unit_price,discount_amount,net_amount,tax_code_id,
                                 tax_amount,gross_amount,id,revenue_account_id_snapshot,
                                 inventory_account_id_snapshot,cogs_account_id_snapshot
                          FROM erp.sales_invoice_lines
                         WHERE company_id=%s AND sales_invoice_id=%s AND id=%s
                        RETURNING id
                        """,
                        [
                            credit_id,
                            line_no,
                            scope.company_id,
                            source_invoice_id,
                            source_line_id,
                        ],
                    )
                    credit_line_id = cursor.fetchone()[0]
                    cursor.execute(
                        """
                        INSERT INTO erp.sales_invoice_tax_components(
                            company_id,sales_invoice_id,sales_invoice_line_id,tax_code_id,
                            tax_code_component_id,tax_jurisdiction_id,rate_schedule_id,
                            tax_rate_version_id,taxable_base_amount,rate_snapshot,
                            tax_inclusive_snapshot,tax_amount,rate_version_code_snapshot,
                            calculation_method_snapshot,calculation_base_snapshot,
                            fixed_amount_snapshot,recovery_percent_snapshot,
                            rounding_method_snapshot,rounding_precision_snapshot,
                            currency_code_snapshot,output_tax_account_id_snapshot,
                            account_role_snapshot,polarity_snapshot
                        ) SELECT company_id,%s,%s,tax_code_id,tax_code_component_id,
                                 tax_jurisdiction_id,rate_schedule_id,tax_rate_version_id,
                                 taxable_base_amount,rate_snapshot,tax_inclusive_snapshot,
                                 tax_amount,rate_version_code_snapshot,
                                 calculation_method_snapshot,calculation_base_snapshot,
                                 fixed_amount_snapshot,recovery_percent_snapshot,
                                 rounding_method_snapshot,rounding_precision_snapshot,
                                 currency_code_snapshot,output_tax_account_id_snapshot,
                                 'output_tax','debit'
                          FROM erp.sales_invoice_tax_components
                         WHERE company_id=%s AND sales_invoice_id=%s
                           AND sales_invoice_line_id=%s
                        """,
                        [
                            credit_id,
                            credit_line_id,
                            scope.company_id,
                            source_invoice_id,
                            source_line_id,
                        ],
                    )
            result = sales_invoice_detail(scope.company_id, credit_id)
            assert result is not None
            return result
    except IntegrityError as exc:
        raise _write_conflict(exc) from exc
