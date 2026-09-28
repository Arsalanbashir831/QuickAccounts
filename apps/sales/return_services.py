"""Linked sale adjustments. Every financial command owns one outer transaction."""

import datetime as dt
import json
import uuid
from decimal import Decimal
from typing import Any, cast

from django.db import transaction

from apps.inventory.return_costing import prepare_return_costing, record_return_costing
from apps.payments.services import (
    _account,
    _claim,
    _context,
    _json,
    _line,
    _money,
    _posting_period,
    _rows,
    _run,
)
from apps.sales.return_selectors import return_detail
from common.access.scopes import CompanyScope, assert_company_write, bind_and_verify_company
from common.api.errors import Conflict, PreconditionFailed, ScopeNotFound

D = Decimal


def _one(sql: str, params: list[Any]) -> dict[str, Any]:
    rows = _rows(sql, params)
    if not rows:
        raise ScopeNotFound()
    return rows[0]


def _finish(
    receipt: uuid.UUID,
    result: dict[str, Any],
    status: int = 200,
    *,
    result_type: str = "sales_return",
) -> None:
    _run(
        "UPDATE erp.api_command_receipts SET "
        "status='completed',response_status=%s,response_body=%s::jsonb,complete"
        "d_at=clock_timestamp(),result_type=%s,result_id=%s WHERE i"
        "d=%s",
        [status, json.dumps(result), result_type, result["id"], receipt],
    )


def _effect(
    scope: CompanyScope, object_id: uuid.UUID, event: str, *, aggregate_type: str = "sales_return"
) -> None:
    _run(
        "INSERT INTO "
        "erp.outbox_events(company_id,event_key,aggregate_type,aggregate_id,eve"
        "nt_type,payload) VALUES (%s,%s,%s,%s,%s,%s::jsonb)",
        [
            scope.company_id,
            f"{event}:{object_id}",
            aggregate_type,
            object_id,
            event,
            json.dumps({"id": str(object_id)}),
        ],
    )


def _source(company_id: uuid.UUID, invoice_id: uuid.UUID) -> dict[str, Any]:
    source = _one(
        "SELECT i.*,c.functional_currency,fc.minor_units AS "
        "functional_precision,dc.minor_units AS precision FROM erp.sales_invoices i JOIN "
        "erp.companies c ON c.id=i.company_id JOIN erp.currencies fc ON "
        "fc.code=c.functional_currency JOIN erp.currencies dc ON dc.code=i.currency_code "
        "WHERE i.company_id=%s AND i.id=%s FOR UPDATE OF i",
        [company_id, invoice_id],
    )
    if source["status"] != "posted" or source["document_kind"] != "invoice":
        raise Conflict("RETURN_SOURCE_INVALID", "Returns require a posted original invoice.")
    if source["currency_code"] != source["functional_currency"] or source["exchange_rate"] != 1:
        raise Conflict(
            "RETURN_FX_POLICY_REQUIRED",
            "Returns currently require functional-currency sales; foreign-currency "
            "corrections need an FX policy.",
        )
    return source


def _locked(
    scope: CompanyScope, return_id: uuid.UUID, revision: int, *, posted: bool = False
) -> tuple[dict[str, Any], dict[str, Any]]:
    preliminary = _one(
        "SELECT sales_invoice_id FROM erp.sales_returns WHERE company_id=%s AND id=%s",
        [scope.company_id, return_id],
    )
    source = _source(scope.company_id, preliminary["sales_invoice_id"])
    document = _one(
        "SELECT * FROM erp.sales_returns WHERE company_id=%s AND id=%s FOR UPDATE",
        [scope.company_id, return_id],
    )
    if document["row_version"] != revision:
        raise PreconditionFailed(document["row_version"])
    if (posted and document["status"] != "posted") or (
        not posted and document["status"] not in {"draft", "inspected"}
    ):
        raise Conflict("RETURN_STATUS_INVALID", "The return is not in the required state.")
    return document, source


def _replace_lines(
    scope: CompanyScope, document: dict[str, Any], lines: list[dict[str, Any]]
) -> None:
    if not 1 <= len(lines) <= 50 or len({line["sales_invoice_line_id"] for line in lines}) != len(
        lines
    ):
        raise Conflict("RETURN_LINES_INVALID", "Supply one to fifty distinct original lines.")
    _run(
        "DELETE FROM erp.sales_return_tax_components WHERE company_id=%s AND "
        "sales_return_line_id IN (SELECT id FROM erp.sales_return_lines WHERE "
        "company_id=%s AND sales_return_id=%s)",
        [scope.company_id, scope.company_id, document["id"]],
    )
    _run(
        "DELETE FROM erp.sales_return_lines WHERE company_id=%s AND sales_return_id=%s",
        [scope.company_id, document["id"]],
    )
    for line in lines:
        quantity = line.get("quantity", D(0))
        credit = line.get("credit_amount")
        if (document["kind"] == "return" and quantity <= 0) or (
            document["kind"] == "cashback" and (quantity != 0 or credit is None)
        ):
            raise Conflict(
                "RETURN_KIND_MISMATCH",
                "Physical returns require quantity; cash back requires an amount and no quantity.",
            )
        _run(
            "INSERT INTO "
            "erp.sales_return_lines(company_id,sales_return_id,sales_invoice_line_i"
            "d,quantity,requested_credit) VALUES (%s,%s,%s,%s,%s)",
            [scope.company_id, document["id"], line["sales_invoice_line_id"], quantity, credit],
        )


