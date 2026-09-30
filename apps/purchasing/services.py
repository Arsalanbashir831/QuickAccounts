import datetime as dt
import decimal
import hashlib
import json
import uuid
from dataclasses import dataclass
from typing import Any, cast

from django.db import DatabaseError, IntegrityError, connection, transaction

from apps.purchasing.selectors import purchase_bill_detail
from common.access.scopes import (
    CompanyScope,
    assert_company_write,
    bind_and_verify_company,
    require_permission,
)
from common.api.errors import APIError, Conflict, PreconditionFailed, ScopeNotFound

ROUNDING_METHOD = "half_up"
MAX_LINES = 50
SOURCE_ADDRESS_KINDS = {"supplier": "registered", "ship_from": "shipping", "bill_to": "billing"}


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
        cursor.execute("SELECT set_config('app.purchase_bill_action', %s, true)", [action])


def _money_quantizer(minor_units: int) -> decimal.Decimal:
    return decimal.Decimal(1).scaleb(-minor_units)


def _money(value: decimal.Decimal, minor_units: int) -> decimal.Decimal:
    return value.quantize(_money_quantizer(minor_units), rounding=decimal.ROUND_HALF_UP)


def _validate_header(
    company_id: uuid.UUID,
    *,
    supplier_id: uuid.UUID,
    currency_code: str,
    tax_jurisdiction_id: uuid.UUID | None,
    bill_date: dt.date,
    due_date: dt.date | None,
) -> int:
    if due_date is not None and due_date < bill_date:
        raise APIError(code="INVALID_DUE_DATE", message="Due date must not precede bill date.")
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT 1 FROM erp.business_partners
            WHERE company_id = %s AND id = %s AND is_active
              AND partner_kind IN ('supplier', 'both')
            """,
            [company_id, supplier_id],
        )
        if cursor.fetchone() is None:
            raise APIError(
                code="INVALID_BILL_SUPPLIER",
                message="The bill supplier is unavailable or is not a supplier.",
            )
        cursor.execute(
            "SELECT minor_units FROM erp.currencies WHERE code = %s AND is_active",
            [currency_code],
        )
        currency = cursor.fetchone()
        if currency is None:
            raise APIError(code="INVALID_CURRENCY", message="The bill currency is unavailable.")
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
            code="INVALID_BILL_LINE_COUNT",
            message=f"A bill must contain between 1 and {MAX_LINES} lines.",
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
                raise APIError(code="INVALID_BILL_ITEM", message="A bill item is unavailable.")
        if account_ids:
            cursor.execute(
                """
                SELECT id FROM erp.accounts
                WHERE company_id = %s AND id = ANY(%s::uuid[])
                  AND is_active AND allow_posting
                  AND account_type IN ('expense','cost_of_sales','asset')
                """,
                [company_id, account_ids],
            )
            found = {row[0] for row in cursor.fetchall()}
            if found != set(account_ids):
                raise APIError(
                    code="INVALID_BILL_LINE_ACCOUNT",
                    message=(
                        "A bill line account is unavailable or is not a purchase posting account."
                    ),
                )


def _address_rows(
    company_id: uuid.UUID,
    supplier_id: uuid.UUID,
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
                [
                    company_id,
                    supplier_id,
                    address["source_address_id"],
                    SOURCE_ADDRESS_KINDS[address["address_kind"]],
                ],
            )
            source = cursor.fetchone()
            if source is None:
                raise APIError(
                    code="INVALID_BILL_ADDRESS",
                    message="A supplier address is unavailable or does not match its kind.",
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
    extended = cast(decimal.Decimal, line["quantity"]) * cast(decimal.Decimal, line["unit_cost"])
    return _money(extended, minor_units)


def _insert_lines(
    company_id: uuid.UUID,
    bill_id: uuid.UUID,
    lines: list[dict[str, Any]],
    minor_units: int,
) -> None:
    _validate_lines(company_id, lines)
    with connection.cursor() as cursor:
        for line_no, line in enumerate(lines, start=1):
            amount = _base_amount(line, minor_units)
            cursor.execute(
                """
                INSERT INTO erp.purchase_bill_lines(
                    company_id, purchase_bill_id, line_no, item_id, line_account_id,
                    description, quantity, unit_cost, net_amount,
                    tax_code_id, tax_amount, gross_amount
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,0,%s)
                """,
                [
                    company_id,
                    bill_id,
                    line_no,
                    line.get("item_id"),
                    line.get("line_account_id"),
                    line["description"],
                    line["quantity"],
                    line["unit_cost"],
                    amount,
                    line.get("tax_code_id"),
                    amount,
                ],
            )


def _insert_addresses(
    company_id: uuid.UUID,
    bill_id: uuid.UUID,
    rows: list[tuple[object, ...]],
) -> None:
    with connection.cursor() as cursor:
        for row in rows:
            params: list[Any] = [company_id, bill_id, *row]
            cursor.execute(
                """
                INSERT INTO erp.purchase_bill_address_snapshots(
                    company_id, purchase_bill_id, address_kind, recipient_name,
                    tax_registration_no, line_1, line_2, city, region,
                    postal_code, country_code
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                params,
            )


def _write_conflict(exc: IntegrityError) -> Conflict:
    constraint = _constraint_name(exc)
    if constraint == "purchase_bills_company_id_bill_no_key":
        return Conflict(
            "BILL_NUMBER_EXISTS",
            "A purchase bill with this number already exists in the company.",
            {"field": "bill_no"},
        )
    if constraint == "uq_purchase_bill_line_credited_once":
        return Conflict(
            "BILL_LINE_ALREADY_CREDITED",
            "A selected source line already has a linked credit correction.",
        )
    return Conflict(
        "PURCHASE_BILL_CONFLICT",
        "The purchase bill conflicts with existing data.",
        {"constraint": constraint},
    )


