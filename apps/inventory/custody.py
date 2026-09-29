"""Physical custody before commercial acceptance; never invent owned stock or GL."""

import uuid
from typing import Any, cast

from django.db import transaction

from apps.payments.services import _claim, _context, _json, _rows, _run
from apps.sales.return_services import _effect, _finish, _locked, _one
from common.access.scopes import CompanyScope, assert_company_write, bind_and_verify_company
from common.api.errors import PreconditionFailed


def intake_detail(company: uuid.UUID, intake: uuid.UUID) -> dict[str, Any]:
    result = _one(
        "SELECT c.*,r.id AS sales_return_id,r.status AS return_status,"
        "CASE WHEN c.status='rejected' OR r.status='posted' THEN 0 ELSE c.quantity END "
        "AS custody_quantity FROM erp.return_custody_intakes c "
        "LEFT JOIN erp.sales_return_lines l "
        "ON l.company_id=c.company_id AND l.id=c.sales_return_line_id "
        "LEFT JOIN erp.sales_returns r "
        "ON r.company_id=l.company_id AND r.id=l.sales_return_id WHERE c.company_id=%s AND c.id=%s",
        [company, intake],
    )
    result["owned_inventory"] = result["return_status"] == "posted"
    return cast(dict[str, Any], _json(result))


def list_intakes(
    company: uuid.UUID, after: uuid.UUID | None, limit: int, warehouse: uuid.UUID | None = None
) -> list[dict[str, Any]]:
    return cast(
        list[dict[str, Any]],
        _json(
            _rows(
                "SELECT c.*,CASE WHEN c.status='rejected' OR r.status='posted' "
                "THEN 0 ELSE c.quantity END custody_quantity FROM erp.return_custody_intakes c "
                "LEFT JOIN erp.sales_return_lines l ON l.company_id=c.company_id "
                "AND l.id=c.sales_return_line_id LEFT JOIN erp.sales_returns r "
                "ON r.company_id=l.company_id AND r.id=l.sales_return_id WHERE c.company_id=%s "
                "AND (%s::uuid IS NULL OR c.id>%s) AND (%s::uuid IS NULL OR c.warehouse_id=%s) "
                "ORDER BY c.id LIMIT %s",
                [company, after, after, warehouse, warehouse, limit],
            )
        ),
    )


def receive_intake(
    scope: CompanyScope, data: dict[str, Any], *, key: str, request_id: str | None
) -> dict[str, Any]:
    with transaction.atomic(durable=True):
        bind_and_verify_company(scope)
        receipt, replay = _claim(scope, "inventory.custody.receive", data["intake_no"], key, data)
        assert_company_write(scope.company_id, "inventory", "inventory.post")
        if replay is not None:
            return replay
        _context(request_id)
        intake = _one(
            "INSERT INTO erp.return_custody_intakes(company_id,intake_no,item_id,"
            "warehouse_id,quantity,received_date,reason,customer_reference,"
            "batch_serial_reference) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
            [
                scope.company_id,
                data["intake_no"],
                data["item_id"],
                data["warehouse_id"],
                data["quantity"],
                data["received_date"],
                data["reason"],
                data.get("customer_reference"),
                data.get("batch_serial_reference"),
            ],
        )
        result = intake_detail(scope.company_id, intake["id"])
        _effect(
            scope, intake["id"], "return.custody.received", aggregate_type="return_custody_intake"
        )
        _finish(receipt, result, 201, result_type="return_custody_intake")
        return result


def transition_intake(
    scope: CompanyScope,
    intake: uuid.UUID,
    data: dict[str, Any],
    *,
    revision: int,
    key: str,
    request_id: str | None,
) -> dict[str, Any]:
    with transaction.atomic(durable=True):
        bind_and_verify_company(scope)
        receipt, replay = _claim(
            scope, "inventory.custody.transition", str(intake), key, data | {"revision": revision}
        )
        assert_company_write(scope.company_id, "inventory", "inventory.post")
        if data["status"] == "matched":
            assert_company_write(scope.company_id, "sales", "sales.return.create")
        if replay is not None:
            return replay
        _context(request_id)
        if data["status"] == "matched":
            line = _one(
                "SELECT sales_return_id FROM erp.sales_return_lines WHERE company_id=%s AND id=%s",
                [scope.company_id, data["sales_return_line_id"]],
            )
            _locked(scope, line["sales_return_id"], data["return_revision"])
        current = _one(
            "SELECT * FROM erp.return_custody_intakes WHERE company_id=%s AND id=%s FOR UPDATE",
            [scope.company_id, intake],
        )
        if current["row_version"] != revision:
            raise PreconditionFailed(current["row_version"])
        _run(
            "UPDATE erp.return_custody_intakes SET status=%s,condition=%s,requested_resolution=%s,"
            "inspection_notes=%s,sales_return_line_id=%s WHERE company_id=%s AND id=%s",
            [
                data["status"],
                data.get("condition", current["condition"]),
                data.get("requested_resolution", current["requested_resolution"]),
                data.get("inspection_notes", current["inspection_notes"]),
                data.get("sales_return_line_id"),
                scope.company_id,
                intake,
            ],
        )
        result = intake_detail(scope.company_id, intake)
        _effect(
            scope,
            intake,
            f"return.custody.{data['status']}",
            aggregate_type="return_custody_intake",
        )
        _finish(receipt, result, result_type="return_custody_intake")
        return result
