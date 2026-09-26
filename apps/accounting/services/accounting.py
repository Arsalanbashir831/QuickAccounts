import hashlib
import json
import uuid
from dataclasses import dataclass
from typing import Any

from django.db import DatabaseError, IntegrityError, connection, transaction

from apps.accounting.selectors.accounting import entry_detail
from common.access.scopes import CompanyScope, assert_company_write, bind_and_verify_company
from common.api.errors import APIError, Conflict, PreconditionFailed, ScopeNotFound


def _execute(sql: str, params: list[object]) -> tuple[object, ...] | None:
    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        if cursor.description is None:
            return None
        return cursor.fetchone()


def _database_conflict(exc: DatabaseError) -> Conflict:
    constraint = getattr(getattr(exc, "__cause__", None), "diag", None)
    return Conflict(
        "DATABASE_CONSTRAINT",
        "The requested change conflicts with accounting data or constraints.",
        {"constraint": getattr(constraint, "constraint_name", None)},
    )


CONFIG_INSERTS = {
    "accounts": (
        """
        INSERT INTO erp.accounts(
            company_id, parent_account_id, code, name, account_type, normal_balance,
            is_control_account, allow_posting, is_active
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id
        """,
        (
            "parent_account_id",
            "code",
            "name",
            "account_type",
            "normal_balance",
            "is_control_account",
            "allow_posting",
            "is_active",
        ),
    ),
    "journals": (
        "INSERT INTO erp.journals(company_id,code,name,journal_type,is_active) "
        "VALUES (%s,%s,%s,%s,%s) RETURNING id",
        ("code", "name", "journal_type", "is_active"),
    ),
    "periods": (
        "INSERT INTO erp.fiscal_periods(company_id,code,starts_on,ends_on) "
        "VALUES (%s,%s,%s,%s) RETURNING id",
        ("code", "starts_on", "ends_on"),
    ),
    "dimension-types": (
        "INSERT INTO erp.dimension_types(company_id,code,name,is_required,is_active) "
        "VALUES (%s,%s,%s,%s,%s) RETURNING id",
        ("code", "name", "is_required", "is_active"),
    ),
    "dimension-values": (
        "INSERT INTO erp.dimension_values"
        "(company_id,dimension_type_id,code,name,is_active) "
        "VALUES (%s,%s,%s,%s,%s) RETURNING id",
        ("dimension_type_id", "code", "name", "is_active"),
    ),
    "posting-rules": (
        "INSERT INTO erp.accounting_posting_rules"
        "(company_id,event_code,role_code,account_id) VALUES (%s,%s,%s,%s) RETURNING id",
        ("event_code", "role_code", "account_id"),
    ),
}


BUSINESS_PROFILE_BY_TYPE = {
    "retail": "retail_wholesale",
    "wholesale": "retail_wholesale",
    "ecommerce": "ecommerce",
    "manufacturing": "manufacturing",
}


def create_config(scope: CompanyScope, resource: str, data: dict[str, Any]) -> uuid.UUID:
    sql, fields = CONFIG_INSERTS[resource]
    try:
        with transaction.atomic(durable=True):
            bind_and_verify_company(scope)
            assert_company_write(scope.company_id, "accounting", "accounting.setup.manage")
            row = _execute(sql, [scope.company_id, *(data[field] for field in fields)])
    except IntegrityError as exc:
        raise _database_conflict(exc) from exc
    assert row is not None
    return row[0]  # type: ignore[return-value]


def update_account(
    scope: CompanyScope,
    account_id: uuid.UUID,
    data: dict[str, Any],
) -> None:
    allowed = {"parent_account_id", "name", "allow_posting", "is_active"}
    fields = [field for field in data if field in allowed]
    if not fields:
        return
    assignments = ", ".join(f"{field} = %s" for field in fields)
    try:
        with transaction.atomic(durable=True):
            bind_and_verify_company(scope)
            assert_company_write(scope.company_id, "accounting", "accounting.setup.manage")
            row = _execute(
                f"UPDATE erp.accounts SET {assignments} "
                "WHERE company_id = %s AND id = %s RETURNING id",
                [*(data[field] for field in fields), scope.company_id, account_id],
            )
            if row is None:
                raise ScopeNotFound()
    except IntegrityError as exc:
        raise _database_conflict(exc) from exc