def create_purchase_bill(
    scope: CompanyScope, data: dict[str, Any], *, request_id: str | None
) -> dict[str, Any]:
    try:
        with transaction.atomic(durable=True):
            bind_and_verify_company(scope)
            assert_company_write(scope.company_id, "purchasing", "purchasing.bill.edit_draft")
            minor_units = _validate_header(
                scope.company_id,
                supplier_id=data["supplier_id"],
                currency_code=data["currency_code"],
                tax_jurisdiction_id=data.get("tax_jurisdiction_id"),
                bill_date=data["bill_date"],
                due_date=data.get("due_date"),
            )
            _validate_lines(scope.company_id, data["lines"])
            addresses = _address_rows(
                scope.company_id, data["supplier_id"], data.get("addresses", [])
            )
            _set_context(request_id, "purchase_bill.created")
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO erp.purchase_bills(
                        company_id, bill_no, document_kind, supplier_id,
                        bill_date, tax_point_date, tax_jurisdiction_id,
                        due_date, currency_code, exchange_rate, status
                    ) VALUES (%s,%s,'bill',%s,%s,%s,%s,%s,%s,%s,'draft')
                    RETURNING id
                    """,
                    [
                        scope.company_id,
                        data["bill_no"],
                        data["supplier_id"],
                        data["bill_date"],
                        data["tax_point_date"],
                        data.get("tax_jurisdiction_id"),
                        data.get("due_date"),
                        data["currency_code"],
                        data["exchange_rate"],
                    ],
                )
                bill_id = cast(uuid.UUID, cursor.fetchone()[0])
            _insert_addresses(scope.company_id, bill_id, addresses)
            _insert_lines(scope.company_id, bill_id, data["lines"], minor_units)
            result = purchase_bill_detail(scope.company_id, bill_id)
            assert result is not None
            return result
    except IntegrityError as exc:
        raise _write_conflict(exc) from exc


def _locked_bill(
    company_id: uuid.UUID, bill_id: uuid.UUID, expected_revision: int
) -> dict[str, Any]:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT bill_no, supplier_id, bill_date, tax_point_date,
                   tax_jurisdiction_id, due_date, currency_code, exchange_rate,
                   status, row_version, document_kind
            FROM erp.purchase_bills
            WHERE company_id = %s AND id = %s
            FOR UPDATE
            """,
            [company_id, bill_id],
        )
        row = cursor.fetchone()
    if row is None:
        raise ScopeNotFound()
    if row[8] != "draft":
        raise Conflict("BILL_NOT_DRAFT", "Only a draft purchase bill can be changed.")
    if int(row[9]) != expected_revision:
        raise PreconditionFailed(int(row[9]))
    names = (
        "bill_no",
        "supplier_id",
        "bill_date",
        "tax_point_date",
        "tax_jurisdiction_id",
        "due_date",
        "currency_code",
        "exchange_rate",
        "document_kind",
    )
    return dict(zip(names, [*row[:8], row[10]], strict=True))