def create_return(
    scope: CompanyScope,
    invoice_id: uuid.UUID,
    data: dict[str, Any],
    *,
    key: str,
    request_id: str | None,
) -> dict[str, Any]:
    with transaction.atomic(durable=True):
        bind_and_verify_company(scope)
        receipt, replay = _claim(scope, "sales.return.create", str(invoice_id), key, data)
        assert_company_write(scope.company_id, "sales", "sales.return.edit_draft")
        if replay is not None:
            return replay
        _context(request_id)
        source = _source(scope.company_id, invoice_id)
        if data["return_date"] < source["issue_date"]:
            raise Conflict("RETURN_PRECEDES_SALE", "Return date cannot precede the sale.")
        document = _one(
            "INSERT INTO "
            "erp.sales_returns(company_id,sales_invoice_id,return_no,kind,return_date,reason) "
            "VALUES (%s,%s,%s,%s,%s,%s) RETURNING *",
            [
                scope.company_id,
                invoice_id,
                data["return_no"],
                data["kind"],
                data["return_date"],
                data["reason"],
            ],
        )
        _replace_lines(scope, document, data["lines"])
        result = return_detail(scope.company_id, document["id"])
        _finish(receipt, result, 201)
        return result


def update_return(
    scope: CompanyScope,
    return_id: uuid.UUID,
    data: dict[str, Any],
    *,
    revision: int,
    request_id: str | None,
) -> dict[str, Any]:
    with transaction.atomic(durable=True):
        bind_and_verify_company(scope)
        assert_company_write(scope.company_id, "sales", "sales.return.edit_draft")
        _context(request_id)
        document, source = _locked(scope, return_id, revision)
        if data.get("return_date", document["return_date"]) < source["issue_date"]:
            raise Conflict("RETURN_PRECEDES_SALE", "Return date cannot precede the sale.")
        _run(
            "UPDATE erp.sales_returns SET reason=%s,return_date=%s,status='draft' WHERE "
            "company_id=%s AND id=%s",
            [
                data.get("reason", document["reason"]),
                data.get("return_date", document["return_date"]),
                scope.company_id,
                return_id,
            ],
        )
        if "lines" in data:
            _replace_lines(scope, document, data["lines"])
        else:
            _run(
                "UPDATE erp.sales_return_lines SET "
                "inspected_at=NULL,condition=NULL,disposition=NULL,warehouse_id=NULL,lo"
                "ss_account_id=NULL WHERE company_id=%s AND sales_return_id=%s",
                [scope.company_id, return_id],
            )
        return return_detail(scope.company_id, return_id)


