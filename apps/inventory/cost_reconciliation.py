"""Bounded, single-snapshot inspection of immutable costing and live projections."""

import json
import uuid
from typing import Any

from apps.payments.services import _rows
from common.api.errors import Conflict

# Unretained positive origins are intentional lazy retention, not missing history.
# Opening checkpoints replace exact covered history; they never fabricate receipts.
SQL = r"""
WITH m AS MATERIALIZED (
 SELECT * FROM erp.stock_movements WHERE company_id=%s ORDER BY id LIMIT 10001
), cp AS MATERIALIZED (
 SELECT * FROM erp.inventory_cost_checkpoints WHERE company_id=%s ORDER BY id LIMIT 10001
), cm AS MATERIALIZED (
 SELECT * FROM erp.inventory_cost_checkpoint_movements WHERE company_id=%s
 ORDER BY checkpoint_id,movement_id LIMIT 10001
), l AS MATERIALIZED (
 SELECT * FROM erp.inventory_cost_layers WHERE company_id=%s ORDER BY id LIMIT 10001
), a AS MATERIALIZED (
 SELECT * FROM erp.inventory_cost_allocations WHERE company_id=%s ORDER BY id LIMIT 10001
), p AS MATERIALIZED (
 SELECT * FROM erp.inventory_positions WHERE company_id=%s ORDER BY id LIMIT 10001
), r AS MATERIALIZED (
 SELECT * FROM erp.inventory_reservations WHERE company_id=%s AND status='active'
 ORDER BY id LIMIT 10001
), b AS MATERIALIZED (
 SELECT * FROM erp.inventory_cost_basis_snapshots WHERE company_id=%s ORDER BY id LIMIT 10001
), h AS MATERIALIZED (
 SELECT * FROM erp.inventory_return_cost_basis WHERE company_id=%s ORDER BY id LIMIT 10001
), bounds AS (
 SELECT greatest((SELECT count(*) FROM m),(SELECT count(*) FROM cp),
 (SELECT count(*) FROM cm),(SELECT count(*) FROM l),(SELECT count(*) FROM a),
 (SELECT count(*) FROM p),(SELECT count(*) FROM r),(SELECT count(*) FROM b),
 (SELECT count(*) FROM h))>10000 exceeded
), covered AS (
 SELECT cp.id,count(cm.movement_id) n,coalesce(sum(m.quantity_delta),0) quantity,
 coalesce(sum(m.value_delta_company),0) value_company,
 coalesce(bool_and(cm.movement_id IS NULL OR (m.id IS NOT NULL
 AND m.warehouse_id=cp.warehouse_id AND m.item_id=cp.item_id
 AND m.lot_id IS NOT DISTINCT FROM cp.lot_id)),true) valid
 FROM cp LEFT JOIN cm ON cm.checkpoint_id=cp.id LEFT JOIN m ON m.id=cm.movement_id
 GROUP BY cp.id
), uncovered AS MATERIALIZED (
 SELECT m.*,cp.id checkpoint_id FROM m JOIN cp ON cp.warehouse_id=m.warehouse_id
 AND cp.item_id=m.item_id AND cp.lot_id IS NOT DISTINCT FROM m.lot_id
 WHERE NOT EXISTS(SELECT 1 FROM cm WHERE cm.checkpoint_id=cp.id AND cm.movement_id=m.id)
), used AS (
 SELECT cost_layer_id,sum(quantity) quantity,sum(value_company) value_company
 FROM a WHERE cost_layer_id IS NOT NULL GROUP BY cost_layer_id
), residuals AS MATERIALIZED (
 SELECT l.*,l.quantity-coalesce(u.quantity,0) remaining_quantity,
 l.value_company-coalesce(u.value_company,0) remaining_value_company,
 coalesce(cp.quantity,m.quantity_delta) origin_quantity,
 coalesce(cp.value_company,m.value_delta_company) origin_value_company,
 coalesce(cp.warehouse_id,m.warehouse_id) origin_warehouse_id,
 coalesce(cp.item_id,m.item_id) origin_item_id,
 CASE WHEN l.opening_checkpoint_id IS NOT NULL THEN cp.lot_id ELSE m.lot_id END origin_lot_id,
 CASE WHEN l.opening_checkpoint_id IS NOT NULL THEN cp.id IS NOT NULL
 ELSE EXISTS(SELECT 1 FROM uncovered x WHERE x.id=l.receipt_movement_id
 AND x.quantity_delta>0) END origin_valid
 FROM l LEFT JOIN used u ON u.cost_layer_id=l.id
 LEFT JOIN cp ON cp.id=l.opening_checkpoint_id LEFT JOIN m ON m.id=l.receipt_movement_id
), pending AS MATERIALIZED (
 SELECT cp.warehouse_id,cp.item_id,cp.lot_id,cp.quantity,cp.value_company FROM cp
 WHERE cp.quantity>0 AND NOT EXISTS(SELECT 1 FROM l WHERE l.opening_checkpoint_id=cp.id)
 UNION ALL SELECT m.warehouse_id,m.item_id,m.lot_id,m.quantity_delta,m.value_delta_company
 FROM uncovered m WHERE m.quantity_delta>0
 AND NOT EXISTS(SELECT 1 FROM l WHERE l.receipt_movement_id=m.id)
), pools AS (
 SELECT warehouse_id,item_id,lot_id,sum(quantity) quantity,sum(value_company) value_company
 FROM (SELECT warehouse_id,item_id,lot_id,remaining_quantity quantity,
 remaining_value_company value_company FROM residuals UNION ALL SELECT * FROM pending) x
 GROUP BY warehouse_id,item_id,lot_id
), stock AS (
 SELECT warehouse_id,item_id,lot_id,sum(quantity_delta) quantity,
 sum(value_delta_company) value_company FROM m GROUP BY warehouse_id,item_id,lot_id
), reserved AS (
 SELECT warehouse_id,item_id,lot_id,sum(quantity) quantity FROM r
 GROUP BY warehouse_id,item_id,lot_id
), scope_keys AS (
 SELECT warehouse_id,item_id,lot_id FROM stock UNION
 SELECT warehouse_id,item_id,lot_id FROM reserved UNION
 SELECT warehouse_id,item_id,lot_id FROM p UNION
 SELECT warehouse_id,item_id,lot_id FROM cp UNION
 SELECT warehouse_id,item_id,lot_id FROM l
), scopes AS MATERIALIZED (
 SELECT x.*,cp.id checkpoint_id,p.id position_id,
 coalesce(s.quantity,0) ledger_quantity,coalesce(s.value_company,0) ledger_value_company,
 coalesce(rv.quantity,0) ledger_reserved_quantity,
 p.on_hand_quantity position_quantity,p.value_company position_value_company,
 p.reserved_quantity position_reserved_quantity,
 coalesce(pool.quantity,0) layer_quantity,coalesce(pool.value_company,0) layer_value_company
 FROM scope_keys x LEFT JOIN stock s ON s.warehouse_id=x.warehouse_id
 AND s.item_id=x.item_id AND s.lot_id IS NOT DISTINCT FROM x.lot_id
 LEFT JOIN reserved rv ON rv.warehouse_id=x.warehouse_id AND rv.item_id=x.item_id
 AND rv.lot_id IS NOT DISTINCT FROM x.lot_id
 LEFT JOIN p ON p.warehouse_id=x.warehouse_id AND p.item_id=x.item_id
 AND p.lot_id IS NOT DISTINCT FROM x.lot_id
 LEFT JOIN cp ON cp.warehouse_id=x.warehouse_id AND cp.item_id=x.item_id
 AND cp.lot_id IS NOT DISTINCT FROM x.lot_id
 LEFT JOIN pools pool ON pool.warehouse_id=x.warehouse_id AND pool.item_id=x.item_id
 AND pool.lot_id IS NOT DISTINCT FROM x.lot_id
), issue_totals AS (
 SELECT issue_movement_id,sum(quantity) quantity,sum(value_company) value_company,
 bool_and(cost_layer_id IS NOT NULL) retained FROM a GROUP BY issue_movement_id
), diagnostics AS MATERIALIZED (
 SELECT 'checkpoint' object_type,cp.id object_id,'CHECKPOINT_MEMBERSHIP_DIFFERENCE' code
 FROM cp JOIN covered c ON c.id=cp.id WHERE c.n<>cp.movement_count OR c.quantity<>cp.quantity
 OR c.value_company<>cp.value_company OR NOT c.valid
 UNION ALL SELECT 'layer',id,'LAYER_ORIGIN_DIFFERENCE' FROM residuals
 WHERE NOT origin_valid OR quantity IS DISTINCT FROM origin_quantity
 OR value_company IS DISTINCT FROM origin_value_company
 OR warehouse_id IS DISTINCT FROM origin_warehouse_id OR item_id IS DISTINCT FROM origin_item_id
 OR lot_id IS DISTINCT FROM origin_lot_id
 UNION ALL SELECT 'layer',id,'LAYER_RESIDUAL_INVALID' FROM residuals
 WHERE remaining_quantity<0 OR remaining_value_company<0
 OR (remaining_quantity=0 AND remaining_value_company<>0)
 UNION ALL SELECT 'allocation',a.id,'ALLOCATION_ORIGIN_DIFFERENCE' FROM a
 LEFT JOIN l ON l.id=a.cost_layer_id LEFT JOIN m ON m.id=a.issue_movement_id
 WHERE a.cost_layer_id IS NOT NULL AND (l.id IS NULL OR m.id IS NULL OR m.quantity_delta>=0
 OR a.receipt_movement_id IS DISTINCT FROM l.receipt_movement_id
 OR a.opening_checkpoint_id IS DISTINCT FROM l.opening_checkpoint_id
 OR m.warehouse_id IS DISTINCT FROM l.warehouse_id OR m.item_id IS DISTINCT FROM l.item_id
 OR m.lot_id IS DISTINCT FROM l.lot_id)
 UNION ALL SELECT 'movement',m.id,'ISSUE_COSTING_DIFFERENCE' FROM uncovered m
 LEFT JOIN issue_totals t ON t.issue_movement_id=m.id
 LEFT JOIN b ON b.movement_id=m.id LEFT JOIN h ON h.movement_id=m.id
 WHERE m.quantity_delta<0 AND (num_nonnulls(b.id,h.id)<>1
 OR coalesce(b.issue_quantity,h.issue_quantity) IS DISTINCT FROM -m.quantity_delta
 OR coalesce(b.issue_value_company,h.issue_value_company) IS DISTINCT FROM -m.value_delta_company
 OR t.quantity IS DISTINCT FROM -m.quantity_delta
 OR t.value_company IS DISTINCT FROM -m.value_delta_company OR t.retained IS NOT TRUE)
 UNION ALL SELECT 'scope',checkpoint_id,'LAYER_LEDGER_DIFFERENCE' FROM scopes
 WHERE checkpoint_id IS NOT NULL AND (layer_quantity<>ledger_quantity
 OR layer_value_company<>ledger_value_company)
 UNION ALL SELECT 'scope',coalesce(position_id,checkpoint_id),'STOCK_LEDGER_INVALID' FROM scopes
 WHERE ledger_quantity<0 OR ledger_value_company<0 OR ledger_reserved_quantity<0
 OR ledger_reserved_quantity>ledger_quantity OR (ledger_quantity=0 AND ledger_value_company<>0)
), projection_differences AS MATERIALIZED (
 SELECT * FROM scopes WHERE position_id IS NULL OR position_quantity<>ledger_quantity
 OR position_value_company<>ledger_value_company
 OR position_reserved_quantity<>ledger_reserved_quantity
), summary AS (
 SELECT (SELECT count(*) FROM diagnostics) diagnostic_count,
 (SELECT count(*) FROM projection_differences) projection_difference_count,
 (SELECT count(*) FROM scopes) scope_count,
 (SELECT count(*) FROM cp) adopted_scope_count,
 (SELECT count(*) FROM scopes WHERE checkpoint_id IS NULL
 AND (ledger_quantity<>0 OR ledger_value_company<>0 OR EXISTS(SELECT 1 FROM m
 WHERE m.warehouse_id=scopes.warehouse_id AND m.item_id=scopes.item_id
 AND m.lot_id IS NOT DISTINCT FROM scopes.lot_id))) unadopted_scope_count
)
SELECT (SELECT exceeded FROM bounds) exceeded,jsonb_build_object(
 'matches',diagnostic_count=0 AND projection_difference_count=0 AND unadopted_scope_count=0,
 'cost_history_matches',diagnostic_count=0,'projection_matches',projection_difference_count=0,
 'adoption_complete',unadopted_scope_count=0,'scope_count',scope_count,
 'adopted_scope_count',adopted_scope_count,'unadopted_scope_count',unadopted_scope_count,
 'retained_layer_count',(SELECT count(*) FROM l),
 'pending_origin_count',(SELECT count(*) FROM pending),
 'diagnostic_count',diagnostic_count,'projection_difference_count',projection_difference_count,
 'truncated',greatest(scope_count,diagnostic_count,projection_difference_count)>%s,
 'scopes',coalesce((SELECT jsonb_agg(to_jsonb(x)) FROM (
 SELECT warehouse_id,item_id,lot_id,checkpoint_id,ledger_quantity::text,
 ledger_value_company::text,ledger_reserved_quantity::text,position_quantity::text,
 position_value_company::text,position_reserved_quantity::text,
 CASE WHEN checkpoint_id IS NOT NULL THEN layer_quantity::text END layer_quantity,
 CASE WHEN checkpoint_id IS NOT NULL THEN layer_value_company::text END layer_value_company
 FROM scopes ORDER BY warehouse_id,item_id,lot_id NULLS FIRST LIMIT %s) x),'[]'::jsonb),
 'diagnostics',coalesce((SELECT jsonb_agg(to_jsonb(x)) FROM (
 SELECT * FROM diagnostics ORDER BY object_type,object_id,code LIMIT %s) x),'[]'::jsonb),
 'projection_differences',coalesce((SELECT jsonb_agg(to_jsonb(x)) FROM (
 SELECT warehouse_id,item_id,lot_id,ledger_quantity::text,ledger_value_company::text,
 ledger_reserved_quantity::text,position_quantity::text,position_value_company::text,
 position_reserved_quantity::text FROM projection_differences
 ORDER BY warehouse_id,item_id,lot_id NULLS FIRST LIMIT %s) x),'[]'::jsonb)
) result FROM summary
"""


def cost_reconciliation(company: uuid.UUID, *, limit: int = 200) -> dict[str, Any]:
    if not 1 <= limit <= 200:
        raise ValueError("Cost reconciliation detail limit must be between 1 and 200.")
    row = _rows(SQL, [company] * 9 + [limit] * 4)[0]
    if row["exceeded"]:
        raise Conflict(
            "INVENTORY_REPORT_JOB_REQUIRED", "Cost reconciliation exceeds the interactive limit."
        )
    raw = row["result"]
    result: dict[str, Any] = json.loads(raw) if isinstance(raw, str) else raw
    return result