def update_purchase_bill(
    scope: CompanyScope,
    bill_id: uuid.UUID,
    data: dict[str, Any],
    *,
    expected_revision: int,
    request_id: str | None,
) -> dict[str, Any]:
    try:
        with transaction.atomic(durable=True):
            bind_and_verify_company(scope)
            assert_company_write(scope.company_id, "purchasing", "purchasing.bill.edit_draft")
            current = _locked_bill(scope.company_id, bill_id, expected_revision)
            if current["document_kind"] != "bill":
                raise Conflict(
                    "SUPPLIER_CREDIT_IMMUTABLE",
                    "Linked supplier-credit lines cannot be edited after creation.",
                )
            merged = {**current, **{key: value for key, value in data.items() if key in current}}
            minor_units = _validate_header(
                scope.company_id,
                supplier_id=merged["supplier_id"],
                currency_code=merged["currency_code"],
                tax_jurisdiction_id=merged["tax_jurisdiction_id"],
                bill_date=merged["bill_date"],
                due_date=merged["due_date"],
            )
            addresses = None
            if "addresses" in data:
                addresses = _address_rows(
                    scope.company_id, merged["supplier_id"], data["addresses"]
                )
            if "lines" in data:
                _validate_lines(scope.company_id, data["lines"])
            _set_context(request_id, "purchase_bill.updated")
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    DELETE FROM erp.purchase_bill_tax_components
                    WHERE company_id=%s AND purchase_bill_id=%s
                    """,
                    [scope.company_id, bill_id],
                )
                if "lines" in data:
                    cursor.execute(
                        """
                        DELETE FROM erp.purchase_bill_lines
                        WHERE company_id=%s AND purchase_bill_id=%s
                        """,
                        [scope.company_id, bill_id],
                    )
                    _insert_lines(scope.company_id, bill_id, data["lines"], minor_units)
                else:
                    cursor.execute(
                        """
                        UPDATE erp.purchase_bill_lines
                        SET net_amount = round(quantity * unit_cost, %s),
                            tax_amount = 0,
                            gross_amount = round(quantity * unit_cost, %s)
                        WHERE company_id = %s AND purchase_bill_id = %s
                        """,
                        [minor_units, minor_units, scope.company_id, bill_id],
                    )
                if addresses is not None:
                    cursor.execute(
                        """
                        DELETE FROM erp.purchase_bill_address_snapshots
                        WHERE company_id=%s AND purchase_bill_id=%s
                        """,
                        [scope.company_id, bill_id],
                    )
                    _insert_addresses(scope.company_id, bill_id, addresses)
                cursor.execute(
                    """
                    UPDATE erp.purchase_bills
                    SET bill_no=%s, supplier_id=%s, bill_date=%s,
                        tax_point_date=%s, tax_jurisdiction_id=%s, due_date=%s,
                        currency_code=%s, exchange_rate=%s, calculated_at=NULL
                    WHERE company_id=%s AND id=%s
                    """,
                    [
                        merged["bill_no"],
                        merged["supplier_id"],
                        merged["bill_date"],
                        merged["tax_point_date"],
                        merged["tax_jurisdiction_id"],
                        merged["due_date"],
                        merged["currency_code"],
                        merged["exchange_rate"],
                        scope.company_id,
                        bill_id,
                    ],
                )
            result = purchase_bill_detail(scope.company_id, bill_id)
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
    if not first[3] or first[1] not in {"purchase", "both"}:
        raise APIError(code="INVALID_TAX_CODE", message="The tax code is not active for purchases.")
    if document_jurisdiction_id is None or first[0] != document_jurisdiction_id:
        raise APIError(
            code="TAX_JURISDICTION_MISMATCH",
            message="The bill and tax code jurisdictions do not match.",
        )
    rules: list[dict[str, Any]] = []
    for row in rows:
        if row[7] is None:
            raise APIError(
                code="TAX_RATE_NOT_EFFECTIVE",
                message="No tax rate version is effective on the bill tax point date.",
            )
        if row[14] != "line" or row[15] not in {"input", "bidirectional"}:
            raise APIError(
                code="TAX_RULE_UNSUPPORTED",
                message="This draft calculator supports line-stage input tax rules only.",
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


def calculate_purchase_bill(
    scope: CompanyScope,
    bill_id: uuid.UUID,
    *,
    expected_revision: int,
    request_id: str | None,
) -> dict[str, Any]:
    with transaction.atomic(durable=True):
        bind_and_verify_company(scope)
        assert_company_write(scope.company_id, "purchasing", "purchasing.bill.edit_draft")
        header = _locked_bill(scope.company_id, bill_id, expected_revision)
        if header["document_kind"] != "bill":
            raise Conflict(
                "SUPPLIER_CREDIT_ALREADY_CALCULATED",
                "Linked supplier credits preserve the source bill tax snapshots.",
            )
        minor_units = _validate_header(
            scope.company_id,
            supplier_id=header["supplier_id"],
            currency_code=header["currency_code"],
            tax_jurisdiction_id=header["tax_jurisdiction_id"],
            bill_date=header["bill_date"],
            due_date=header["due_date"],
        )
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, quantity, unit_cost, tax_code_id
                FROM erp.purchase_bill_lines
                WHERE company_id=%s AND purchase_bill_id=%s ORDER BY line_no
                """,
                [scope.company_id, bill_id],
            )
            lines = cursor.fetchall()
        if not lines:
            raise APIError(code="BILL_LINES_REQUIRED", message="The bill has no lines.")
        calculated: list[
            tuple[
                uuid.UUID, decimal.Decimal, decimal.Decimal, decimal.Decimal, list[dict[str, Any]]
            ]
        ] = []
        for line_id, quantity, unit_cost, tax_code_id in lines:
            entered = _money(quantity * unit_cost, minor_units)
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
        _set_context(request_id, "purchase_bill.calculated")
        with connection.cursor() as cursor:
            cursor.execute(
                """
                DELETE FROM erp.purchase_bill_tax_components
                WHERE company_id=%s AND purchase_bill_id=%s
                """,
                [scope.company_id, bill_id],
            )
            for line_id, net, tax, gross, components in calculated:
                cursor.execute(
                    """
                    UPDATE erp.purchase_bill_lines SET net_amount=%s, tax_amount=%s, gross_amount=%s
                    WHERE company_id=%s AND purchase_bill_id=%s AND id=%s
                    """,
                    [net, tax, gross, scope.company_id, bill_id, line_id],
                )
                for component in components:
                    cursor.execute(
                        """
                        INSERT INTO erp.purchase_bill_tax_components(
                            company_id, purchase_bill_id, purchase_bill_line_id,
                            tax_code_id, tax_code_component_id, tax_jurisdiction_id,
                            rate_schedule_id, tax_rate_version_id, taxable_base_amount,
                            rate_snapshot, tax_inclusive_snapshot, tax_amount, recoverable_amount,
                            rate_version_code_snapshot, calculation_method_snapshot,
                            calculation_base_snapshot, fixed_amount_snapshot,
                            recovery_percent_snapshot, rounding_method_snapshot,
                            rounding_precision_snapshot, currency_code_snapshot
                        ) SELECT %s,%s,%s,l.tax_code_id,%s,%s,%s,%s,%s,%s,tc.is_tax_inclusive,%s,%s,
                                 %s,%s,%s,%s,%s,%s,%s,%s
                          FROM erp.purchase_bill_lines l
                          JOIN erp.tax_codes tc
                            ON tc.company_id=l.company_id AND tc.id=l.tax_code_id
                         WHERE l.company_id=%s AND l.purchase_bill_id=%s AND l.id=%s
                        """,
                        [
                            scope.company_id,
                            bill_id,
                            line_id,
                            component["component_id"],
                            component["jurisdiction_id"],
                            component["schedule_id"],
                            component["version_id"],
                            component["taxable_base"],
                            component["rate"],
                            component["tax_amount"],
                            _money(
                                component["tax_amount"] * component["recovery_percent"] / 100,
                                minor_units,
                            ),
                            component["version_code"],
                            component["method"],
                            component["base"],
                            component["fixed_amount"],
                            component["recovery_percent"],
                            ROUNDING_METHOD,
                            minor_units,
                            header["currency_code"],
                            scope.company_id,
                            bill_id,
                            line_id,
                        ],
                    )
            cursor.execute(
                """
                UPDATE erp.purchase_bills SET calculated_at=clock_timestamp()
                WHERE company_id=%s AND id=%s
                """,
                [scope.company_id, bill_id],
            )
        result = purchase_bill_detail(scope.company_id, bill_id)
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
            completed_at=clock_timestamp(),result_type='purchase_bill',result_id=%s
        WHERE id=%s
        RETURNING id
        """,
        [json.dumps(body), body["id"], receipt_id],
    )


def _posting_conflict(exc: DatabaseError) -> Conflict:
    cause = getattr(exc, "__cause__", None)
    constraint = getattr(getattr(cause, "diag", None), "constraint_name", None)
    if constraint == "uq_journal_entry_source_document":
        return Conflict("BILL_ALREADY_POSTED", "The bill already has a posting journal.")
    return Conflict(
        "BILL_POSTING_CONFLICT",
        "The bill could not be posted because a financial invariant was not satisfied.",
        {"constraint": constraint},
    )


def _validate_posting_tax(
    company_id: uuid.UUID,
    bill_id: uuid.UUID,
    supplier_id: uuid.UUID,
    tax_point_date: dt.date,
    document_kind: str,
) -> None:
    if document_kind == "supplier_credit":
        invalid_credit = _execute(
            """
            SELECT count(*)
            FROM erp.purchase_bill_tax_components c
            JOIN erp.purchase_bill_lines credit_line
              ON credit_line.company_id=c.company_id
             AND credit_line.purchase_bill_id=c.purchase_bill_id
             AND credit_line.id=c.purchase_bill_line_id
            JOIN erp.purchase_bill_lines source_line
              ON source_line.company_id=credit_line.company_id
             AND source_line.id=credit_line.credit_of_bill_line_id
            JOIN erp.purchase_bills credit_bill
              ON credit_bill.company_id=credit_line.company_id
             AND credit_bill.id=credit_line.purchase_bill_id
            JOIN erp.currencies curr ON curr.code=credit_bill.currency_code
            LEFT JOIN erp.purchase_bill_tax_components original
              ON original.company_id=credit_line.company_id
             AND original.purchase_bill_line_id=credit_line.credit_of_bill_line_id
             AND original.tax_code_component_id=c.tax_code_component_id
            WHERE c.company_id=%s AND c.purchase_bill_id=%s AND (
                original.id IS NULL
                OR c.tax_rate_version_id<>original.tax_rate_version_id
                OR c.taxable_base_amount<>(
                    round(original.taxable_base_amount*(credit_line.credit_quantity_offset+
                    credit_line.quantity)/source_line.quantity,6)-
                    round(original.taxable_base_amount*credit_line.credit_quantity_offset/
                    source_line.quantity,6))
                OR c.rate_snapshot<>original.rate_snapshot
                OR c.tax_amount<>(round(original.tax_amount*(credit_line.credit_quantity_offset+
                    credit_line.quantity)/source_line.quantity,curr.minor_units)-
                    round(original.tax_amount*credit_line.credit_quantity_offset/
                    source_line.quantity,curr.minor_units))
                OR c.recoverable_amount<>(round(original.recoverable_amount*
                    (credit_line.credit_quantity_offset+credit_line.quantity)/source_line.quantity,
                    curr.minor_units)-round(original.recoverable_amount*
                    credit_line.credit_quantity_offset/source_line.quantity,curr.minor_units))
                OR c.input_tax_account_id_snapshot IS DISTINCT FROM
                   original.input_tax_account_id_snapshot
                OR c.polarity_snapshot<>'credit'
            )
            """,
            [company_id, bill_id],
        )
        missing_credit = _execute(
            """SELECT count(*) FROM erp.purchase_bill_lines l
            JOIN erp.purchase_bill_tax_components original
              ON original.company_id=l.company_id
             AND original.purchase_bill_line_id=l.credit_of_bill_line_id
            WHERE l.company_id=%s AND l.purchase_bill_id=%s AND NOT EXISTS(
              SELECT 1 FROM erp.purchase_bill_tax_components c
              WHERE c.company_id=l.company_id AND c.purchase_bill_line_id=l.id
              AND c.tax_code_component_id=original.tax_code_component_id)""",
            [company_id, bill_id],
        )
        if (invalid_credit is not None and int(invalid_credit[0]) > 0) or (
            missing_credit is not None and int(missing_credit[0]) > 0
        ):
            raise Conflict(
                "CREDIT_TAX_SNAPSHOT_MISMATCH",
                "Credit tax components must exactly reverse their source-line snapshots.",
            )
        return
    invalid = _execute(
        """
        SELECT count(*)
        FROM erp.purchase_bill_tax_components c
        JOIN erp.tax_rate_versions v
          ON v.jurisdiction_id=c.tax_jurisdiction_id
         AND v.rate_schedule_id=c.rate_schedule_id AND v.id=c.tax_rate_version_id
        JOIN erp.tax_code_components cc
          ON cc.company_id=c.company_id AND cc.tax_code_id=c.tax_code_id
         AND cc.id=c.tax_code_component_id
        WHERE c.company_id=%s AND c.purchase_bill_id=%s AND (
            c.rate_version_code_snapshot<>v.version_code
            OR c.calculation_method_snapshot<>v.calculation_method
            OR c.calculation_base_snapshot<>v.calculation_base
            OR c.rate_snapshot<>v.rate
            OR c.recovery_percent_snapshot<>v.recovery_percent
            OR cc.input_tax_account_id IS NULL
        )
        """,
        [company_id, bill_id],
    )
    if invalid is not None and int(invalid[0]) > 0:
        raise Conflict(
            "TAX_SNAPSHOT_STALE",
            "Tax configuration changed or lacks an input-tax account; recalculate the draft.",
        )
    registrations = _execute(
        """
        SELECT count(*)
        FROM (
            SELECT DISTINCT rs.jurisdiction_id, rs.tax_type_id
            FROM erp.purchase_bill_tax_components c
            JOIN erp.tax_rate_schedules rs
              ON rs.jurisdiction_id=c.tax_jurisdiction_id AND rs.id=c.rate_schedule_id
            WHERE c.company_id=%s AND c.purchase_bill_id=%s
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
            bill_id,
            company_id,
            tax_point_date,
            tax_point_date,
            company_id,
            supplier_id,
            tax_point_date,
            tax_point_date,
        ],
    )
    if registrations is not None and int(registrations[0]) > 0:
        raise Conflict(
            "TAX_REGISTRATION_REQUIRED",
            "Effective verified company and supplier tax registrations are required.",
        )
    _execute(
        """
        UPDATE erp.purchase_bill_tax_components c
        SET input_tax_account_id_snapshot=cc.input_tax_account_id,
            account_role_snapshot='input_tax',
            polarity_snapshot=%s
        FROM erp.tax_code_components cc
        WHERE c.company_id=%s AND c.purchase_bill_id=%s
          AND cc.company_id=c.company_id AND cc.tax_code_id=c.tax_code_id
          AND cc.id=c.tax_code_component_id
        RETURNING c.id
        """,
        ["credit" if document_kind == "supplier_credit" else "debit", company_id, bill_id],
    )