def inspect_return(
    scope: CompanyScope,
    return_id: uuid.UUID,
    data: dict[str, Any],
    *,
    revision: int,
    key: str,
    request_id: str | None,
) -> dict[str, Any]:
    with transaction.atomic(durable=True):
        bind_and_verify_company(scope)
        receipt, replay = _claim(
            scope, "sales.return.inspect", str(return_id), key, data | {"revision": revision}
        )
        assert_company_write(scope.company_id, "inventory", "inventory.post")
        assert_company_write(scope.company_id, "sales", "sales.return.edit_draft")
        if replay is not None:
            return replay
        _context(request_id)
        document, _ = _locked(scope, return_id, revision)
        if document["kind"] != "return":
            raise Conflict(
                "RETURN_INSPECTION_NOT_APPLICABLE", "Cash back has no physical inspection."
            )
        lines = _rows(
            "SELECT id,quantity FROM erp.sales_return_lines WHERE company_id=%s AND "
            "sales_return_id=%s ORDER BY id FOR UPDATE",
            [scope.company_id, return_id],
        )
        inspections = {line["line_id"]: line for line in data["lines"]}
        if len(inspections) != len(data["lines"]) or set(inspections) != {
            line["id"] for line in lines
        }:
            raise Conflict(
                "RETURN_INSPECTION_INCOMPLETE", "Inspect every return line exactly once."
            )
        for line in lines:
            inspection = inspections[line["id"]]
            if inspection["received_quantity"] != line["quantity"]:
                raise Conflict(
                    "RETURN_RECEIVED_QUANTITY_MISMATCH",
                    "Revise the return draft to match received quantities.",
                )
            warehouse = _one(
                "SELECT stock_category FROM erp.warehouses WHERE company_id=%s AND id=%s AND "
                "is_active FOR SHARE",
                [scope.company_id, inspection["warehouse_id"]],
            )
            category = {"restock": "sellable", "write_off": "damaged"}.get(
                inspection["disposition"], inspection["disposition"]
            )
            if warehouse["stock_category"] != category or (
                inspection["disposition"] == "restock" and inspection["condition"] != "resellable"
            ):
                raise Conflict(
                    "RETURN_DISPOSITION_INVALID",
                    "Condition and destination category do not match the disposition.",
                )
            loss_account = inspection.get("loss_account_id")
            if inspection["disposition"] == "write_off":
                assert_company_write(scope.company_id, "inventory", "inventory.write_off")
                if not inspection.get("approve_write_off") or loss_account is None:
                    raise Conflict(
                        "RETURN_WRITE_OFF_APPROVAL_REQUIRED",
                        "Write-off requires explicit approval and an expense account.",
                    )
                _account(scope.company_id, loss_account, {"expense", "cost_of_sales"})
            elif loss_account is not None:
                raise Conflict(
                    "RETURN_LOSS_ACCOUNT_NOT_APPLICABLE", "Only write-offs accept a loss account."
                )
            _run(
                "UPDATE erp.sales_return_lines SET "
                "condition=%s,disposition=%s,warehouse_id=%s,loss_account_id=%s,inspect"
                "ed_at=clock_timestamp() WHERE company_id=%s AND id=%s",
                [
                    inspection["condition"],
                    inspection["disposition"],
                    inspection["warehouse_id"],
                    loss_account,
                    scope.company_id,
                    line["id"],
                ],
            )
        _run(
            "UPDATE erp.sales_returns SET status='inspected' WHERE company_id=%s AND id=%s",
            [scope.company_id, return_id],
        )
        result = return_detail(scope.company_id, return_id)
        _finish(receipt, result)
        return result


def void_return(
    scope: CompanyScope, return_id: uuid.UUID, *, revision: int, key: str, request_id: str | None
) -> dict[str, Any]:
    with transaction.atomic(durable=True):
        bind_and_verify_company(scope)
        receipt, replay = _claim(
            scope, "sales.return.void", str(return_id), key, {"revision": revision}
        )
        assert_company_write(scope.company_id, "sales", "sales.return.edit_draft")
        if replay is not None:
            return replay
        _context(request_id)
        _locked(scope, return_id, revision)
        _run(
            "UPDATE erp.sales_returns SET status='void' WHERE company_id=%s AND id=%s",
            [scope.company_id, return_id],
        )
        result = return_detail(scope.company_id, return_id)
        _finish(receipt, result)
        return result


def apply_return_credit(
    scope: CompanyScope, return_id: uuid.UUID, *, revision: int, key: str, request_id: str | None
) -> dict[str, Any]:
    with transaction.atomic(durable=True):
        bind_and_verify_company(scope)
        receipt, replay = _claim(
            scope, "sales.return.apply", str(return_id), key, {"revision": revision}
        )
        assert_company_write(scope.company_id, "payments", "payments.post")
        if replay is not None:
            return replay
        _context(request_id)
        document, source = _locked(scope, return_id, revision, posted=True)
        amount = _apply_credit(scope, return_id, source["id"], document["return_date"])
        if amount <= 0:
            raise Conflict(
                "RETURN_CREDIT_NOT_APPLICABLE", "No remaining credit or original invoice balance."
            )
        application = _one(
            "SELECT id FROM erp.sales_return_credit_applications "
            "WHERE company_id=%s AND sales_return_id=%s ORDER BY posted_at DESC,id DESC LIMIT 1",
            [scope.company_id, return_id],
        )
        _effect(
            scope,
            application["id"],
            "sales.return.credit.applied",
            aggregate_type="sales_return_credit_application",
        )
        result = return_detail(scope.company_id, return_id)
        _finish(receipt, result)
        return result


def _control(company_id: uuid.UUID, source: dict[str, Any]) -> uuid.UUID:
    rows = _rows(
        "SELECT l.account_id FROM erp.journal_lines l JOIN erp.accounts a ON "
        "a.company_id=l.company_id AND a.id=l.account_id WHERE l.company_id=%s AND "
        "l.journal_entry_id=%s AND a.account_type='receivable'",
        [company_id, source["journal_entry_id"]],
    )
    if len(rows) != 1:
        raise Conflict(
            "RETURN_CONTROL_ACCOUNT_INVALID", "Original receivable account is ambiguous."
        )
    _account(company_id, rows[0]["account_id"], {"receivable"})
    return cast(uuid.UUID, rows[0]["account_id"])


