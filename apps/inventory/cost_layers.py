"""Layer retention for explicitly checkpoint-adopted stock scopes."""

import uuid
from typing import Any

from apps.inventory.layer_costing import LayerBalance, LayerUse
from apps.payments.services import _rows, _run
from common.api.errors import Conflict


def locked_layers(company: uuid.UUID, scope_key: tuple[Any, ...]) -> list[LayerBalance] | None:
    """Caller already holds the scope position lock. No implicit adoption."""
    params = [company, *scope_key]
    checkpoints = _rows(
        "SELECT * FROM erp.inventory_cost_checkpoints WHERE company_id=%s AND warehouse_id=%s "
        "AND item_id=%s AND lot_id IS NOT DISTINCT FROM %s",
        params,
    )
    if not checkpoints:
        return None
    checkpoint = checkpoints[0]
    if checkpoint["quantity"]:
        _run(
            "INSERT INTO erp.inventory_cost_layers(id,company_id,warehouse_id,item_id,lot_id,"
            "opening_checkpoint_id,quantity,value_company) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT(company_id,opening_checkpoint_id) DO NOTHING",
            [
                uuid.uuid5(
                    uuid.NAMESPACE_URL, f"inventory-layer:{company}:checkpoint:{checkpoint['id']}"
                )
            ]
            + params
            + [checkpoint["id"], checkpoint["quantity"], checkpoint["value_company"]],
        )
    # Exact checkpoint membership, not dates or UUID order, defines covered history.
    receipts = _rows(
        "SELECT m.id,m.quantity_delta,m.value_delta_company FROM erp.stock_movements m "
        "WHERE m.company_id=%s AND m.warehouse_id=%s AND m.item_id=%s "
        "AND m.lot_id IS NOT DISTINCT FROM %s AND m.quantity_delta>0 "
        "AND NOT EXISTS(SELECT 1 FROM erp.inventory_cost_checkpoint_movements c "
        "WHERE c.company_id=m.company_id AND c.checkpoint_id=%s AND c.movement_id=m.id) "
        "AND NOT EXISTS(SELECT 1 FROM erp.inventory_cost_layers l "
        "WHERE l.company_id=m.company_id AND l.receipt_movement_id=m.id) ORDER BY m.id LIMIT 1001",
        params + [checkpoint["id"]],
    )
    if len(receipts) > 1000:
        raise Conflict(
            "STOCK_LAYER_JOB_REQUIRED", "Receipt adoption exceeds the interactive limit."
        )
    for receipt in receipts:
        _run(
            "INSERT INTO erp.inventory_cost_layers(id,company_id,warehouse_id,item_id,lot_id,"
            "receipt_movement_id,quantity,value_company) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
            [uuid.uuid5(uuid.NAMESPACE_URL, f"inventory-layer:{company}:receipt:{receipt['id']}")]
            + params
            + [receipt["id"], receipt["quantity_delta"], receipt["value_delta_company"]],
        )
    rows = _rows(
        "SELECT l.* FROM erp.inventory_cost_layers l WHERE company_id=%s AND warehouse_id=%s "
        "AND item_id=%s AND lot_id IS NOT DISTINCT FROM %s ORDER BY id LIMIT 1001 FOR UPDATE",
        params,
    )
    if len(rows) > 1000:
        raise Conflict("STOCK_LAYER_JOB_REQUIRED", "Layer scope exceeds the interactive limit.")
    totals = {
        row["cost_layer_id"]: row
        for row in _rows(
            "SELECT cost_layer_id,sum(quantity) quantity,sum(value_company) value "
            "FROM erp.inventory_cost_allocations WHERE company_id=%s "
            "AND cost_layer_id=ANY(%s::uuid[]) GROUP BY cost_layer_id",
            [company, [row["id"] for row in rows]],
        )
    }
    balances = []
    for row in rows:
        used = totals.get(row["id"], {"quantity": 0, "value": 0})
        quantity = row["quantity"] - used["quantity"]
        value = row["value_company"] - used["value"]
        if quantity < 0 or value < 0 or (quantity == 0 and value != 0):
            raise Conflict("STOCK_COST_LAYER_DRIFT", "Layer residual is invalid.")
        if quantity:
            balances.append(LayerBalance(row["id"], quantity, value))
    return balances


def record_layer_uses(
    company: uuid.UUID,
    movement: uuid.UUID,
    uses: list[LayerUse],
    *,
    historical_basis_id: uuid.UUID | None = None,
) -> None:
    for use in uses:
        _run(
            "INSERT INTO erp.inventory_cost_allocations(company_id,issue_movement_id,"
            "receipt_movement_id,opening_checkpoint_id,cost_layer_id,quantity,value_company,"
            "unit_cost_company,basis_layer_quantity,basis_layer_value,preceding_layer_value,"
            "historical_basis_id) "
            "SELECT company_id,%s,receipt_movement_id,opening_checkpoint_id,id,"
            "%s,%s,%s,%s,%s,%s,%s "
            "FROM erp.inventory_cost_layers WHERE company_id=%s AND id=%s",
            [
                movement,
                use.quantity,
                use.value,
                use.unit_cost,
                use.basis_quantity,
                use.basis_value,
                use.preceding_value,
                historical_basis_id,
                company,
                use.layer_id,
            ],
        )


def remaining_layers(layers: list[LayerBalance], uses: list[LayerUse]) -> list[LayerBalance]:
    consumption = {use.layer_id: use for use in uses}
    return [
        LayerBalance(
            layer.id,
            layer.quantity - consumption[layer.id].quantity,
            layer.value - consumption[layer.id].value,
        )
        for layer in layers
        if layer.quantity > consumption[layer.id].quantity
    ]