def delete_account(scope: CompanyScope, account_id: uuid.UUID) -> None:
    try:
        with transaction.atomic(durable=True):
            bind_and_verify_company(scope)
            assert_company_write(scope.company_id, "accounting", "accounting.setup.manage")
            row = _execute(
                "DELETE FROM erp.accounts WHERE company_id = %s AND id = %s RETURNING id",
                [scope.company_id, account_id],
            )
            if row is None:
                raise ScopeNotFound()
    except IntegrityError as exc:
        constraint = getattr(getattr(exc, "__cause__", None), "diag", None)
        raise Conflict(
            "ACCOUNT_IN_USE",
            "The account is referenced by accounting or business data; deactivate it instead.",
            {"constraint": getattr(constraint, "constraint_name", None)},
        ) from exc


def apply_chart_template(
    scope: CompanyScope,
    *,
    template_code: str,
    business_type: str,
) -> dict[str, Any]:
    requested_profile = BUSINESS_PROFILE_BY_TYPE[business_type]
    try:
        with transaction.atomic(durable=True):
            bind_and_verify_company(scope)
            assert_company_write(scope.company_id, "accounting", "accounting.setup.manage")
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT business_type, chart_template_code
                    FROM erp.companies WHERE id = %s FOR UPDATE
                    """,
                    [scope.company_id],
                )
                company = cursor.fetchone()
                if company is None:
                    raise ScopeNotFound()

                cursor.execute(
                    """
                    SELECT code, name, business_profile, version
                    FROM erp.chart_of_account_templates
                    WHERE code = %s AND is_active
                    """,
                    [template_code],
                )
                template = cursor.fetchone()
                if template is None:
                    raise APIError(
                        code="CHART_TEMPLATE_NOT_FOUND",
                        message="The requested chart-of-accounts template is unavailable.",
                        status_code=404,
                    )
                if template[2] != requested_profile:
                    raise APIError(
                        code="BUSINESS_TYPE_TEMPLATE_MISMATCH",
                        message="The chart template is not compatible with this business type.",
                        details={
                            "business_type": business_type,
                            "template_business_profile": template[2],
                        },
                    )

                current_template = company[1]
                if current_template is not None and current_template != template_code:
                    raise Conflict(
                        "CHART_TEMPLATE_CHANGE_REQUIRES_REVIEW",
                        "A different chart template has already been applied. Existing financial "
                        "accounts cannot be replaced automatically.",
                        {
                            "current_template_code": current_template,
                            "requested_template_code": template_code,
                        },
                    )

                cursor.execute(
                    """
                    SELECT a.id, a.code, a.name, a.account_type, a.normal_balance,
                           a.is_control_account, a.allow_posting, parent.code,
                           a.source_template_code, a.source_template_account_code
                    FROM erp.accounts a
                    LEFT JOIN erp.accounts parent
                      ON parent.company_id = a.company_id AND parent.id = a.parent_account_id
                    WHERE a.company_id = %s
                    """,
                    [scope.company_id],
                )
                existing = {row[1]: row for row in cursor.fetchall()}
                cursor.execute(
                    """
                    SELECT code, parent_code, name, account_type, normal_balance,
                           is_control_account, allow_posting
                    FROM erp.chart_of_account_template_accounts
                    WHERE template_code = %s ORDER BY sort_order, code
                    """,
                    [template_code],
                )
                template_accounts = cursor.fetchall()

                account_ids: dict[str, uuid.UUID | str] = {
                    code: row[0] for code, row in existing.items()
                }
                created_codes: list[str] = []
                existing_codes: list[str] = []
                for account in template_accounts:
                    code, parent_code, name, account_type, normal_balance = account[:5]
                    is_control_account, allow_posting = account[5:]
                    current = existing.get(code)
                    if current is not None:
                        from_this_template = (current[8], current[9]) == (template_code, code)
                        current_shape = (
                            current[2],
                            current[3],
                            current[4],
                            current[5],
                            current[6],
                            current[7],
                        )
                        template_shape = (
                            name,
                            account_type,
                            normal_balance,
                            is_control_account,
                            allow_posting,
                            parent_code,
                        )
                        if not from_this_template and current_shape != template_shape:
                            raise Conflict(
                                "CHART_TEMPLATE_ACCOUNT_CONFLICT",
                                "An existing account code has a different structural meaning.",
                                {"account_code": code},
                            )
                        existing_codes.append(code)
                        continue

                    parent_id = account_ids.get(parent_code) if parent_code else None
                    if parent_code and parent_id is None:
                        raise Conflict(
                            "CHART_TEMPLATE_PARENT_MISSING",
                            "A template parent account could not be resolved.",
                            {"account_code": code, "parent_code": parent_code},
                        )
                    cursor.execute(
                        """
                        INSERT INTO erp.accounts(
                            company_id, parent_account_id, code, name, account_type,
                            normal_balance, is_control_account, allow_posting, is_active,
                            source_template_code, source_template_account_code
                        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,true,%s,%s)
                        RETURNING id
                        """,
                        [
                            scope.company_id,
                            parent_id,
                            code,
                            name,
                            account_type,
                            normal_balance,
                            is_control_account,
                            allow_posting,
                            template_code,
                            code,
                        ],
                    )
                    account_id = cursor.fetchone()[0]
                    account_ids[code] = account_id
                    created_codes.append(code)

                cursor.execute(
                    """
                    UPDATE erp.companies
                    SET business_type = %s,
                        chart_template_code = %s,
                        chart_template_applied_at = COALESCE(
                            chart_template_applied_at, clock_timestamp()
                        )
                    WHERE id = %s
                    RETURNING chart_template_applied_at
                    """,
                    [business_type, template_code, scope.company_id],
                )
                applied_at = cursor.fetchone()[0]
    except IntegrityError as exc:
        raise _database_conflict(exc) from exc

    return {
        "business_type": business_type,
        "template_code": template[0],
        "template_name": template[1],
        "template_version": template[3],
        "applied_at": applied_at,
        "created_count": len(created_codes),
        "existing_count": len(existing_codes),
        "created_account_codes": created_codes,
    }


def _insert_lines(company_id: uuid.UUID, entry_id: uuid.UUID, lines: list[dict[str, Any]]) -> None:
    with connection.cursor() as cursor:
        for line_no, line in enumerate(lines, start=1):
            cursor.execute(
                """
                INSERT INTO erp.journal_lines(
                    company_id,journal_entry_id,line_no,account_id,business_partner_id,
                    sales_channel_id,description,transaction_currency,exchange_rate,
                    transaction_debit,transaction_credit,debit_amount,credit_amount
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                RETURNING id
                """,
                [
                    company_id,
                    entry_id,
                    line_no,
                    line["account_id"],
                    line.get("business_partner_id"),
                    line.get("sales_channel_id"),
                    line.get("description"),
                    line["transaction_currency"].upper(),
                    line["exchange_rate"],
                    line["transaction_debit"],
                    line["transaction_credit"],
                    line["debit_amount"],
                    line["credit_amount"],
                ],
            )
            line_id = cursor.fetchone()[0]
            seen_types: set[uuid.UUID] = set()
            for dimension in line.get("dimensions", []):
                dimension_type_id = dimension["dimension_type_id"]
                if dimension_type_id in seen_types:
                    raise Conflict(
                        "DUPLICATE_DIMENSION_TYPE",
                        "A journal line can use each dimension type only once.",
                    )
                seen_types.add(dimension_type_id)
                cursor.execute(
                    """
                    INSERT INTO erp.journal_line_dimensions(
                        company_id,journal_line_id,dimension_type_id,dimension_value_id
                    ) VALUES (%s,%s,%s,%s)
                    """,
                    [
                        company_id,
                        line_id,
                        dimension_type_id,
                        dimension["dimension_value_id"],
                    ],
                )


def create_entry(scope: CompanyScope, data: dict[str, Any]) -> uuid.UUID:
    entry_id = uuid.uuid4()
    draft_number = f"DRAFT-{entry_id}"
    try:
        with transaction.atomic(durable=True):
            bind_and_verify_company(scope)
            assert_company_write(scope.company_id, "accounting", "accounting.entry.create")
            row = _execute(
                """
                INSERT INTO erp.journal_entries(
                    id, company_id, journal_id, fiscal_period_id, entry_number,
                    entry_date, description, created_by
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id
                """,
                [
                    entry_id,
                    scope.company_id,
                    data["journal_id"],
                    data["fiscal_period_id"],
                    draft_number,
                    data["entry_date"],
                    data.get("description"),
                    scope.user_id,
                ],
            )
            assert row is not None
            _insert_lines(scope.company_id, entry_id, data["lines"])
    except IntegrityError as exc:
        raise _database_conflict(exc) from exc
    return entry_id


def update_entry(
    scope: CompanyScope,
    entry_id: uuid.UUID,
    expected_revision: int,
    data: dict[str, Any],
) -> int:
    fields = [field for field in ("fiscal_period_id", "entry_date", "description") if field in data]
    assignments = [f"{field} = %s" for field in fields]
    assignments.append("row_version = row_version + 1")
    try:
        with transaction.atomic(durable=True):
            bind_and_verify_company(scope)
            assert_company_write(scope.company_id, "accounting", "accounting.entry.create")
            row = _execute(
                f"""
                UPDATE erp.journal_entries SET {", ".join(assignments)}
                WHERE company_id = %s AND id = %s AND status = 'draft' AND row_version = %s
                RETURNING row_version
                """,
                [
                    *(data[field] for field in fields),
                    scope.company_id,
                    entry_id,
                    expected_revision,
                ],
            )
            if row is None:
                current = _execute(
                    "SELECT status,row_version FROM erp.journal_entries "
                    "WHERE company_id=%s AND id=%s",
                    [scope.company_id, entry_id],
                )
                if current is None:
                    raise ScopeNotFound()
                if current[0] != "draft":
                    raise Conflict("ENTRY_IMMUTABLE", "Only draft entries can be edited.")
                raise PreconditionFailed(int(current[1]))
            if "lines" in data:
                _execute(
                    "DELETE FROM erp.journal_lines WHERE company_id=%s AND journal_entry_id=%s",
                    [scope.company_id, entry_id],
                )
                _insert_lines(scope.company_id, entry_id, data["lines"])
            return int(row[0])
    except IntegrityError as exc:
        raise _database_conflict(exc) from exc


def _canonical_hash(payload: dict[str, Any]) -> bytes:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).digest()


@dataclass(frozen=True, slots=True)
class Receipt:
    receipt_id: uuid.UUID
    replay_body: dict[str, Any] | None
    replay_status: int | None


def _claim_receipt(
    scope: CompanyScope,
    *,
    operation: str,
    resource_key: str,
    idempotency_key: str,
    payload: dict[str, Any],
) -> Receipt:
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
        return Receipt(row[0], None, None)
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
    replay_body = existing[4]
    if isinstance(replay_body, str):
        replay_body = json.loads(replay_body)
    return Receipt(existing[0], replay_body, int(existing[3]))


def _complete_receipt(
    receipt_id: uuid.UUID,
    status_code: int,
    body: dict[str, Any],
    *,
    result_type: str = "journal_entry",
) -> None:
    _execute(
        """
        UPDATE erp.api_command_receipts
        SET status='completed',response_status=%s,response_body=%s::jsonb,
            completed_at=clock_timestamp(),
            result_type=%s,result_id=%s
        WHERE id=%s
        """,
        [status_code, json.dumps(body), result_type, body["id"], receipt_id],
    )


def _audit_and_outbox(
    scope: CompanyScope,
    *,
    action: str,
    entry_id: uuid.UUID,
    request_id: str | None,
    payload: dict[str, Any],
    object_type: str = "journal_entry",
    event_key_suffix: str = "",
) -> None:
    _execute(
        """
        INSERT INTO erp.company_audit_events(
            tenant_id,company_id,actor_user_id,action,object_type,object_id,new_data,request_id
        ) VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s)
        """,
        [
            scope.tenant_id,
            scope.company_id,
            scope.user_id,
            action,
            object_type,
            str(entry_id),
            json.dumps(payload),
            request_id,
        ],
    )
    _execute(
        """
        INSERT INTO erp.outbox_events(
            company_id,event_key,aggregate_type,aggregate_id,event_type,payload
        ) VALUES (%s,%s,%s,%s,%s,%s::jsonb)
        """,
        [
            scope.company_id,
            f"{action}:{entry_id}:{event_key_suffix}"
            if event_key_suffix
            else f"{action}:{entry_id}",
            object_type,
            entry_id,
            action,
            json.dumps(payload),
        ],
    )


def post_entry(
    scope: CompanyScope,
    *,
    entry_id: uuid.UUID,
    expected_revision: int,
    idempotency_key: str,
    request_id: str | None,
) -> tuple[dict[str, Any], int]:
    payload = {"entry_id": str(entry_id), "expected_revision": expected_revision}
    try:
        with transaction.atomic(durable=True):
            bind_and_verify_company(scope)
            receipt = _claim_receipt(
                scope,
                operation="accounting.entry.post",
                resource_key=str(entry_id),
                idempotency_key=idempotency_key,
                payload=payload,
            )
            if receipt.replay_body is not None:
                return receipt.replay_body, receipt.replay_status or 200
            assert_company_write(scope.company_id, "accounting", "accounting.entry.post")
            pre_read = _execute(
                "SELECT fiscal_period_id FROM erp.journal_entries WHERE company_id=%s AND id=%s",
                [scope.company_id, entry_id],
            )
            if pre_read is None:
                raise ScopeNotFound()
            period_id = pre_read[0]
            period = _execute(
                "SELECT state FROM erp.fiscal_periods WHERE company_id=%s AND id=%s FOR SHARE",
                [scope.company_id, period_id],
            )
            if period is None or period[0] != "open":
                raise Conflict("PERIOD_NOT_OPEN", "The selected fiscal period is not open.")
            entry = _execute(
                """
                SELECT status,row_version,entry_number FROM erp.journal_entries
                WHERE company_id=%s AND id=%s FOR UPDATE
                """,
                [scope.company_id, entry_id],
            )
            if entry is None:
                raise ScopeNotFound()
            if entry[0] != "draft":
                raise Conflict("ENTRY_NOT_DRAFT", "Only draft entries can be posted.")
            if int(entry[1]) != expected_revision:
                raise PreconditionFailed(int(entry[1]))
            _validate_entry_amounts(scope.company_id, entry_id)
            if str(entry[2]).startswith("DRAFT-"):
                _execute(
                    """
                    UPDATE erp.journal_entries
                    SET entry_number=erp.allocate_document_number(
                        company_id,fiscal_period_id,'MANUAL_JOURNAL'
                    )
                    WHERE company_id=%s AND id=%s
                    """,
                    [scope.company_id, entry_id],
                )
            _execute("SELECT erp.post_journal_entry(%s,%s)", [scope.company_id, entry_id])
            body = entry_detail(scope.company_id, entry_id)
            assert body is not None
            _audit_and_outbox(
                scope,
                action="accounting.entry.posted",
                entry_id=entry_id,
                request_id=request_id,
                payload={"entry_number": body["entry_number"]},
            )
            _complete_receipt(receipt.receipt_id, 200, body)
            return body, 200
    except APIError:
        raise
    except DatabaseError as exc:
        raise _database_conflict(exc) from exc


def _period_blockers(company_id: uuid.UUID, period_id: uuid.UUID) -> list[dict[str, Any]]:
    blockers: list[dict[str, Any]] = []
    draft = _execute(
        """
        SELECT count(*) FROM erp.journal_entries
        WHERE company_id=%s AND fiscal_period_id=%s AND status='draft'
        """,
        [company_id, period_id],
    )
    if draft is not None and int(draft[0]) > 0:
        blockers.append({"code": "DRAFT_JOURNALS", "count": int(draft[0])})
    imbalance = _execute(
        """
        SELECT count(*) FROM (
            SELECT e.id
            FROM erp.journal_entries e
            JOIN erp.journal_lines l
              ON l.company_id=e.company_id AND l.journal_entry_id=e.id
            WHERE e.company_id=%s AND e.fiscal_period_id=%s AND e.status='posted'
            GROUP BY e.id
            HAVING sum(l.debit_amount)<>sum(l.credit_amount)
                OR sum(l.transaction_debit)<>sum(l.transaction_credit)
        ) invalid
        """,
        [company_id, period_id],
    )
    if imbalance is not None and int(imbalance[0]) > 0:
        blockers.append({"code": "UNBALANCED_POSTED_JOURNALS", "count": int(imbalance[0])})
    tax = _execute(
        """
        SELECT count(*)
        FROM erp.tax_filing_periods tf
        JOIN erp.fiscal_periods fp ON fp.company_id=tf.company_id
        WHERE fp.company_id=%s AND fp.id=%s
          AND daterange(tf.starts_on,tf.ends_on,'[]') && daterange(fp.starts_on,fp.ends_on,'[]')
          AND tf.status IN ('open','ready')
        """,
        [company_id, period_id],
    )
    if tax is not None and int(tax[0]) > 0:
        blockers.append({"code": "OPEN_TAX_PERIODS", "count": int(tax[0])})
    return blockers


def _validate_entry_amounts(company_id: uuid.UUID, entry_id: uuid.UUID) -> None:
    invalid_precision = _execute(
        """
        SELECT count(*)
        FROM erp.journal_lines l
        JOIN erp.currencies tc ON tc.code=l.transaction_currency
        JOIN erp.companies c ON c.id=l.company_id
        JOIN erp.currencies fc ON fc.code=c.functional_currency
        WHERE l.company_id=%s AND l.journal_entry_id=%s AND (
            l.transaction_debit<>round(l.transaction_debit,tc.minor_units)
            OR l.transaction_credit<>round(l.transaction_credit,tc.minor_units)
            OR l.debit_amount<>round(l.debit_amount,fc.minor_units)
            OR l.credit_amount<>round(l.credit_amount,fc.minor_units)
        )
        """,
        [company_id, entry_id],
    )
    if invalid_precision is not None and int(invalid_precision[0]) > 0:
        raise Conflict(
            "CURRENCY_PRECISION_INVALID",
            "Journal amounts exceed the configured currency precision.",
        )
    invalid_conversion = _execute(
        """
        SELECT count(*)
        FROM erp.journal_lines l
        JOIN erp.companies c ON c.id=l.company_id
        JOIN erp.currencies fc ON fc.code=c.functional_currency
        WHERE l.company_id=%s AND l.journal_entry_id=%s AND (
            (l.transaction_currency=c.functional_currency AND l.exchange_rate<>1)
            OR l.debit_amount<>round(l.transaction_debit*l.exchange_rate,fc.minor_units)
            OR l.credit_amount<>round(l.transaction_credit*l.exchange_rate,fc.minor_units)
        )
        """,
        [company_id, entry_id],
    )
    if invalid_conversion is not None and int(invalid_conversion[0]) > 0:
        raise Conflict(
            "EXCHANGE_RATE_MISMATCH",
            "Functional amounts do not match transaction amounts and exchange rates.",
        )
    unbalanced_currency = _execute(
        """
        SELECT count(*) FROM (
            SELECT transaction_currency
            FROM erp.journal_lines
            WHERE company_id=%s AND journal_entry_id=%s
            GROUP BY transaction_currency
            HAVING sum(transaction_debit)<>sum(transaction_credit)
        ) invalid
        """,
        [company_id, entry_id],
    )
    if unbalanced_currency is not None and int(unbalanced_currency[0]) > 0:
        raise Conflict(
            "TRANSACTION_CURRENCY_UNBALANCED",
            "Each transaction currency must balance independently.",
        )


def change_period_state(
    scope: CompanyScope,
    *,
    period_id: uuid.UUID,
    action: str,
    reason: str,
    expected_revision: int,
    idempotency_key: str,
    request_id: str | None,
) -> tuple[dict[str, Any], int]:
    from apps.accounting.selectors.accounting import period_detail

    permission = f"accounting.period.{action}"
    payload = {
        "period_id": str(period_id),
        "action": action,
        "reason": reason,
        "expected_revision": expected_revision,
    }
    with transaction.atomic(durable=True):
        bind_and_verify_company(scope)
        receipt = _claim_receipt(
            scope,
            operation=permission,
            resource_key=str(period_id),
            idempotency_key=idempotency_key,
            payload=payload,
        )
        if receipt.replay_body is not None:
            return receipt.replay_body, receipt.replay_status or 200
        assert_company_write(scope.company_id, "accounting", permission)
        period = _execute(
            """
            SELECT state,row_version FROM erp.fiscal_periods
            WHERE company_id=%s AND id=%s FOR UPDATE
            """,
            [scope.company_id, period_id],
        )
        if period is None:
            raise ScopeNotFound()
        if int(period[1]) != expected_revision:
            raise PreconditionFailed(int(period[1]))
        if action == "close":
            if period[0] != "open":
                raise Conflict("PERIOD_NOT_OPEN", "Only an open period can be closed.")
            blockers = _period_blockers(scope.company_id, period_id)
            if blockers:
                raise Conflict(
                    "PERIOD_RECONCILIATION_BLOCKED",
                    "The fiscal period has unresolved reconciliation blockers.",
                    {"blockers": blockers},
                )
            _execute("SELECT erp.close_fiscal_period(%s,%s)", [scope.company_id, period_id])
        elif action == "reopen":
            if period[0] == "locked":
                raise Conflict(
                    "PERIOD_LOCKED",
                    "A locked period requires an approved unlock workflow.",
                )
            if period[0] != "closed":
                raise Conflict("PERIOD_NOT_CLOSED", "Only a closed period can be reopened.")
            _execute(
                "UPDATE erp.fiscal_periods SET state='open' WHERE company_id=%s AND id=%s",
                [scope.company_id, period_id],
            )
        else:
            raise ValueError(f"Unsupported period action: {action}")
        body = period_detail(scope.company_id, period_id)
        assert body is not None
        _audit_and_outbox(
            scope,
            action=(
                "accounting.period.closed" if action == "close" else "accounting.period.reopened"
            ),
            entry_id=period_id,
            request_id=request_id,
            payload={"reason": reason, "state": body["state"]},
            object_type="fiscal_period",
            event_key_suffix=str(receipt.receipt_id),
        )
        _complete_receipt(receipt.receipt_id, 200, body, result_type="fiscal_period")
        return body, 200


def reverse_entry(
    scope: CompanyScope,
    *,
    entry_id: uuid.UUID,
    command: dict[str, Any],
    idempotency_key: str,
    request_id: str | None,
) -> tuple[dict[str, Any], int]:
    payload = {"entry_id": str(entry_id), **command}
    try:
        with transaction.atomic(durable=True):
            bind_and_verify_company(scope)
            receipt = _claim_receipt(
                scope,
                operation="accounting.entry.reverse",
                resource_key=str(entry_id),
                idempotency_key=idempotency_key,
                payload=payload,
            )
            if receipt.replay_body is not None:
                return receipt.replay_body, receipt.replay_status or 201
            assert_company_write(scope.company_id, "accounting", "accounting.entry.reverse")
            period = _execute(
                """
                SELECT state,starts_on,ends_on FROM erp.fiscal_periods
                WHERE company_id=%s AND id=%s FOR SHARE
                """,
                [scope.company_id, command["fiscal_period_id"]],
            )
            if period is None or period[0] != "open":
                raise Conflict("PERIOD_NOT_OPEN", "The reversal period is not open.")
            if not (period[1] <= command["entry_date"] <= period[2]):
                raise Conflict("DATE_OUTSIDE_PERIOD", "The reversal date is outside the period.")
            original = _execute(
                """
                SELECT journal_id,status,source_type FROM erp.journal_entries
                WHERE company_id=%s AND id=%s FOR UPDATE
                """,
                [scope.company_id, entry_id],
            )
            if original is None:
                raise ScopeNotFound()
            if original[1] != "posted":
                raise Conflict("ENTRY_NOT_POSTED", "Only posted entries can be reversed.")
            if original[2] is not None:
                raise Conflict(
                    "SOURCE_CORRECTION_REQUIRED",
                    "Source-linked journals must be corrected through their source document.",
                )
            reversal_id = uuid.uuid4()
            number_row = _execute(
                "SELECT erp.allocate_document_number(%s,%s,'MANUAL_JOURNAL')",
                [scope.company_id, command["fiscal_period_id"]],
            )
            assert number_row is not None
            _execute(
                """
                INSERT INTO erp.journal_entries(
                    id,company_id,journal_id,fiscal_period_id,entry_number,entry_date,
                    description,reversal_of_entry_id,created_by
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                [
                    reversal_id,
                    scope.company_id,
                    original[0],
                    command["fiscal_period_id"],
                    number_row[0],
                    command["entry_date"],
                    command["reason"],
                    entry_id,
                    scope.user_id,
                ],
            )
            _execute(
                """
                INSERT INTO erp.journal_lines(
                    company_id,journal_entry_id,line_no,account_id,business_partner_id,
                    sales_channel_id,description,transaction_currency,exchange_rate,
                    transaction_debit,transaction_credit,debit_amount,credit_amount
                )
                SELECT company_id,%s,line_no,account_id,business_partner_id,sales_channel_id,
                       description,transaction_currency,exchange_rate,
                       transaction_credit,transaction_debit,credit_amount,debit_amount
                FROM erp.journal_lines
                WHERE company_id=%s AND journal_entry_id=%s
                """,
                [reversal_id, scope.company_id, entry_id],
            )
            _execute(
                """
                INSERT INTO erp.journal_line_dimensions(
                    company_id,journal_line_id,dimension_type_id,dimension_value_id
                )
                SELECT original.company_id,reversal.id,d.dimension_type_id,d.dimension_value_id
                FROM erp.journal_lines original
                JOIN erp.journal_lines reversal
                  ON reversal.company_id=original.company_id
                 AND reversal.journal_entry_id=%s
                 AND reversal.line_no=original.line_no
                JOIN erp.journal_line_dimensions d
                  ON d.company_id=original.company_id AND d.journal_line_id=original.id
                WHERE original.company_id=%s AND original.journal_entry_id=%s
                """,
                [reversal_id, scope.company_id, entry_id],
            )
            _execute("SELECT erp.post_journal_entry(%s,%s)", [scope.company_id, reversal_id])
            body = entry_detail(scope.company_id, reversal_id)
            assert body is not None
            _audit_and_outbox(
                scope,
                action="accounting.entry.reversed",
                entry_id=reversal_id,
                request_id=request_id,
                payload={"reversal_of_entry_id": str(entry_id), "reason": command["reason"]},
            )
            _complete_receipt(receipt.receipt_id, 201, body)
            return body, 201
    except APIError:
        raise
    except DatabaseError as exc:
        raise _database_conflict(exc) from exc