def _journal(
    scope: CompanyScope,
    object_id: uuid.UUID,
    source_type: str,
    date: dt.date,
    data: dict[str, Any],
    key: str,
) -> uuid.UUID:
    _posting_period(scope.company_id, data["fiscal_period_id"], data["journal_id"], date)
    number = _one(
        "SELECT erp.allocate_document_number(%s,%s,'SALES_JOURNAL') AS number",
        [scope.company_id, data["fiscal_period_id"]],
    )["number"]
    entry = uuid.uuid4()
    _run(
        "INSERT INTO "
        "erp.journal_entries(id,company_id,journal_id,fiscal_period_id,entry_nu"
        "mber,entry_date,source_type,source_id,idempotency_key,created_by) VALU"
        "ES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        [
            entry,
            scope.company_id,
            data["journal_id"],
            data["fiscal_period_id"],
            number,
            date,
            source_type,
            object_id,
            f"{source_type}:{object_id}:{key}",
            scope.user_id,
        ],
    )
    return entry


def _available_credit(company_id: uuid.UUID, return_id: uuid.UUID) -> D:
    return D(
        _one(
            "SELECT r.credit_total-coalesce((SELECT sum(amount) FROM erp.customer_refunds "
            "WHERE company_id=r.company_id AND sales_return_id=r.id),0)-coalesce((SELECT "
            "sum(amount) FROM erp.sales_return_credit_applications WHERE "
            "company_id=r.company_id AND sales_return_id=r.id),0) AS amount FROM "
            "erp.sales_returns r WHERE company_id=%s AND id=%s",
            [company_id, return_id],
        )["amount"]
    )


def _invoice_open(company_id: uuid.UUID, invoice_id: uuid.UUID) -> D:
    return D(
        _one(
            "SELECT coalesce((SELECT sum(gross_amount) FROM erp.sales_invoice_lines WHERE "
            "company_id=i.company_id AND sales_invoice_id=i.id),0)-coalesce((SELECT "
            "sum(a.applied_document_amount) FROM erp.ar_receipt_allocations a JOIN "
            "erp.payments p ON p.company_id=a.company_id AND p.id=a.payment_id WHERE "
            "a.company_id=i.company_id AND a.sales_invoice_id=i.id AND p.status='posted' AND "
            "NOT EXISTS(SELECT 1 FROM erp.payments rv WHERE rv.company_id=p.company_id AND "
            "rv.reversal_of_payment_id=p.id AND rv.status='posted')),0)-coalesce((SELECT "
            "sum(amount) FROM erp.sales_return_credit_applications WHERE "
            "company_id=i.company_id AND sales_invoice_id=i.id),0) AS amount FROM "
            "erp.sales_invoices i WHERE company_id=%s AND id=%s",
            [company_id, invoice_id],
        )["amount"]
    )


def _apply_credit(
    scope: CompanyScope, return_id: uuid.UUID, invoice_id: uuid.UUID, date: dt.date
) -> D:
    amount = min(
        _available_credit(scope.company_id, return_id), _invoice_open(scope.company_id, invoice_id)
    )
    if amount > 0:
        _run(
            "INSERT INTO "
            "erp.sales_return_credit_applications(company_id,sales_return_id,sales_"
            "invoice_id,amount,effective_date) VALUES (%s,%s,%s,%s,%s)",
            [scope.company_id, return_id, invoice_id, amount, date],
        )
    return amount


