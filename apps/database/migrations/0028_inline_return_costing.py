from importlib import import_module

from django.db import migrations

# Preserve the already-reviewed action checks while extending the same immutable
# ledger to an inline write-off, which has a return line but no separate action.
prior = import_module("apps.database.migrations.0027_linked_return_costing")
start = prior.SQL.index("CREATE FUNCTION erp.guard_return_cost_basis()")
end = prior.SQL.index("CREATE TRIGGER trg_return_cost_basis_history", start)
original_guard = prior.SQL[start:end]
inline_guard = original_guard.replace("CREATE FUNCTION", "CREATE OR REPLACE FUNCTION", 1)
inline_guard = inline_guard.replace(
    "DECLARE m record;a record;p record;h record;v numeric;",
    "DECLARE m record;a record;p record;h record;inline_line record;v numeric;",
)
inline_guard = inline_guard.replace(
    " IF m.source_type IS DISTINCT FROM 'return_stock_action' OR m.source_id IS DISTINCT FROM a.id",
    """ IF NEW.return_stock_action_id IS NULL THEN
 SELECT l.*,s.item_id INTO inline_line FROM erp.sales_return_lines l
 JOIN erp.sales_invoice_lines s ON s.company_id=l.company_id AND s.id=l.sales_invoice_line_id
 WHERE l.company_id=NEW.company_id AND l.id=NEW.return_line_id;
 IF NOT FOUND OR inline_line.disposition IS DISTINCT FROM 'write_off'
 OR m.source_type IS DISTINCT FROM 'sales_return'
 OR m.source_id IS DISTINCT FROM inline_line.sales_return_id
 OR m.source_line_id IS DISTINCT FROM inline_line.id
 OR m.warehouse_id IS DISTINCT FROM inline_line.warehouse_id
 OR m.item_id IS DISTINCT FROM inline_line.item_id OR m.lot_id IS NOT NULL
 OR NEW.basis_quantity IS DISTINCT FROM inline_line.quantity
 OR NEW.issue_quantity<>inline_line.quantity
 OR NEW.issue_value_company IS DISTINCT FROM inline_line.historical_cost
 OR NEW.basis_value_company IS DISTINCT FROM inline_line.historical_cost
 OR m.quantity_delta IS DISTINCT FROM -NEW.issue_quantity
 OR m.value_delta_company IS DISTINCT FROM -NEW.issue_value_company
 OR NEW.unit_cost_company<>round(NEW.issue_value_company/NEW.issue_quantity,6)
 OR m.unit_cost_company IS DISTINCT FROM NEW.unit_cost_company
 OR NOT EXISTS(SELECT 1 FROM erp.stock_movements rm WHERE rm.company_id=m.company_id
 AND rm.source_type='sales_return' AND rm.source_id=m.source_id
 AND rm.source_line_id=inline_line.id AND rm.quantity_delta=inline_line.quantity
 AND rm.value_delta_company=inline_line.historical_cost
 AND rm.warehouse_id=m.warehouse_id AND rm.item_id=m.item_id AND rm.lot_id IS NULL)
 THEN RAISE EXCEPTION 'Inline return write-off does not reconcile'; END IF;
 ELSE
 IF m.source_type IS DISTINCT FROM 'return_stock_action' OR m.source_id IS DISTINCT FROM a.id""",
)
inline_guard = inline_guard.replace(
    " OR NEW.scope_quantity IS DISTINCT FROM p.on_hand_quantity+NEW.issue_quantity",
    """ THEN RAISE EXCEPTION 'Return historical basis does not reconcile'; END IF;
 END IF;
 IF NEW.scope_quantity IS DISTINCT FROM p.on_hand_quantity+NEW.issue_quantity""",
)