def _posting_accounts(
    company_id: uuid.UUID,
    bill_id: uuid.UUID,
    *,
    require_stock: bool,
    document_kind: str,
) -> tuple[uuid.UUID, dict[uuid.UUID, uuid.UUID], dict[uuid.UUID, uuid.UUID]]:
    payable = _execute(
        """
        SELECT r.account_id
        FROM erp.accounting_posting_rules r
        JOIN erp.accounts a ON a.company_id=r.company_id AND a.id=r.account_id
        WHERE r.company_id=%s AND r.event_code='purchase_bill'
          AND r.role_code='accounts_payable'
          AND a.is_active AND a.allow_posting AND a.account_type='payable'
        """,
        [company_id],
    )
    if payable is None:
        raise Conflict(
            "PURCHASE_POSTING_RULE_MISSING",
            "An active accounts-payable posting rule is required.",
        )
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT l.id, l.item_id, l.line_account_id, i.item_kind,
                   p.purchase_account_id, p.inventory_account_id,
                   l.purchase_account_id_snapshot,l.inventory_account_id_snapshot
            FROM erp.purchase_bill_lines l
            LEFT JOIN erp.items i ON i.company_id=l.company_id AND i.id=l.item_id
            LEFT JOIN erp.item_accounting_profiles p
              ON p.company_id=l.company_id AND p.item_id=l.item_id
            WHERE l.company_id=%s AND l.purchase_bill_id=%s
            ORDER BY l.id
            """,
            [company_id, bill_id],
        )
        rows = cursor.fetchall()
    line_accounts: dict[uuid.UUID, uuid.UUID] = {}
    stock: dict[uuid.UUID, uuid.UUID] = {}
    with connection.cursor() as cursor:
        for row in rows:
            (
                line_id,
                item_id,
                line_account_id,
                item_kind,
                purchase_id,
                inventory_id,
                purchase_snapshot,
                inventory_snapshot,
            ) = row
            if document_kind == "supplier_credit":
                account_id = inventory_snapshot if item_kind == "stock" else purchase_snapshot
                inventory_id = inventory_snapshot
            else:
                account_id = (
                    inventory_id if item_kind == "stock" else line_account_id or purchase_id
                )
            if account_id is None:
                raise Conflict(
                    "ITEM_PURCHASE_ACCOUNT_MISSING",
                    "Every bill line requires an active purchase or inventory account mapping.",
                )
            allowed = _execute(
                """
                SELECT 1 FROM erp.accounts
                WHERE company_id=%s AND id=%s AND is_active AND allow_posting
                  AND account_type IN ('expense','cost_of_sales','asset')
                """,
                [company_id, account_id],
            )
            if allowed is None:
                raise Conflict(
                    "ITEM_PURCHASE_ACCOUNT_INVALID",
                    "A bill purchase account is inactive or incompatible.",
                )
            line_accounts[line_id] = account_id
            if item_kind == "stock":
                if inventory_id is None:
                    raise Conflict(
                        "ITEM_STOCK_ACCOUNTS_MISSING",
                        "Stock lines require an inventory account mapping.",
                    )
                stock_account_valid = _execute(
                    """
                    SELECT 1 FROM erp.accounts
                    WHERE company_id=%s AND id=%s AND is_active AND allow_posting
                      AND account_type='asset'
                    """,
                    [company_id, inventory_id],
                )
                if stock_account_valid is None:
                    raise Conflict(
                        "ITEM_STOCK_ACCOUNTS_INVALID",
                        "The inventory account mapping is inactive or incompatible.",
                    )
                stock[item_id] = inventory_id
            if document_kind == "bill":
                cursor.execute(
                    """
                    UPDATE erp.purchase_bill_lines
                    SET purchase_account_id_snapshot=%s,
                        inventory_account_id_snapshot=%s
                    WHERE company_id=%s AND purchase_bill_id=%s AND id=%s
                    """,
                    [purchase_id or line_account_id, inventory_id, company_id, bill_id, line_id],
                )
    if require_stock and not stock:
        raise APIError(
            code="WAREHOUSE_NOT_APPLICABLE",
            message="warehouse_id is only accepted when the bill receives stock.",
        )
    return cast(uuid.UUID, payable[0]), line_accounts, stock


def _journal_line(
    *,
    account_id: uuid.UUID,
    amount: decimal.Decimal,
    currency: str,
    exchange_rate: decimal.Decimal,
    is_debit: bool,
    supplier_id: uuid.UUID | None,
    description: str,
    functional_amount: decimal.Decimal,
) -> dict[str, Any]:
    zero = decimal.Decimal(0)
    return {
        "account_id": account_id,
        "business_partner_id": supplier_id,
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
    bill_id: uuid.UUID,
    bill_date: dt.date,
    warehouse_id: uuid.UUID,
    journal_entry_id: uuid.UUID,
    exchange_rate: decimal.Decimal,
    functional_minor_units: int,
    serial_receipts: list[dict[str, Any]],
) -> None:
    warehouse = _execute(
        "SELECT 1 FROM erp.warehouses WHERE company_id=%s AND id=%s AND is_active FOR SHARE",
        [scope.company_id, warehouse_id],
    )
    if warehouse is None:
        raise APIError(code="INVALID_WAREHOUSE", message="The stock warehouse is unavailable.")
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT l.id,l.item_id,l.quantity,l.net_amount,
                   coalesce(sum(c.tax_amount - c.recoverable_amount),0),
                   i.track_lots,i.track_serials
            FROM erp.purchase_bill_lines l
            JOIN erp.items i ON i.company_id=l.company_id AND i.id=l.item_id
            LEFT JOIN erp.purchase_bill_tax_components c
              ON c.company_id=l.company_id AND c.purchase_bill_id=l.purchase_bill_id
             AND c.purchase_bill_line_id=l.id
            WHERE l.company_id=%s AND l.purchase_bill_id=%s AND i.item_kind='stock'
            GROUP BY l.id,l.item_id,l.quantity,l.net_amount,i.track_lots,i.track_serials
            ORDER BY l.item_id,l.id
            """,
            [scope.company_id, bill_id],
        )
        stock_lines = cursor.fetchall()
    if any(row[5] for row in stock_lines):
        raise Conflict(
            "TRACKED_STOCK_RECEIPT_REQUIRED",
            "Lot-tracked items require a typed goods-receipt workflow.",
        )
    selected = {entry["purchase_bill_line_id"]: entry["serial_numbers"]
                for entry in serial_receipts}
    tracked_ids = {row[0] for row in stock_lines if row[6]}
    if len(selected) != len(serial_receipts) or set(selected) != tracked_ids:
        raise Conflict(
            "PURCHASE_SERIAL_SELECTION_REQUIRED",
            "Provide serials for every serialized bill line and no other lines.",
        )
    with connection.cursor() as cursor:
        for line_id, item_id, quantity, net_amount, nonrecoverable_tax, _, tracked in stock_lines:
            value = _money(
                (cast(decimal.Decimal, net_amount) + cast(decimal.Decimal, nonrecoverable_tax))
                * exchange_rate,
                functional_minor_units,
            )
            unit_cost = (value / cast(decimal.Decimal, quantity)).quantize(
                decimal.Decimal("0.000001"), rounding=decimal.ROUND_HALF_UP
            )
            if tracked:
                labels = [label.strip() for label in selected[line_id]]
                if (
                    quantity != len(labels)
                    or len({label.upper() for label in labels}) != len(labels)
                    or any(not label for label in labels)
                ):
                    raise Conflict(
                        "PURCHASE_SERIAL_COUNT_MISMATCH",
                        "Serialized receipt requires one distinct serial per purchased unit.",
                    )
                cursor.execute(
                    "INSERT INTO erp.inventory_lots(company_id,item_id,lot_code,serial_code) "
                    "SELECT %s,%s,x.label,x.label FROM unnest(%s::text[]) x(label) "
                    "RETURNING id,serial_code",
                    [scope.company_id, item_id, labels],
                )
                lot_ids = {label.upper(): lot_id for lot_id, label in cursor.fetchall()}
                values = [unit_cost] * (len(labels) - 1)
                values.append(value - unit_cost * (len(labels) - 1))
                cursor.execute(
                    "INSERT INTO erp.stock_movements(company_id,event_key,occurred_at,"
                    "warehouse_id,item_id,lot_id,movement_kind,quantity_delta,"
                    "unit_cost_company,value_delta_company,source_type,source_id,"
                    "source_line_id,journal_entry_id) SELECT %s,"
                    "%s||':serial:'||x.lot_id::text,%s,%s,%s,x.lot_id,'receipt',1,"
                    "x.cost,x.cost,'purchase_bill',%s,%s,%s FROM "
                    "unnest(%s::uuid[],%s::numeric[]) x(lot_id,cost)",
                    [
                        scope.company_id, f"purchase_bill:{bill_id}:line:{line_id}",
                        bill_date, warehouse_id, item_id, bill_id, line_id, journal_entry_id,
                        [lot_ids[label.upper()] for label in labels], values,
                    ],
                )
                continue
            cursor.execute(
                """
                INSERT INTO erp.stock_movements(
                    company_id,event_key,occurred_at,warehouse_id,item_id,movement_kind,
                    quantity_delta,unit_cost_company,value_delta_company,source_type,
                    source_id,source_line_id,journal_entry_id
                ) VALUES (%s,%s,%s,%s,%s,'receipt',%s,%s,%s,
                          'purchase_bill',%s,%s,%s)
                """,
                [
                    scope.company_id,
                    f"purchase_bill:{bill_id}:line:{line_id}",
                    bill_date,
                    warehouse_id,
                    item_id,
                    quantity,
                    unit_cost,
                    value,
                    bill_id,
                    line_id,
                    journal_entry_id,
                ],
            )