def post_return(
    scope: CompanyScope,
    return_id: uuid.UUID,
    data: dict[str, Any],
    *,
    revision: int,
    key: str,
    request_id: str | None,
) -> dict[str, Any]:
    with transaction.atomic(durable=True):
        bind_and_verify_company(scope)
        receipt, replay = _claim(
            scope, "sales.return.post", str(return_id), key, data | {"revision": revision}
        )
        assert_company_write(scope.company_id, "sales", "sales.return.post")
        assert_company_write(scope.company_id, "tax_calculation", "sales.return.post")
        if replay is not None:
            return replay
        _context(request_id)
        document, source = _locked(scope, return_id, revision)
        if document["kind"] == "return":
            assert_company_write(scope.company_id, "inventory", "inventory.post")
            if document["status"] != "inspected":
                raise Conflict(
                    "RETURN_INSPECTION_REQUIRED", "Inspect physical returns before posting."
                )
        entry = _journal(scope, return_id, "sales_return", document["return_date"], data, key)
        control = _control(scope.company_id, source)
        lines = _rows(
            "SELECT * FROM erp.sales_return_lines WHERE company_id=%s AND sales_return_id=%s "
            "ORDER BY sales_invoice_line_id FOR UPDATE",
            [scope.company_id, return_id],
        )
        if document["kind"] == "return":
            _run(
                "SELECT pg_advisory_xact_lock_shared(hashtextextended(%s,0))",
                [f"inventory-rebuild:{scope.company_id}"],
            )
            stock_scopes = _rows(
                "SELECT DISTINCT l.warehouse_id,s.item_id FROM erp.sales_return_lines l "
                "JOIN erp.sales_invoice_lines s ON s.company_id=l.company_id "
                "AND s.id=l.sales_invoice_line_id WHERE l.company_id=%s "
                "AND l.sales_return_id=%s ORDER BY l.warehouse_id,s.item_id",
                [scope.company_id, return_id],
            )
            for stock_scope in stock_scopes:
                positions = _rows(
                    "SELECT id FROM erp.inventory_positions WHERE company_id=%s "
                    "AND warehouse_id=%s AND item_id=%s AND lot_id IS NULL FOR UPDATE",
                    [scope.company_id, stock_scope["warehouse_id"], stock_scope["item_id"]],
                )
                if not positions:
                    _one(
                        "SELECT id FROM erp.items WHERE company_id=%s AND id=%s FOR UPDATE",
                        [scope.company_id, stock_scope["item_id"]],
                    )
        journal_lines: list[tuple[uuid.UUID, D, bool, dict[str, Any], int]] = []
        total = D(0)
        for line in lines:
            original = _one(
                "SELECT l.*,i.item_kind,i.track_lots,i.track_serials FROM erp.sales_invoice_lines "
                "l LEFT JOIN erp.items i ON i.company_id=l.company_id AND i.id=l.item_id WHERE "
                "l.company_id=%s AND l.id=%s FOR UPDATE OF l",
                [scope.company_id, line["sales_invoice_line_id"]],
            )
            prior = _one(
                "SELECT coalesce(sum(l.quantity),0) AS quantity,coalesce(sum(l.credit_gross),0) AS "
                "gross,coalesce(sum(l.historical_cost),0) AS cost FROM erp.sales_return_lines l "
                "JOIN erp.sales_returns r ON r.company_id=l.company_id AND r.id=l.sales_return_id "
                "WHERE l.company_id=%s AND l.sales_invoice_line_id=%s AND r.status='posted'",
                [scope.company_id, original["id"]],
            )
            legacy = _one(
                "SELECT coalesce(sum(l.gross_amount),0) AS gross FROM erp.sales_invoice_lines l "
                "JOIN erp.sales_invoices i ON i.company_id=l.company_id AND "
                "i.id=l.sales_invoice_id WHERE l.company_id=%s AND l.credit_of_invoice_line_id=%s "
                "AND i.status IN ('draft','posted')",
                [scope.company_id, original["id"]],
            )["gross"]
            if prior["quantity"] + line["quantity"] > original["quantity"]:
                raise Conflict(
                    "RETURN_QUANTITY_EXCEEDED", "Quantity exceeds the remaining original sale."
                )
            remaining = original["gross_amount"] - prior["gross"] - legacy
            gross = line["requested_credit"]
            if document["kind"] == "return":
                maximum = _money(
                    original["gross_amount"]
                    * (prior["quantity"] + line["quantity"])
                    / original["quantity"],
                    source["precision"],
                ) - _money(
                    original["gross_amount"] * prior["quantity"] / original["quantity"],
                    source["precision"],
                )
                gross = min(maximum, remaining) if gross is None else gross
                if gross > maximum:
                    raise Conflict(
                        "RETURN_CREDIT_EXCEEDED", "Credit exceeds the original returned value."
                    )
            if (
                gross is None
                or gross <= 0
                or gross > remaining
                or _money(gross, source["precision"]) != gross
            ):
                raise Conflict(
                    "RETURN_CREDIT_EXCEEDED",
                    "Credit exceeds remaining sale value or currency precision.",
                )
            if legacy:
                raise Conflict(
                    "RETURN_LEGACY_CREDIT_CONFLICT",
                    "Resolve the existing whole-line credit before returning this line.",
                )
            components = _rows(
                "SELECT * FROM erp.sales_invoice_tax_components WHERE company_id=%s AND "
                "sales_invoice_line_id=%s ORDER BY id",
                [scope.company_id, original["id"]],
            )
            tax_total = D(0)
            _run(
                "DELETE FROM erp.sales_return_tax_components WHERE company_id=%s AND "
                "sales_return_line_id=%s",
                [scope.company_id, line["id"]],
            )
            for component in components:
                if component["rounding_method_snapshot"] != "half_up":
                    raise Conflict(
                        "RETURN_TAX_POLICY_REQUIRED", "Unsupported original tax rounding policy."
                    )
                previous_tax = _one(
                    "SELECT coalesce(sum(t.tax_amount),0) AS amount FROM "
                    "erp.sales_return_tax_components t JOIN erp.sales_return_lines l ON "
                    "l.company_id=t.company_id AND l.id=t.sales_return_line_id JOIN "
                    "erp.sales_returns "
                    "r ON r.company_id=l.company_id AND r.id=l.sales_return_id WHERE t.comp"
                    "any_id=%s "
                    "AND t.source_component_id=%s AND r.status='posted'",
                    [scope.company_id, component["id"]],
                )["amount"]
                amount = (
                    _money(
                        component["tax_amount"]
                        * (prior["gross"] + gross)
                        / original["gross_amount"],
                        component["rounding_precision_snapshot"],
                    )
                    - previous_tax
                )
                base = _money(
                    component["taxable_base_amount"] * gross / original["gross_amount"],
                    component["rounding_precision_snapshot"],
                )
                _run(
                    "INSERT INTO "
                    "erp.sales_return_tax_components(company_id,sales_return_line_id,source"
                    "_component_id,tax_amount,taxable_base_amount,snapshot) VALUES (%s,%s,%"
                    "s,%s,%s,%s::jsonb)",
                    [
                        scope.company_id,
                        line["id"],
                        component["id"],
                        amount,
                        base,
                        json.dumps(
                            _json(
                                component
                                | {
                                    "tax_amount": amount,
                                    "taxable_base_amount": base,
                                    "polarity_snapshot": "debit",
                                }
                            )
                        ),
                    ],
                )
                _account(
                    scope.company_id,
                    component["output_tax_account_id_snapshot"],
                    {"liability", "payable"},
                )
                journal_lines.append(
                    (
                        component["output_tax_account_id_snapshot"],
                        amount,
                        True,
                        source,
                        source["functional_precision"],
                    )
                )
                tax_total += amount
            net = gross - tax_total
            if net < 0:
                raise Conflict(
                    "RETURN_TAX_ROUNDING_INVALID", "Credit is too small to reconcile tax rounding."
                )
            revenue = original["revenue_account_id_snapshot"]
            _account(scope.company_id, revenue, {"revenue"})
            journal_lines.append((revenue, net, True, source, source["functional_precision"]))
            cost = D(0)
            if document["kind"] == "return":
                if (
                    original["item_kind"] != "stock"
                    or original["track_lots"]
                    or original["track_serials"]
                ):
                    raise Conflict(
                        "RETURN_TRACKED_OR_NONSTOCK_UNSUPPORTED",
                        "Physical returns currently require untracked stock items.",
                    )
                issue = _one(
                    "SELECT coalesce(-sum(quantity_delta),0) AS "
                    "quantity,coalesce(-sum(value_delta_company),0) AS cost FROM erp.stock_"
                    "movements "
                    "WHERE company_id=%s AND source_type='sales_invoice' AND source_id=%s AND "
                    "source_line_id=%s AND quantity_delta<0",
                    [scope.company_id, source["id"], original["id"]],
                )
                if issue["quantity"] != original["quantity"]:
                    raise Conflict(
                        "RETURN_HISTORICAL_ISSUE_REQUIRED",
                        "Original fulfillment/cost history does not reconcile.",
                    )
                cost = (
                    _money(
                        issue["cost"]
                        * (prior["quantity"] + line["quantity"])
                        / original["quantity"],
                        6,
                    )
                    - prior["cost"]
                )
                if _money(cost, source["functional_precision"]) != cost:
                    raise Conflict(
                        "RETURN_COST_ROUNDING_POLICY_REQUIRED",
                        "This fractional historical cost requires a reviewed inventory-to-GL "
                        "rounding policy.",
                    )
                warehouse = _one(
                    "SELECT stock_category FROM erp.warehouses WHERE company_id=%s AND id=%s AND "
                    "is_active FOR SHARE",
                    [scope.company_id, line["warehouse_id"]],
                )
                expected_category = {"restock": "sellable", "write_off": "damaged"}.get(
                    line["disposition"], line["disposition"]
                )
                if warehouse["stock_category"] != expected_category:
                    raise Conflict(
                        "RETURN_DISPOSITION_INVALID", "Destination changed since inspection."
                    )
                inventory = original["inventory_account_id_snapshot"]
                cogs = original["cogs_account_id_snapshot"]
                _account(scope.company_id, inventory, {"asset"})
                _account(scope.company_id, cogs, {"expense", "cost_of_sales"})
                stock_source = source | {
                    "currency_code": source["functional_currency"],
                    "exchange_rate": D(1),
                }
                journal_lines.extend(
                    [
                        (inventory, cost, True, stock_source, source["functional_precision"]),
                        (cogs, cost, False, stock_source, source["functional_precision"]),
                    ]
                )
                _run(
                    "INSERT INTO "
                    "erp.stock_movements(company_id,event_key,occurred_at,warehouse_id,item"
                    "_id,movement_kind,quantity_delta,unit_cost_company,value_delta_company"
                    ",source_type,source_id,source_line_id,journal_entry_id) VALUES (%s,%s,"
                    "%s,%s,%s,'receipt',%s,%s,%s,'sales_return',%s,%s,%s)",
                    [
                        scope.company_id,
                        f"return:{return_id}:{line['id']}:receipt",
                        document["return_date"],
                        line["warehouse_id"],
                        original["item_id"],
                        line["quantity"],
                        _money(cost / line["quantity"], 6),
                        cost,
                        return_id,
                        line["id"],
                        entry,
                    ],
                )
                if line["disposition"] == "write_off":
                    assert_company_write(scope.company_id, "inventory", "inventory.write_off")
                    _account(
                        scope.company_id, line["loss_account_id"], {"expense", "cost_of_sales"}
                    )
                    position = _one(
                        "SELECT * FROM erp.inventory_positions WHERE company_id=%s "
                        "AND warehouse_id=%s AND item_id=%s AND lot_id IS NULL FOR UPDATE",
                        [scope.company_id, line["warehouse_id"], original["item_id"]],
                    )
                    unit_cost = _money(cost / line["quantity"], 6)
                    uses = prepare_return_costing(
                        scope.company_id,
                        line["warehouse_id"],
                        original["item_id"],
                        line["id"],
                        position,
                        basis_quantity=line["quantity"],
                        basis_value=cost,
                        quantity=line["quantity"],
                        value=cost,
                        unit_cost=unit_cost,
                    )
                    # The inline basis guard reads this immutable-on-posting line fact.
                    _run(
                        "UPDATE erp.sales_return_lines SET historical_cost=%s "
                        "WHERE company_id=%s AND id=%s",
                        [cost, scope.company_id, line["id"]],
                    )
                    movement = uuid.uuid4()
                    _run(
                        "INSERT INTO "
                        "erp.stock_movements(id,company_id,event_key,occurred_at,warehouse_id,item"
                        "_id,movement_kind,quantity_delta,unit_cost_company,value_delta_company"
                        ",source_type,source_id,source_line_id,journal_entry_id) VALUES (%s,%s,%s,"
                        "%s,%s,%s,'adjustment',%s,%s,%s,'sales_return',%s,%s,%s)",
                        [
                            movement,
                            scope.company_id,
                            f"return:{return_id}:{line['id']}:writeoff",
                            document["return_date"],
                            line["warehouse_id"],
                            original["item_id"],
                            -line["quantity"],
                            _money(cost / line["quantity"], 6),
                            -cost,
                            return_id,
                            line["id"],
                            entry,
                        ],
                    )
                    if uses is not None:
                        record_return_costing(
                            scope.company_id,
                            movement,
                            None,
                            line["id"],
                            position,
                            uses,
                            basis_quantity=line["quantity"],
                            basis_value=cost,
                            quantity=line["quantity"],
                            value=cost,
                            unit_cost=unit_cost,
                            currency=source["functional_currency"],
                            precision=source["functional_precision"],
                        )
                    journal_lines.extend(
                        [
                            (
                                line["loss_account_id"],
                                cost,
                                True,
                                stock_source,
                                source["functional_precision"],
                            ),
                            (inventory, cost, False, stock_source, source["functional_precision"]),
                        ]
                    )
            _run(
                "UPDATE erp.sales_return_lines SET "
                "credit_net=%s,credit_tax=%s,credit_gross=%s,historical_cost=%s WHERE "
                "company_id=%s AND id=%s",
                [net, tax_total, gross, cost, scope.company_id, line["id"]],
            )
            total += gross
        counter = sum(
            (
                _money(amount * facts["exchange_rate"], precision)
                for _, amount, debit, facts, precision in journal_lines
                if debit and facts is source
            ),
            D(0),
        )
        _line(
            scope.company_id,
            entry,
            1,
            control,
            total,
            False,
            source,
            source["functional_precision"],
            functional_amount=counter,
        )
        for index, (account, amount, debit, facts, precision) in enumerate(journal_lines, 2):
            _line(scope.company_id, entry, index, account, amount, debit, facts, precision)
        _run("SELECT erp.post_journal_entry(%s,%s)", [scope.company_id, entry])
        _run(
            "UPDATE erp.sales_returns SET "
            "status='posted',credit_total=%s,journal_entry_id=%s,posted_at=clock_timestamp() "
            "WHERE company_id=%s AND id=%s",
            [total, entry, scope.company_id, return_id],
        )
        _apply_credit(scope, return_id, source["id"], document["return_date"])
        _effect(scope, return_id, "sales.return.posted")
        result = return_detail(scope.company_id, return_id)
        _finish(receipt, result)
        return result


