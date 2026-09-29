"""Approved disposal orchestration reuses authoritative stock/return commands."""

import uuid
from typing import Any, cast

from django.db import transaction

from apps.inventory.return_stock import dispose_return_stock
from apps.inventory.stock_commands import post_document, save_document
from apps.payments.services import _claim, _context, _json, _rows, _run
from apps.sales.return_services import _effect, _finish, _locked, _one
from common.access.scopes import CompanyScope, assert_company_write, bind_and_verify_company
from common.api.errors import Conflict, PreconditionFailed


def case_detail(company: uuid.UUID, case: uuid.UUID) -> dict[str, Any]:
    return cast(
        dict[str, Any],
        _json(
            _one(
                "SELECT * FROM erp.stock_disposal_cases WHERE company_id=%s AND id=%s",
                [company, case],
            )
        ),
    )


def list_cases(company: uuid.UUID, after: uuid.UUID | None, limit: int) -> list[dict[str, Any]]:
    return cast(
        list[dict[str, Any]],
        _json(
            _rows(
                "SELECT * FROM erp.stock_disposal_cases WHERE "
                "company_id=%s AND (%s::uuid IS NULL OR id>%s) ORDER BY id LIMIT %s",
                [company, after, after, limit],
            )
        ),
    )


def create_case(
    scope: CompanyScope, data: dict[str, Any], *, key: str, request_id: str | None
) -> dict[str, Any]:
    with transaction.atomic(durable=True):
        bind_and_verify_company(scope)
        receipt, replay = _claim(scope, "inventory.disposal.create", data["case_no"], key, data)
        assert_company_write(scope.company_id, "inventory", "inventory.post")
        if replay is not None:
            return replay
        _context(request_id)
        if data.get("sales_return_line_id"):
            source_line = _one(
                "SELECT sales_return_id FROM erp.sales_return_lines WHERE company_id=%s AND id=%s",
                [scope.company_id, data["sales_return_line_id"]],
            )
            source_version = _one(
                "SELECT row_version FROM erp.sales_returns WHERE company_id=%s AND id=%s",
                [scope.company_id, source_line["sales_return_id"]],
            )
            _locked(
                scope, source_line["sales_return_id"], source_version["row_version"], posted=True
            )
        item = _one(
            "SELECT track_lots,track_serials FROM erp.items WHERE company_id=%s AND id=%s",
            [scope.company_id, data["item_id"]],
        )
        if item["track_lots"] or item["track_serials"]:
            raise Conflict(
                "DISPOSAL_TRACKING_UNSUPPORTED", "Use untracked stock for disposal cases."
            )
        position = _one(
            "SELECT on_hand_quantity-reserved_quantity available FROM "
            "erp.inventory_positions WHERE company_id=%s AND warehouse_id=%s "
            "AND item_id=%s AND lot_id IS NULL FOR UPDATE",
            [scope.company_id, data["warehouse_id"], data["item_id"]],
        )
        if data["quantity"] > position["available"]:
            raise Conflict("DISPOSAL_STOCK_UNAVAILABLE", "Case exceeds available stock.")
        row = _one(
            "INSERT INTO erp.stock_disposal_cases(company_id,case_no,item_id,warehouse_id,"
            "quantity,reason,method,sales_return_line_id) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) "
            "RETURNING id",
            [
                scope.company_id,
                data["case_no"],
                data["item_id"],
                data["warehouse_id"],
                data["quantity"],
                data["reason"],
                data["method"],
                data.get("sales_return_line_id"),
            ],
        )
        result = case_detail(scope.company_id, row["id"])
        _effect(scope, row["id"], "stock.disposal.created", aggregate_type="stock_disposal_case")
        _finish(receipt, result, 201, result_type="stock_disposal_case")
        return result