def post_purchase_bill(
    scope: CompanyScope,
    bill_id: uuid.UUID,
    command: dict[str, Any],
    *,
    expected_revision: int,
    idempotency_key: str,
    request_id: str | None,
) -> tuple[dict[str, Any], int]:
    payload = {
        "bill_id": str(bill_id),
        "expected_revision": expected_revision,
        **command,
    }
    try:
        with transaction.atomic(durable=True):
            bind_and_verify_company(scope)
            receipt = _claim_receipt(
                scope,
                operation="purchasing.bill.post",
                resource_key=str(bill_id),
                idempotency_key=idempotency_key,
                payload=payload,
            )
            if receipt.replay_body is not None:
                require_permission(scope.company_id, "purchasing.bill.view")
                return receipt.replay_body, receipt.replay_status or 200
            assert_company_write(scope.company_id, "purchasing", "purchasing.bill.post")
            warehouse_id = command.get("warehouse_id")
            inventory_preflight = _execute(
                """
                SELECT i.document_kind, EXISTS (
                    SELECT 1
                    FROM erp.purchase_bill_lines l
                    JOIN erp.items item
                      ON item.company_id=l.company_id AND item.id=l.item_id
                    WHERE l.company_id=i.company_id AND l.purchase_bill_id=i.id
                      AND item.item_kind='stock'
                )
                FROM erp.purchase_bills i
                WHERE i.company_id=%s AND i.id=%s
                """,
                [scope.company_id, bill_id],
            )
            if inventory_preflight is None:
                raise ScopeNotFound()
            if (
                inventory_preflight[0] == "bill" and command.get("serial_returns")
            ) or (
                inventory_preflight[0] == "supplier_credit" and command.get("serial_receipts")
            ):
                raise Conflict(
                    "PURCHASE_SERIAL_SELECTION_INVALID",
                    "Serial receipt and return selections must match the document kind.",
                )
            if inventory_preflight[0] == "supplier_credit":
                changed_item = _execute(
                    "SELECT 1 FROM erp.purchase_bill_lines l JOIN erp.items item "
                    "ON item.company_id=l.company_id AND item.id=l.item_id "
                    "WHERE l.company_id=%s AND l.purchase_bill_id=%s AND item.item_kind<>'stock' "
                    "AND EXISTS(SELECT 1 FROM erp.stock_movements m "
                    "WHERE m.company_id=l.company_id AND m.source_type='purchase_bill' "
                    "AND m.source_line_id=l.credit_of_bill_line_id "
                    "AND m.quantity_delta>0) LIMIT 1",
                    [scope.company_id, bill_id],
                )
                if changed_item is not None:
                    raise Conflict(
                        "SUPPLIER_RETURN_ITEM_CHANGED",
                        "A received stock item was reclassified; review it before returning.",
                    )
            if inventory_preflight[0] == "supplier_credit" and inventory_preflight[1]:
                if warehouse_id is None:
                    raise Conflict(
                        "SUPPLIER_RETURN_WAREHOUSE_REQUIRED",
                        "Physical stocked supplier credits require a return warehouse.",
                    )
                assert_company_write(scope.company_id, "inventory", "inventory.post")
            if inventory_preflight[0] == "bill" and inventory_preflight[1]:
                if warehouse_id is None:
                    raise Conflict(
                        "STOCK_RECEIPT_WAREHOUSE_REQUIRED",
                        "A warehouse is required when posting a bill that receives stock.",
                    )
                assert_company_write(scope.company_id, "inventory", "inventory.post")
            elif inventory_preflight[0] == "bill" and warehouse_id is not None:
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
                raise Conflict("PERIOD_NOT_OPEN", "The bill fiscal period is not open.")
            bill = _execute(
                """
                SELECT bill_no,document_kind,supplier_id,bill_date,tax_point_date,
                       currency_code,exchange_rate,status,row_version,calculated_at
                FROM erp.purchase_bills
                WHERE company_id=%s AND id=%s FOR UPDATE
                """,
                [scope.company_id, bill_id],
            )
            if bill is None:
                raise ScopeNotFound()
            if bill[7] != "draft":
                raise Conflict("BILL_NOT_DRAFT", "Only a draft bill can be posted.")
            if int(bill[8]) != expected_revision:
                raise PreconditionFailed(int(bill[8]))
            if not (period[1] <= bill[3] <= period[2]):
                raise Conflict("DATE_OUTSIDE_PERIOD", "Bill date is outside the fiscal period.")
            if bill[9] is None:
                raise Conflict(
                    "BILL_CALCULATION_REQUIRED",
                    "Calculate the current draft revision before posting.",
                )
            journal = _execute(
                """
                SELECT 1 FROM erp.journals
                WHERE company_id=%s AND id=%s AND is_active AND journal_type='purchase'
                FOR SHARE
                """,
                [scope.company_id, command["journal_id"]],
            )
            if journal is None:
                raise APIError(
                    code="INVALID_PURCHASE_JOURNAL",
                    message="The selected active purchase journal is unavailable.",
                )
            missing_tax = _execute(
                """
                SELECT count(*) FROM erp.purchase_bill_lines l
                WHERE l.company_id=%s AND l.purchase_bill_id=%s AND l.tax_code_id IS NOT NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM erp.purchase_bill_tax_components c
                      WHERE c.company_id=l.company_id
                        AND c.purchase_bill_id=l.purchase_bill_id
                        AND c.purchase_bill_line_id=l.id
                  )
                """,
                [scope.company_id, bill_id],
            )
            if missing_tax is not None and int(missing_tax[0]) > 0:
                raise Conflict(
                    "BILL_CALCULATION_REQUIRED",
                    "Every taxable line requires calculated tax components.",
                )
            _validate_posting_tax(
                scope.company_id,
                bill_id,
                bill[2],
                bill[4],
                bill[1],
            )
            payable_id, purchase_accounts, stock_accounts = _posting_accounts(
                scope.company_id,
                bill_id,
                require_stock=warehouse_id is not None,
                document_kind=bill[1],
            )
            if stock_accounts and bill[1] == "bill" and warehouse_id is None:
                raise Conflict(
                    "STOCK_RECEIPT_WAREHOUSE_REQUIRED",
                    "A warehouse is required when posting a bill that receives stock.",
                )
            if bill[1] == "supplier_credit" and warehouse_id is not None and not stock_accounts:
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
            functional_minor_units = int(company_currency[1])
            entry_number = _execute(
                "SELECT erp.allocate_document_number(%s,%s,'PURCHASE_JOURNAL')",
                [scope.company_id, command["fiscal_period_id"]],
            )
            assert entry_number is not None
            final_bill_no = bill[0]
            if str(final_bill_no).startswith("DRAFT-"):
                number = _execute(
                    "SELECT erp.allocate_document_number(%s,%s,'PURCHASE_BILL')",
                    [scope.company_id, command["fiscal_period_id"]],
                )
                assert number is not None
                final_bill_no = number[0]
            entry_id = uuid.uuid4()
            _execute(
                """
                INSERT INTO erp.journal_entries(
                    id,company_id,journal_id,fiscal_period_id,entry_number,entry_date,
                    description,source_type,source_id,idempotency_key,created_by
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,'purchase_bill',%s,%s,%s)
                RETURNING id
                """,
                [
                    entry_id,
                    scope.company_id,
                    command["journal_id"],
                    command["fiscal_period_id"],
                    entry_number[0],
                    bill[3],
                    f"Purchase document {final_bill_no}",
                    bill_id,
                    idempotency_key,
                    scope.user_id,
                ],
            )
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT l.id,l.net_amount,l.tax_amount,l.gross_amount,
                           coalesce(sum(c.tax_amount),0),
                           coalesce(sum(c.recoverable_amount),0)
                    FROM erp.purchase_bill_lines l
                    LEFT JOIN erp.purchase_bill_tax_components c
                      ON c.company_id=l.company_id AND c.purchase_bill_id=l.purchase_bill_id
                     AND c.purchase_bill_line_id=l.id
                    WHERE l.company_id=%s AND l.purchase_bill_id=%s
                    GROUP BY l.id,l.net_amount,l.tax_amount,l.gross_amount,l.line_no
                    ORDER BY l.line_no
                    """,
                    [scope.company_id, bill_id],
                )
                document_lines = cursor.fetchall()
                cursor.execute(
                    """
                    SELECT input_tax_account_id_snapshot,sum(recoverable_amount)
                    FROM erp.purchase_bill_tax_components
                    WHERE company_id=%s AND purchase_bill_id=%s
                    GROUP BY input_tax_account_id_snapshot
                    """,
                    [scope.company_id, bill_id],
                )
                tax_totals = cursor.fetchall()
            is_credit = bill[1] == "supplier_credit"
            transaction_currency = str(bill[5])
            exchange_rate = cast(decimal.Decimal, bill[6])
            purchase_totals: dict[uuid.UUID, decimal.Decimal] = {}
            functional_totals: dict[uuid.UUID, decimal.Decimal] = {}
            gross_total = decimal.Decimal(0)
            for line_id, net, _, gross, total_tax, recoverable_tax in document_lines:
                account_id = purchase_accounts[line_id]
                # Total tax is already represented by gross; only the unrecoverable
                # portion belongs in the purchase/inventory account.
                nonrecoverable_tax = cast(decimal.Decimal, total_tax) - cast(
                    decimal.Decimal, recoverable_tax
                )
                transaction_amount = cast(decimal.Decimal, net) + nonrecoverable_tax
                functional = _money(transaction_amount * exchange_rate, functional_minor_units)
                purchase_totals[account_id] = (
                    purchase_totals.get(account_id, decimal.Decimal(0)) + transaction_amount
                )
                functional_totals[account_id] = (
                    functional_totals.get(account_id, decimal.Decimal(0)) + functional
                )
                gross_total += gross
            journal_lines: list[dict[str, Any]] = []
            counter_functional_total = decimal.Decimal(0)
            for account_id, amount in purchase_totals.items():
                functional = functional_totals[account_id]
                counter_functional_total += functional
                journal_lines.append(
                    _journal_line(
                        account_id=account_id,
                        amount=amount,
                        currency=transaction_currency,
                        exchange_rate=exchange_rate,
                        is_debit=not is_credit,
                        supplier_id=bill[2],
                        description="Purchase reversal" if is_credit else "Purchase",
                        functional_amount=functional,
                    )
                )
            for tax_account_id, amount in tax_totals:
                if tax_account_id is None:
                    raise Conflict(
                        "INPUT_TAX_ACCOUNT_MISSING",
                        "Every calculated tax component requires an input-tax account.",
                    )
                functional = _money(amount * exchange_rate, functional_minor_units)
                counter_functional_total += functional
                journal_lines.append(
                    _journal_line(
                        account_id=tax_account_id,
                        amount=amount,
                        currency=transaction_currency,
                        exchange_rate=exchange_rate,
                        is_debit=not is_credit,
                        supplier_id=bill[2],
                        description="Input tax reversal" if is_credit else "Input tax",
                        functional_amount=functional,
                    )
                )
            journal_lines.insert(
                0,
                _journal_line(
                    account_id=payable_id,
                    amount=gross_total,
                    currency=transaction_currency,
                    exchange_rate=exchange_rate,
                    is_debit=is_credit,
                    supplier_id=bill[2],
                    description="Accounts payable",
                    functional_amount=counter_functional_total,
                ),
            )
            if stock_accounts and not is_credit:
                assert warehouse_id is not None
                _stock_effects(
                    scope,
                    bill_id,
                    bill[3],
                    warehouse_id,
                    entry_id,
                    exchange_rate,
                    functional_minor_units,
                    command.get("serial_receipts", []),
                )
            elif stock_accounts and is_credit:
                from apps.inventory.supplier_returns import issue_supplier_return

                assert warehouse_id is not None
                issue_supplier_return(
                    scope,
                    bill_id,
                    bill[3],
                    warehouse_id,
                    entry_id,
                    exchange_rate,
                    str(company_currency[0]),
                    functional_minor_units,
                    command.get("serial_returns", []),
                )
            _insert_posting_journal_lines(scope.company_id, entry_id, journal_lines)
            _execute("SELECT erp.post_journal_entry(%s,%s)", [scope.company_id, entry_id])
            _set_context(request_id, "purchase_bill.posted")
            _execute(
                """
                UPDATE erp.purchase_bills
                SET bill_no=%s,journal_entry_id=%s,status='posted',posted_at=clock_timestamp()
                WHERE company_id=%s AND id=%s
                RETURNING id
                """,
                [final_bill_no, entry_id, scope.company_id, bill_id],
            )
            body = purchase_bill_detail(scope.company_id, bill_id)
            assert body is not None
            _execute(
                """
                INSERT INTO erp.outbox_events(
                    company_id,event_key,aggregate_type,aggregate_id,event_type,payload
                ) VALUES (%s,%s,'purchase_bill',%s,'purchasing.bill.posted',%s::jsonb)
                RETURNING id
                """,
                [
                    scope.company_id,
                    f"purchasing.bill.posted:{bill_id}:{receipt.receipt_id}",
                    bill_id,
                    json.dumps(
                        {
                            "bill_id": str(bill_id),
                            "bill_no": final_bill_no,
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


def create_linked_supplier_credit(
    scope: CompanyScope,
    source_bill_id: uuid.UUID,
    data: dict[str, Any],
    *,
    request_id: str | None,
) -> dict[str, Any]:
    try:
        with transaction.atomic(durable=True):
            bind_and_verify_company(scope)
            assert_company_write(scope.company_id, "purchasing", "purchasing.bill.edit_draft")
            source = _execute(
                """
                SELECT supplier_id,tax_point_date,tax_jurisdiction_id,currency_code,
                       exchange_rate,status,document_kind
                FROM erp.purchase_bills
                WHERE company_id=%s AND id=%s FOR UPDATE
                """,
                [scope.company_id, source_bill_id],
            )
            if source is None:
                raise ScopeNotFound()
            if source[5] != "posted" or source[6] != "bill":
                raise Conflict(
                    "CREDIT_SOURCE_NOT_POSTED_BILL",
                    "Credit corrections require a posted original bill.",
                )
            source_line_ids = data["source_line_ids"]
            requested = dict(zip(source_line_ids, data.get("partial_quantities", []), strict=False))
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT id FROM erp.purchase_bill_lines
                    WHERE company_id=%s AND purchase_bill_id=%s AND id=ANY(%s::uuid[])
                    ORDER BY line_no
                    FOR UPDATE
                    """,
                    [scope.company_id, source_bill_id, source_line_ids],
                )
                selected = [row[0] for row in cursor.fetchall()]
            if len(selected) != len(source_line_ids):
                raise APIError(
                    code="INVALID_CREDIT_SOURCE_LINE",
                    message="A selected credit source line is unavailable.",
                )
            credit_id = uuid.uuid4()
            _set_context(request_id, "purchase_bill.supplier_credit.created")
            _execute(
                """
                INSERT INTO erp.purchase_bills(
                    id,company_id,bill_no,document_kind,supplier_id,bill_date,
                    tax_point_date,tax_jurisdiction_id,due_date,currency_code,
                    exchange_rate,status,calculated_at,credit_of_bill_id
                ) VALUES (%s,%s,%s,'supplier_credit',%s,%s,%s,%s,%s,%s,%s,
                          'draft',clock_timestamp(),%s)
                RETURNING id
                """,
                [
                    credit_id,
                    scope.company_id,
                    data["bill_no"],
                    source[0],
                    data["bill_date"],
                    source[1],
                    source[2],
                    data["bill_date"],
                    source[3],
                    source[4],
                    source_bill_id,
                ],
            )
            _execute(
                """
                INSERT INTO erp.purchase_bill_address_snapshots(
                    company_id,purchase_bill_id,address_kind,recipient_name,
                    tax_registration_no,line_1,line_2,city,region,postal_code,country_code
                ) SELECT company_id,%s,address_kind,recipient_name,tax_registration_no,
                         line_1,line_2,city,region,postal_code,country_code
                  FROM erp.purchase_bill_address_snapshots
                 WHERE company_id=%s AND purchase_bill_id=%s
                RETURNING id
                """,
                [credit_id, scope.company_id, source_bill_id],
            )
            with connection.cursor() as cursor:
                cursor.execute("SELECT minor_units FROM erp.currencies WHERE code=%s", [source[3]])
                minor_units = cursor.fetchone()[0]

                def portion(
                    value: decimal.Decimal,
                    before: decimal.Decimal,
                    quantity: decimal.Decimal,
                    original: decimal.Decimal,
                    precision: int,
                ) -> decimal.Decimal:
                    return _money(value * (before + quantity) / original, precision) - _money(
                        value * before / original, precision
                    )

                for line_no, source_line_id in enumerate(selected, start=1):
                    cursor.execute(
                        "SELECT quantity,net_amount,tax_amount FROM erp.purchase_bill_lines "
                        "WHERE company_id=%s AND id=%s",
                        [scope.company_id, source_line_id],
                    )
                    original_quantity, original_net, original_tax = cursor.fetchone()
                    cursor.execute(
                        "SELECT coalesce(sum(l.quantity),0),bool_or(b.status='draft') "
                        "FROM erp.purchase_bill_lines l JOIN erp.purchase_bills b "
                        "ON b.company_id=l.company_id AND b.id=l.purchase_bill_id "
                        "WHERE l.company_id=%s AND l.credit_of_bill_line_id=%s "
                        "AND b.status<>'void'",
                        [scope.company_id, source_line_id],
                    )
                    offset, open_draft = cursor.fetchone()
                    quantity = requested.get(source_line_id, original_quantity)
                    if open_draft or quantity <= 0 or offset + quantity > original_quantity:
                        raise Conflict(
                            "BILL_LINE_ALREADY_CREDITED",
                            "Supplier credit quantity exceeds the available source line.",
                        )
                    net = portion(original_net, offset, quantity, original_quantity, minor_units)
                    cursor.execute(
                        "SELECT id,taxable_base_amount,tax_amount,recoverable_amount "
                        "FROM erp.purchase_bill_tax_components WHERE company_id=%s "
                        "AND purchase_bill_line_id=%s ORDER BY id",
                        [scope.company_id, source_line_id],
                    )
                    components = cursor.fetchall()
                    component_amounts = [
                        (
                            component[0],
                            portion(component[1], offset, quantity, original_quantity, 6),
                            portion(component[2], offset, quantity, original_quantity, minor_units),
                            portion(component[3], offset, quantity, original_quantity, minor_units),
                        )
                        for component in components
                    ]
                    tax = sum((c[2] for c in component_amounts), decimal.Decimal(0))
                    if tax != portion(
                        original_tax, offset, quantity, original_quantity, minor_units
                    ):
                        raise Conflict(
                            "CREDIT_TAX_ROUNDING_REQUIRED",
                            "Tax components cannot be apportioned without a rounding variance.",
                        )
                    cursor.execute(
                        """
                        INSERT INTO erp.purchase_bill_lines(
                            company_id,purchase_bill_id,line_no,item_id,line_account_id,
                            description,quantity,unit_cost,net_amount,
                            tax_code_id,tax_amount,gross_amount,credit_of_bill_line_id,
                            purchase_account_id_snapshot,inventory_account_id_snapshot,
                            credit_quantity_offset
                        ) SELECT company_id,%s,%s,item_id,line_account_id,description,
                                 %s,unit_cost,%s,tax_code_id,
                                 %s,%s,id,purchase_account_id_snapshot,
                                 inventory_account_id_snapshot,%s
                          FROM erp.purchase_bill_lines
                         WHERE company_id=%s AND purchase_bill_id=%s AND id=%s
                        RETURNING id
                        """,
                        [
                            credit_id,
                            line_no,
                            quantity,
                            net,
                            tax,
                            net + tax,
                            offset,
                            scope.company_id,
                            source_bill_id,
                            source_line_id,
                        ],
                    )
                    credit_line_id = cursor.fetchone()[0]
                    for component_id, base, amount, recoverable in component_amounts:
                        cursor.execute(
                            """
                        INSERT INTO erp.purchase_bill_tax_components(
                            company_id,purchase_bill_id,purchase_bill_line_id,tax_code_id,
                            tax_code_component_id,tax_jurisdiction_id,rate_schedule_id,
                            tax_rate_version_id,taxable_base_amount,rate_snapshot,
                            tax_inclusive_snapshot,tax_amount,recoverable_amount,
                            rate_version_code_snapshot,
                            calculation_method_snapshot,calculation_base_snapshot,
                            fixed_amount_snapshot,recovery_percent_snapshot,
                            rounding_method_snapshot,rounding_precision_snapshot,
                            currency_code_snapshot,input_tax_account_id_snapshot,
                            account_role_snapshot,polarity_snapshot
                        ) SELECT company_id,%s,%s,tax_code_id,tax_code_component_id,
                                 tax_jurisdiction_id,rate_schedule_id,tax_rate_version_id,
                                 %s,rate_snapshot,tax_inclusive_snapshot,
                                 %s,%s,rate_version_code_snapshot,
                                 calculation_method_snapshot,calculation_base_snapshot,
                                 fixed_amount_snapshot,recovery_percent_snapshot,
                                 rounding_method_snapshot,rounding_precision_snapshot,
                                 currency_code_snapshot,input_tax_account_id_snapshot,
                                 'input_tax','credit'
                          FROM erp.purchase_bill_tax_components
                         WHERE company_id=%s AND id=%s
                        """,
                            [
                                credit_id,
                                credit_line_id,
                                base,
                                amount,
                                recoverable,
                                scope.company_id,
                                component_id,
                            ],
                        )
            result = purchase_bill_detail(scope.company_id, credit_id)
            assert result is not None
            return result
    except IntegrityError as exc:
        raise _write_conflict(exc) from exc