def refund_return(
    scope: CompanyScope,
    return_id: uuid.UUID,
    data: dict[str, Any],
    *,
    revision: int,
    key: str,
    request_id: str | None,
) -> dict[str, Any]:
    with transaction.atomic(durable=True):
        bind_and_verify_company(scope)
        receipt, replay = _claim(
            scope, "sales.return.refund", str(return_id), key, data | {"revision": revision}
        )
        assert_company_write(scope.company_id, "payments", "payments.refund")
        if replay is not None:
            return replay
        _context(request_id)
        payment = _one(
            "SELECT * FROM erp.payments WHERE company_id=%s AND id=%s FOR UPDATE",
            [scope.company_id, data["receipt_payment_id"]],
        )
        document, source = _locked(scope, return_id, revision, posted=True)
        amount = data["amount"]
        if (
            amount > _available_credit(scope.company_id, return_id)
            or _money(amount, source["precision"]) != amount
        ):
            raise Conflict(
                "REFUND_CREDIT_EXCEEDED", "Refund exceeds remaining credit or currency precision."
            )
        if (
            payment["status"] != "posted"
            or payment["direction"] != "receipt"
            or payment["withholding_total"] != 0
        ):
            raise Conflict(
                "REFUND_RECEIPT_INVALID",
                "Refund requires an eligible posted cash receipt without withholding.",
            )
        _account(scope.company_id, data["cash_account_id"], {"asset"})
        refund_id = uuid.uuid4()
        entry = _journal(scope, refund_id, "customer_refund", data["refund_date"], data, key)
        control = _control(scope.company_id, source)
        _line(
            scope.company_id,
            entry,
            1,
            control,
            amount,
            True,
            source,
            source["functional_precision"],
        )
        _line(
            scope.company_id,
            entry,
            2,
            data["cash_account_id"],
            amount,
            False,
            source,
            source["functional_precision"],
        )
        _run("SELECT erp.post_journal_entry(%s,%s)", [scope.company_id, entry])
        _run(
            "INSERT INTO "
            "erp.customer_refunds(id,company_id,sales_return_id,receipt_payment_id,"
            "cash_account_id,amount,refund_date,refund_method,external_reference,jo"
            "urnal_entry_id) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            [
                refund_id,
                scope.company_id,
                return_id,
                payment["id"],
                data["cash_account_id"],
                amount,
                data["refund_date"],
                data["refund_method"],
                data.get("external_reference"),
                entry,
            ],
        )
        _effect(scope, refund_id, "customer.refund.posted", aggregate_type="customer_refund")
        result = return_detail(scope.company_id, return_id)
        _finish(receipt, result)
        return result