def transition_case(
    scope: CompanyScope,
    case_id: uuid.UUID,
    data: dict[str, Any],
    *,
    revision: int,
    key: str,
    request_id: str | None,
) -> dict[str, Any]:
    with transaction.atomic(durable=True):
        bind_and_verify_company(scope)
        receipt, replay = _claim(
            scope, "inventory.disposal.transition", str(case_id), key, data | {"revision": revision}
        )
        assert_company_write(scope.company_id, "inventory", "inventory.post")
        if replay is not None:
            return replay
        _context(request_id)
        preliminary = _one(
            "SELECT * FROM erp.stock_disposal_cases WHERE company_id=%s AND id=%s",
            [scope.company_id, case_id],
        )
        if data["action"] == "link-sale":
            _one(
                "SELECT id FROM erp.sales_invoices WHERE company_id=%s AND id=%s FOR UPDATE",
                [scope.company_id, data["sales_invoice_id"]],
            )
        return_document = None
        if preliminary["sales_return_line_id"]:
            line = _one(
                "SELECT sales_return_id FROM erp.sales_return_lines WHERE company_id=%s AND id=%s",
                [scope.company_id, preliminary["sales_return_line_id"]],
            )
            version = _one(
                "SELECT row_version FROM erp.sales_returns WHERE company_id=%s AND id=%s",
                [scope.company_id, line["sales_return_id"]],
            )
            return_document, _ = _locked(
                scope, line["sales_return_id"], version["row_version"], posted=True
            )
        case = _one(
            "SELECT * FROM erp.stock_disposal_cases WHERE company_id=%s AND id=%s FOR UPDATE",
            [scope.company_id, case_id],
        )
        if case["row_version"] != revision:
            raise PreconditionFailed(case["row_version"])
        action = data["action"]
        if action == "approve":
            if case["status"] != "draft":
                raise Conflict("DISPOSAL_NOT_DRAFT", "Only draft cases can be approved.")
            if case["method"] == "write_off":
                assert_company_write(scope.company_id, "inventory", "inventory.write_off")
                if not data.get("approve_loss"):
                    raise Conflict("DISPOSAL_APPROVAL_REQUIRED", "Explicit loss approval required.")
            if case["method"] == "liquidation":
                assert_company_write(scope.company_id, "inventory", "inventory.quality.approve")
                if not data.get("approve_quality_release") or not data.get("qc_notes"):
                    raise Conflict("DISPOSAL_QC_REQUIRED", "Approve safe-sale QC with notes.")
            _run(
                "UPDATE erp.stock_disposal_cases SET status='approved',qc_notes=%s "
                "WHERE company_id=%s "
                "AND id=%s",
                [data.get("qc_notes"), scope.company_id, case_id],
            )
        elif action == "void":
            _run(
                "UPDATE erp.stock_disposal_cases SET status='void' WHERE company_id=%s AND id=%s",
                [scope.company_id, case_id],
            )
        elif action == "execute":
            if case["status"] != "approved":
                raise Conflict("DISPOSAL_NOT_APPROVED", "Approve the case before execution.")
            if case["method"] == "supplier_claim":
                raise Conflict(
                    "SUPPLIER_CLAIM_REQUIRED", "Execute through the linked supplier claim."
                )
            destination = data.get("to_warehouse_id")
            if case["method"] == "write_off" and not data.get("loss_account_id"):
                raise Conflict(
                    "DISPOSAL_LOSS_ACCOUNT_REQUIRED", "Select the approved loss account."
                )
            if case["method"] != "write_off":
                warehouse = _one(
                    "SELECT operational_role FROM erp.warehouses WHERE company_id=%s "
                    "AND id=%s AND is_active FOR SHARE",
                    [scope.company_id, destination],
                )
                required = "repair" if case["method"] == "repair_later" else "clearance"
                if warehouse["operational_role"] != required:
                    raise Conflict("DISPOSAL_DESTINATION_INVALID", f"Use a {required} warehouse.")
            elif destination is not None:
                raise Conflict(
                    "WRITE_OFF_DESTINATION_INVALID", "Written-off stock is removed, not restocked."
                )
            common = {
                "fiscal_period_id": data["fiscal_period_id"],
                "journal_id": data["journal_id"],
            }
            if return_document is not None:
                command = common | {
                    "line_id": case["sales_return_line_id"],
                    "from_warehouse_id": case["warehouse_id"],
                    "quantity": case["quantity"],
                    "action_date": data["action_date"],
                    "reason": case["reason"],
                }
                command |= {
                    k: data[k]
                    for k in ("to_warehouse_id", "repair_job_id", "loss_account_id")
                    if k in data
                }
                if case["method"] == "write_off":
                    command["approve_write_off"] = True
                result = dispose_return_stock(
                    scope,
                    return_document["id"],
                    command,
                    revision=return_document["row_version"],
                    key=f"case:{case_id}:{key}",
                    request_id=request_id,
                    _nested=True,
                )
                _run(
                    "UPDATE erp.stock_disposal_cases "
                    "SET return_stock_action_id=%s,status='executed' "
                    "WHERE company_id=%s AND id=%s",
                    [result["id"], scope.company_id, case_id],
                )
            else:
                line = {
                    "item_id": case["item_id"],
                    "from_warehouse_id": case["warehouse_id"],
                    "quantity": case["quantity"],
                }
                if destination is not None:
                    line["to_warehouse_id"] = destination
                document = save_document(
                    scope,
                    {
                        "document_no": f"DIS-{case_id}",
                        "document_kind": "adjustment_out"
                        if case["method"] == "write_off"
                        else "transfer",
                        "document_date": data["action_date"],
                        "reason": case["reason"],
                        "lines": [line],
                    },
                    key=f"case-draft:{case_id}:{key}",
                    request_id=request_id,
                    _nested=True,
                )
                _run(
                    "UPDATE erp.stock_disposal_cases SET stock_document_id=%s "
                    "WHERE company_id=%s AND id=%s",
                    [document["id"], scope.company_id, case_id],
                )
                if case["method"] == "write_off":
                    common |= {"approve_loss": True, "offset_account_id": data["loss_account_id"]}
                post_document(
                    scope,
                    document["id"],
                    common,
                    revision=document["row_version"],
                    key=f"case-post:{case_id}:{key}",
                    request_id=request_id,
                    _nested=True,
                )
                _run(
                    "UPDATE erp.stock_disposal_cases SET status='executed' "
                    "WHERE company_id=%s AND id=%s",
                    [scope.company_id, case_id],
                )
        elif action == "link-sale":
            assert_company_write(scope.company_id, "sales", "sales.invoice.view")
            if (
                case["status"] != "executed"
                or case["method"] != "liquidation"
                or case["sales_invoice_id"]
            ):
                raise Conflict("CLEARANCE_SALE_INVALID", "Link one completed clearance sale.")
            _run(
                "UPDATE erp.stock_disposal_cases SET sales_invoice_id=%s "
                "WHERE company_id=%s AND id=%s",
                [data["sales_invoice_id"], scope.company_id, case_id],
            )
        else:
            raise Conflict("DISPOSAL_ACTION_INVALID", "Unsupported disposal action.")
        result = case_detail(scope.company_id, case_id)
        _effect(
            scope,
            case_id,
            f"stock.disposal.{action}.{result['row_version']}",
            aggregate_type="stock_disposal_case",
        )
        _finish(receipt, result, result_type="stock_disposal_case")
        return result
