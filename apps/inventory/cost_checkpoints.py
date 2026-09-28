"""Explicit adoption of verified stock balances, without historical receipt attribution."""

from typing import Any, cast

from django.db import transaction

from apps.inventory.cost_basis import locked_cost_policy
from apps.payments.services import _claim, _context, _json, _rows, _run
from apps.sales.return_services import _effect, _finish, _one
from common.access.scopes import CompanyScope, assert_company_write, bind_and_verify_company
from common.api.errors import Conflict


def create_cost_checkpoint(
    scope: CompanyScope, data: dict[str, Any], *, key: str, request_id: str | None = None
) -> dict[str, Any]:
    with transaction.atomic(durable=True):
        bind_and_verify_company(scope)
        receipt, replay = _claim(scope, "inventory.cost_checkpoint.create", "new", key, data)
        assert_company_write(scope.company_id, "inventory", "inventory.reconcile")
        if replay is not None:
            return replay
        _context(request_id)
        policy = locked_cost_policy(scope)
        params = [scope.company_id, data["warehouse_id"], data["item_id"], data.get("lot_id")]
        position = _one(
            "SELECT * FROM erp.inventory_positions WHERE company_id=%s AND warehouse_id=%s "
            "AND item_id=%s AND lot_id IS NOT DISTINCT FROM %s FOR UPDATE",
            params,
        )
        if _rows(
            "SELECT id FROM erp.inventory_cost_checkpoints WHERE company_id=%s AND warehouse_id=%s "
            "AND item_id=%s AND lot_id IS NOT DISTINCT FROM %s",
            params,
        ):
            raise Conflict(
                "STOCK_COST_CHECKPOINT_EXISTS", "This stock scope already has a checkpoint."
            )
        movements = _rows(
            "SELECT id,quantity_delta,value_delta_company FROM erp.stock_movements "
            "WHERE company_id=%s AND warehouse_id=%s AND item_id=%s "
            "AND lot_id IS NOT DISTINCT FROM %s ORDER BY id LIMIT 10001",
            params,
        )
        if len(movements) > 10000:
            raise Conflict(
                "STOCK_CHECKPOINT_JOB_REQUIRED", "Scope exceeds the interactive adoption limit."
            )
        if (
            sum(m["quantity_delta"] for m in movements) != position["on_hand_quantity"]
            or sum(m["value_delta_company"] for m in movements) != position["value_company"]
        ):
            raise Conflict("STOCK_CHECKPOINT_DRIFT", "Reconcile stock projection before adoption.")
        result = _one(
            "INSERT INTO erp.inventory_cost_checkpoints(company_id,warehouse_id,item_id,lot_id,"
            "policy_id,quantity,value_company,movement_count,reason) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *",
            params
            + [
                policy["id"],
                position["on_hand_quantity"],
                position["value_company"],
                len(movements),
                data["reason"],
            ],
        )
        _run(
            "INSERT INTO erp.inventory_cost_checkpoint_movements"
            "(company_id,checkpoint_id,movement_id) "
            "SELECT company_id,%s,id FROM erp.stock_movements "
            "WHERE company_id=%s AND warehouse_id=%s "
            "AND item_id=%s AND lot_id IS NOT DISTINCT FROM %s",
            [result["id"], *params],
        )
        _effect(
            scope,
            result["id"],
            "inventory.cost_checkpoint.created",
            aggregate_type="inventory_cost_checkpoint",
        )
        response = cast(dict[str, Any], _json(result))
        _finish(receipt, response, result_type="inventory_cost_checkpoint")
        return response