def replace_return(
    scope: CompanyScope,
    return_id: uuid.UUID,
    data: dict[str, Any],
    *,
    revision: int,
    key: str,
    request_id: str | None,
) -> dict[str, Any]:
    from apps.sales.services import post_sales_invoice

    with transaction.atomic(durable=True):
        bind_and_verify_company(scope)
        receipt, replay = _claim(
            scope, "sales.return.replace", str(return_id), key, data | {"revision": revision}
        )
        assert_company_write(scope.company_id, "sales", "sales.return.post")
        if replay is not None:
            return replay
        _context(request_id)
        document, source = _locked(scope, return_id, revision, posted=True)
        if document["kind"] != "return":
            raise Conflict("REPLACEMENT_REQUIRES_RETURN", "Cash back cannot create a replacement.")
        replacement = _one(
            "SELECT * FROM erp.sales_invoices WHERE company_id=%s AND id=%s FOR UPDATE",
            [scope.company_id, data["replacement_invoice_id"]],
        )
        if (
            replacement["partner_id"] != source["partner_id"]
            or replacement["currency_code"] != source["currency_code"]
            or replacement["exchange_rate"] != source["exchange_rate"]
            or replacement["id"] == source["id"]
            or replacement["issue_date"] < document["return_date"]
        ):
            raise Conflict(
                "REPLACEMENT_SOURCE_MISMATCH",
                "Replacement must match partner/currency/rate and not precede the return.",
            )
        if _rows(
            "SELECT id FROM erp.sales_return_replacements WHERE company_id=%s AND "
            "sales_return_id=%s",
            [scope.company_id, return_id],
        ):
            raise Conflict("RETURN_ALREADY_REPLACED", "This return already has a replacement.")
        command = {
            k: data[k] for k in ("journal_id", "fiscal_period_id", "warehouse_id") if k in data
        }
        post_sales_invoice(
            scope,
            replacement["id"],
            command,
            expected_revision=data["replacement_revision"],
            idempotency_key=f"replacement:{return_id}:{key}",
            request_id=request_id,
            _nested=True,
        )
        replacement = _one(
            "SELECT * FROM erp.sales_invoices WHERE company_id=%s AND id=%s",
            [scope.company_id, replacement["id"]],
        )
        _run(
            "INSERT INTO "
            "erp.sales_return_replacements(company_id,sales_return_id,replacement_invoice_id) "
            "VALUES (%s,%s,%s)",
            [scope.company_id, return_id, replacement["id"]],
        )
        if _control(scope.company_id, replacement) != _control(scope.company_id, source):
            raise Conflict(
                "REPLACEMENT_CONTROL_MISMATCH",
                "Replacement receivable account must match the original sale.",
            )
        applied = _apply_credit(scope, return_id, replacement["id"], replacement["issue_date"])
        _effect(scope, return_id, "sales.return.replaced")
        result = return_detail(scope.company_id, return_id)
        result["replacement_settlement"] = {
            "applied_credit": str(applied),
            "collect_amount": str(_invoice_open(scope.company_id, replacement["id"])),
            "refund_or_credit": str(_available_credit(scope.company_id, return_id)),
        }
        _finish(receipt, result)
        return result