SQL = (
    """
ALTER TABLE erp.inventory_return_cost_basis ALTER COLUMN return_stock_action_id DROP NOT NULL;
"""
    + inline_guard
    + """
DROP TRIGGER trg_adopted_stock_issue_complete ON erp.stock_movements;
CREATE CONSTRAINT TRIGGER trg_adopted_stock_issue_complete AFTER INSERT ON erp.stock_movements
 DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
 WHEN(NEW.source_type IS DISTINCT FROM 'return_stock_action'
 AND NEW.source_type IS DISTINCT FROM 'sales_return')
 EXECUTE FUNCTION erp.check_inventory_layer_issue_complete();
CREATE CONSTRAINT TRIGGER trg_inline_return_cost_complete AFTER INSERT ON erp.stock_movements
 DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
 WHEN(NEW.source_type='sales_return' AND NEW.quantity_delta<0)
 EXECUTE FUNCTION erp.check_return_cost_complete();
CREATE FUNCTION erp.check_inline_return_stock() RETURNS trigger LANGUAGE plpgsql
SET search_path=pg_catalog,erp AS $$
DECLARE ret record;line_fact record;q numeric;v numeric;n integer;
BEGIN
 IF TG_TABLE_NAME='sales_returns' THEN
 SELECT * INTO ret FROM erp.sales_returns WHERE company_id=NEW.company_id AND id=NEW.id;
 ELSE
 SELECT * INTO ret FROM erp.sales_returns WHERE company_id=NEW.company_id AND id=NEW.source_id;
 END IF;
 IF NOT FOUND OR ret.status<>'posted' THEN RETURN NULL; END IF;
 FOR line_fact IN SELECT * FROM erp.sales_return_lines WHERE company_id=ret.company_id
 AND sales_return_id=ret.id AND disposition='write_off' LOOP
 SELECT count(*),coalesce(sum(quantity_delta),0),coalesce(sum(value_delta_company),0)
 INTO n,q,v FROM erp.stock_movements WHERE company_id=ret.company_id
 AND source_type='sales_return' AND source_id=ret.id AND source_line_id=line_fact.id
 AND quantity_delta<0;
 IF n<>1 OR q IS DISTINCT FROM -line_fact.quantity
 OR v IS DISTINCT FROM -line_fact.historical_cost OR line_fact.loss_account_id IS NULL THEN
 RAISE EXCEPTION 'Inline return loss must match final inspection and historical cost'; END IF;
 SELECT count(*),coalesce(sum(quantity_delta),0),coalesce(sum(value_delta_company),0)
 INTO n,q,v FROM erp.stock_movements WHERE company_id=ret.company_id
 AND source_type='sales_return' AND source_id=ret.id AND source_line_id=line_fact.id
 AND quantity_delta>0;
 IF n<>1 OR q IS DISTINCT FROM line_fact.quantity
 OR v IS DISTINCT FROM line_fact.historical_cost THEN
 RAISE EXCEPTION 'Inline return loss requires its matching receipt'; END IF;
 END LOOP;
 IF EXISTS(SELECT 1 FROM erp.stock_movements sm LEFT JOIN erp.sales_return_lines sl
 ON sl.company_id=sm.company_id AND sl.id=sm.source_line_id
 WHERE sm.company_id=ret.company_id AND sm.source_type='sales_return' AND sm.source_id=ret.id
 AND sm.quantity_delta<0 AND (sl.disposition IS DISTINCT FROM 'write_off'
 OR sm.journal_entry_id IS DISTINCT FROM ret.journal_entry_id
 OR sm.warehouse_id IS DISTINCT FROM sl.warehouse_id)) THEN
 RAISE EXCEPTION 'Inline return loss origin mismatch'; END IF;
 RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER trg_inline_return_stock_posted AFTER INSERT OR UPDATE
 ON erp.sales_returns DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
 EXECUTE FUNCTION erp.check_inline_return_stock();
CREATE CONSTRAINT TRIGGER trg_inline_return_stock_movement AFTER INSERT
 ON erp.stock_movements DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
 WHEN(NEW.source_type='sales_return' AND NEW.quantity_delta<0)
 EXECUTE FUNCTION erp.check_inline_return_stock();
"""
)

REVERSE = """
DO $$ BEGIN
 IF EXISTS(SELECT 1 FROM erp.inventory_return_cost_basis WHERE return_stock_action_id IS NULL) THEN
 RAISE EXCEPTION 'Cannot reverse retained inline return costing history'; END IF;
END $$;
DROP TRIGGER trg_inline_return_cost_complete ON erp.stock_movements;
DROP TRIGGER trg_inline_return_stock_posted ON erp.sales_returns;
DROP TRIGGER trg_inline_return_stock_movement ON erp.stock_movements;
DROP FUNCTION erp.check_inline_return_stock();
DROP TRIGGER trg_adopted_stock_issue_complete ON erp.stock_movements;
CREATE CONSTRAINT TRIGGER trg_adopted_stock_issue_complete AFTER INSERT ON erp.stock_movements
 DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
 WHEN(NEW.source_type IS DISTINCT FROM 'return_stock_action')
 EXECUTE FUNCTION erp.check_inventory_layer_issue_complete();
ALTER TABLE erp.inventory_return_cost_basis ALTER COLUMN return_stock_action_id SET NOT NULL;
""" + original_guard.replace("CREATE FUNCTION", "CREATE OR REPLACE FUNCTION", 1)


class Migration(migrations.Migration):
    dependencies = [("database", "0027_linked_return_costing")]
    operations = [migrations.RunSQL(SQL, REVERSE)]
