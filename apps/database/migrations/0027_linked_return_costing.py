from django.db import migrations

SQL = r"""
CREATE TABLE erp.inventory_return_cost_basis(
 id uuid PRIMARY KEY DEFAULT uuidv7(),company_id uuid NOT NULL,
 movement_id uuid NOT NULL,return_stock_action_id uuid NOT NULL,return_line_id uuid NOT NULL,
 currency_code char(3) NOT NULL REFERENCES erp.currencies(code),
 currency_precision integer NOT NULL CHECK(currency_precision BETWEEN 0 AND 6),
 scope_quantity numeric(20,6) NOT NULL CHECK(scope_quantity>0),
 scope_value_company numeric(20,6) NOT NULL CHECK(scope_value_company>=0),
 reserved_quantity numeric(20,6) NOT NULL CHECK(reserved_quantity>=0),
 basis_quantity numeric(20,6) NOT NULL CHECK(basis_quantity>0),
 basis_value_company numeric(20,6) NOT NULL CHECK(basis_value_company>=0),
 issue_quantity numeric(20,6) NOT NULL CHECK(issue_quantity>0),
 issue_value_company numeric(20,6) NOT NULL CHECK(issue_value_company>=0),
 unit_cost_company numeric(20,6) NOT NULL CHECK(unit_cost_company>=0),
 allocation_seal_xid xid8 NOT NULL DEFAULT pg_current_xact_id(),
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 CHECK(issue_quantity<=basis_quantity AND issue_quantity<=scope_quantity-reserved_quantity),
 CHECK(basis_quantity<=scope_quantity AND basis_value_company<=scope_value_company),
 UNIQUE(company_id,id),UNIQUE(company_id,movement_id),
 FOREIGN KEY(company_id,movement_id) REFERENCES erp.stock_movements(company_id,id),
 FOREIGN KEY(company_id,return_stock_action_id)
 REFERENCES erp.sales_return_stock_actions(company_id,id),
 FOREIGN KEY(company_id,return_line_id) REFERENCES erp.sales_return_lines(company_id,id)
);
ALTER TABLE erp.inventory_return_cost_basis ENABLE ROW LEVEL SECURITY;
CREATE POLICY company_scope ON erp.inventory_return_cost_basis
 USING(EXISTS(SELECT 1 FROM erp.companies c WHERE c.id=company_id
 AND c.tenant_id=erp.current_tenant_id()))
 WITH CHECK(EXISTS(SELECT 1 FROM erp.companies c WHERE c.id=company_id
 AND c.tenant_id=erp.current_tenant_id()));
ALTER TABLE erp.inventory_cost_allocations ADD COLUMN historical_basis_id uuid,
 ADD CONSTRAINT fk_cost_allocation_history FOREIGN KEY(company_id,historical_basis_id)
 REFERENCES erp.inventory_return_cost_basis(company_id,id);
CREATE INDEX ix_cost_allocation_historical_basis
 ON erp.inventory_cost_allocations(company_id,historical_basis_id)
 WHERE historical_basis_id IS NOT NULL;
CREATE FUNCTION erp.return_layer_claim(p_company uuid,p_layer uuid,p_line uuid)
 RETURNS TABLE(quantity numeric,value_company numeric) LANGUAGE plpgsql
SET search_path=pg_catalog,erp AS $$
DECLARE l record;q numeric:=0;v numeric:=0;uq numeric:=0;uv numeric:=0;
BEGIN
 SELECT * INTO l FROM erp.inventory_cost_layers WHERE company_id=p_company AND id=p_layer;
 IF NOT FOUND THEN RETURN QUERY SELECT 0::numeric,0::numeric; RETURN; END IF;
 IF l.opening_checkpoint_id IS NOT NULL THEN
 -- Unlinked pre-checkpoint losses cannot be attributed to individual returns.
 IF EXISTS(SELECT 1 FROM erp.inventory_cost_checkpoint_movements c
 JOIN erp.stock_movements m ON m.company_id=c.company_id AND m.id=c.movement_id
 WHERE c.company_id=p_company AND c.checkpoint_id=l.opening_checkpoint_id
 AND m.quantity_delta<0 AND (m.source_type IS NULL OR m.source_type NOT IN
 ('sales_return','return_stock_action'))) THEN
 RETURN QUERY SELECT 0::numeric,0::numeric; RETURN; END IF;
 -- An average issue destroys per-return ownership of the aggregate opening pool.
 IF EXISTS(SELECT 1 FROM erp.inventory_cost_allocations WHERE company_id=p_company
 AND cost_layer_id=p_layer AND historical_basis_id IS NULL) THEN
 RETURN QUERY SELECT 0::numeric,0::numeric; RETURN; END IF;
 SELECT coalesce(sum(m.quantity_delta),0),coalesce(sum(m.value_delta_company),0) INTO q,v
 FROM erp.inventory_cost_checkpoint_movements c JOIN erp.stock_movements m
 ON m.company_id=c.company_id AND m.id=c.movement_id WHERE c.company_id=p_company
 AND c.checkpoint_id=l.opening_checkpoint_id AND m.source_line_id=p_line
 AND m.source_type IN ('sales_return','return_stock_action');
 SELECT coalesce(sum(a.quantity),0),coalesce(sum(a.value_company),0) INTO uq,uv
 FROM erp.inventory_cost_allocations a JOIN erp.inventory_return_cost_basis b
 ON b.company_id=a.company_id AND b.id=a.historical_basis_id
 WHERE a.company_id=p_company AND a.cost_layer_id=p_layer AND b.return_line_id=p_line;
 ELSE
 SELECT quantity_delta,value_delta_company INTO q,v FROM erp.stock_movements
 WHERE company_id=p_company AND id=l.receipt_movement_id AND source_line_id=p_line
 AND source_type IN ('sales_return','return_stock_action');
 IF NOT FOUND THEN RETURN QUERY SELECT 0::numeric,0::numeric; RETURN; END IF;
 SELECT coalesce(sum(a.quantity),0),coalesce(sum(a.value_company),0) INTO uq,uv
 FROM erp.inventory_cost_allocations a WHERE a.company_id=p_company AND a.cost_layer_id=p_layer;
 END IF;
 RETURN QUERY SELECT q-uq,v-uv;
END $$;
CREATE FUNCTION erp.guard_return_cost_basis() RETURNS trigger LANGUAGE plpgsql
SET search_path=pg_catalog,erp AS $$
DECLARE m record;a record;p record;h record;v numeric;
BEGIN
 IF TG_OP<>'INSERT' THEN RAISE EXCEPTION 'Return cost history is immutable'; END IF;
 IF NEW.allocation_seal_xid IS DISTINCT FROM pg_current_xact_id() THEN
 RAISE EXCEPTION 'Return cost seal must match owning transaction'; END IF;
 SELECT * INTO m FROM erp.stock_movements WHERE company_id=NEW.company_id AND id=NEW.movement_id;
 SELECT * INTO a FROM erp.sales_return_stock_actions
 WHERE company_id=NEW.company_id AND id=NEW.return_stock_action_id;
 SELECT * INTO p FROM erp.inventory_positions WHERE company_id=NEW.company_id
 AND warehouse_id=m.warehouse_id AND item_id=m.item_id AND lot_id IS NULL FOR UPDATE;
 SELECT coalesce(sum(quantity_delta),0) q,coalesce(sum(value_delta_company),0) v INTO h
 FROM erp.stock_movements WHERE company_id=NEW.company_id AND warehouse_id=m.warehouse_id
 AND item_id=m.item_id AND lot_id IS NULL AND source_line_id=NEW.return_line_id
 AND source_type IN ('sales_return','return_stock_action');
 v:=CASE WHEN NEW.issue_quantity=NEW.basis_quantity THEN NEW.basis_value_company ELSE
 round(NEW.basis_value_company*NEW.issue_quantity/NEW.basis_quantity,6) END;
 IF m.source_type IS DISTINCT FROM 'return_stock_action' OR m.source_id IS DISTINCT FROM a.id
 OR m.source_line_id IS DISTINCT FROM NEW.return_line_id OR m.lot_id IS NOT NULL
 OR a.sales_return_line_id IS DISTINCT FROM NEW.return_line_id
 OR m.item_id IS DISTINCT FROM (SELECT s.item_id FROM erp.sales_return_lines r
 JOIN erp.sales_invoice_lines s ON s.company_id=r.company_id AND s.id=r.sales_invoice_line_id
 WHERE r.company_id=NEW.company_id AND r.id=NEW.return_line_id)
 OR a.from_warehouse_id IS DISTINCT FROM m.warehouse_id
 OR a.quantity IS DISTINCT FROM NEW.issue_quantity OR a.historical_cost IS DISTINCT FROM v
 OR m.quantity_delta IS DISTINCT FROM -NEW.issue_quantity
 OR m.value_delta_company IS DISTINCT FROM -NEW.issue_value_company
 OR NEW.issue_value_company<>v OR NEW.unit_cost_company<>round(v/NEW.issue_quantity,6)
 OR m.unit_cost_company IS DISTINCT FROM NEW.unit_cost_company
 OR NEW.basis_quantity<>h.q+NEW.issue_quantity
 OR NEW.basis_value_company<>h.v+NEW.issue_value_company
 OR NEW.scope_quantity IS DISTINCT FROM p.on_hand_quantity+NEW.issue_quantity
 OR NEW.scope_value_company IS DISTINCT FROM p.value_company+NEW.issue_value_company
 OR NEW.reserved_quantity IS DISTINCT FROM p.reserved_quantity
 OR NEW.currency_code IS DISTINCT FROM (SELECT functional_currency FROM erp.companies
 WHERE id=NEW.company_id) OR NEW.currency_precision IS DISTINCT FROM
 (SELECT minor_units FROM erp.currencies WHERE code=NEW.currency_code)
 OR round(v,NEW.currency_precision)<>v THEN
 RAISE EXCEPTION 'Return historical basis does not reconcile'; END IF;
 IF NOT EXISTS(SELECT 1 FROM erp.inventory_cost_checkpoints WHERE company_id=NEW.company_id
 AND warehouse_id=m.warehouse_id AND item_id=m.item_id AND lot_id IS NULL) THEN
 RAISE EXCEPTION 'Historical allocations require an adopted stock scope'; END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER trg_return_cost_basis_history BEFORE INSERT OR UPDATE OR DELETE
 ON erp.inventory_return_cost_basis FOR EACH ROW EXECUTE FUNCTION erp.guard_return_cost_basis();
CREATE TRIGGER trg_return_cost_basis_audit AFTER INSERT ON erp.inventory_return_cost_basis
 FOR EACH ROW EXECUTE FUNCTION erp.audit_inventory_command();
DROP TRIGGER trg_cost_allocation_history ON erp.inventory_cost_allocations;
CREATE TRIGGER trg_cost_allocation_history BEFORE UPDATE OR DELETE
 ON erp.inventory_cost_allocations FOR EACH ROW
 EXECUTE FUNCTION erp.guard_inventory_cost_allocation();
CREATE TRIGGER trg_average_cost_allocation_insert BEFORE INSERT ON erp.inventory_cost_allocations
 FOR EACH ROW WHEN(NEW.historical_basis_id IS NULL)
 EXECUTE FUNCTION erp.guard_inventory_cost_allocation();
CREATE FUNCTION erp.guard_return_cost_allocation() RETURNS trigger LANGUAGE plpgsql
SET search_path=pg_catalog,erp AS $$
DECLARE l record;b record;m record;u record;c record;v numeric;
BEGIN
 SELECT * INTO l FROM erp.inventory_cost_layers
 WHERE company_id=NEW.company_id AND id=NEW.cost_layer_id FOR UPDATE;
 SELECT * INTO b FROM erp.inventory_return_cost_basis
 WHERE company_id=NEW.company_id AND id=NEW.historical_basis_id;
 IF NOT FOUND OR b.allocation_seal_xid IS DISTINCT FROM pg_current_xact_id() THEN
 RAISE EXCEPTION 'Return allocation requires an unsealed historical basis'; END IF;
 SELECT * INTO m FROM erp.stock_movements WHERE company_id=NEW.company_id AND id=b.movement_id;
 SELECT coalesce(sum(quantity),0) q,coalesce(sum(value_company),0) v INTO u
 FROM erp.inventory_cost_allocations WHERE company_id=NEW.company_id AND cost_layer_id=l.id;
 SELECT * INTO c FROM erp.return_layer_claim(NEW.company_id,l.id,b.return_line_id);
 v:=CASE WHEN b.basis_value_company=0 THEN 0 ELSE
 round((NEW.preceding_layer_value+NEW.basis_layer_value)*b.issue_value_company/
 b.basis_value_company,6)-round(NEW.preceding_layer_value*b.issue_value_company/
 b.basis_value_company,6) END;
 IF NEW.issue_movement_id IS DISTINCT FROM b.movement_id
 OR l.warehouse_id IS DISTINCT FROM m.warehouse_id OR l.item_id IS DISTINCT FROM m.item_id
 OR l.lot_id IS DISTINCT FROM m.lot_id
 OR l.receipt_movement_id IS DISTINCT FROM NEW.receipt_movement_id
 OR l.opening_checkpoint_id IS DISTINCT FROM NEW.opening_checkpoint_id
 OR NEW.basis_layer_quantity IS DISTINCT FROM c.quantity
 OR NEW.basis_layer_value IS DISTINCT FROM c.value_company
 OR NEW.quantity<>c.quantity*b.issue_quantity/b.basis_quantity
 OR NEW.quantity>l.quantity-u.q OR NEW.value_company>l.value_company-u.v
 OR NEW.value_company<>v OR NEW.unit_cost_company<>round(v/NEW.quantity,6)
 OR (NEW.quantity=l.quantity-u.q AND NEW.value_company<>l.value_company-u.v) THEN
 RAISE EXCEPTION 'Return allocation provenance or capacity mismatch'; END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER trg_return_cost_allocation_insert BEFORE INSERT ON erp.inventory_cost_allocations
 FOR EACH ROW WHEN(NEW.historical_basis_id IS NOT NULL)
 EXECUTE FUNCTION erp.guard_return_cost_allocation();
DROP TRIGGER trg_adopted_stock_issue_complete ON erp.stock_movements;
CREATE CONSTRAINT TRIGGER trg_adopted_stock_issue_complete
 AFTER INSERT ON erp.stock_movements DEFERRABLE INITIALLY DEFERRED
 FOR EACH ROW WHEN(NEW.source_type IS DISTINCT FROM 'return_stock_action')
 EXECUTE FUNCTION erp.check_inventory_layer_issue_complete();
CREATE FUNCTION erp.check_return_cost_complete() RETURNS trigger LANGUAGE plpgsql
SET search_path=pg_catalog,erp AS $$
DECLARE m record;b record;a record;v_id uuid;
BEGIN
 IF TG_TABLE_NAME='stock_movements' THEN v_id:=NEW.id; ELSE v_id:=NEW.movement_id; END IF;
 SELECT * INTO m FROM erp.stock_movements WHERE company_id=NEW.company_id AND id=v_id;
 IF NOT EXISTS(SELECT 1 FROM erp.inventory_cost_checkpoints WHERE company_id=m.company_id
 AND warehouse_id=m.warehouse_id AND item_id=m.item_id
 AND lot_id IS NOT DISTINCT FROM m.lot_id) THEN RETURN NULL; END IF;
 SELECT * INTO b FROM erp.inventory_return_cost_basis
 WHERE company_id=m.company_id AND movement_id=m.id;
 IF NOT FOUND THEN RAISE EXCEPTION 'Adopted return disposal requires historical cost basis'; END IF;
 IF EXISTS(SELECT 1 FROM erp.inventory_cost_basis_snapshots
 WHERE company_id=m.company_id AND movement_id=m.id) THEN
 RAISE EXCEPTION 'Historical disposal must not also use average costing'; END IF;
 SELECT coalesce(sum(quantity),0) q,coalesce(sum(value_company),0) v,
 coalesce(sum(basis_layer_quantity),0) bq,coalesce(sum(basis_layer_value),0) bv INTO a
 FROM erp.inventory_cost_allocations WHERE company_id=m.company_id AND issue_movement_id=m.id;
 IF a.q<>b.issue_quantity OR a.v<>b.issue_value_company
 OR a.bq<>b.basis_quantity OR a.bv<>b.basis_value_company THEN
 RAISE EXCEPTION 'Return disposal requires complete historical allocations'; END IF;
 IF EXISTS(SELECT 1 FROM (
 SELECT historical_basis_id,preceding_layer_value,coalesce(sum(basis_layer_value) OVER
 (ORDER BY cost_layer_id ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING),0) expected
 FROM erp.inventory_cost_allocations WHERE company_id=m.company_id AND issue_movement_id=m.id
 ) x WHERE historical_basis_id IS DISTINCT FROM b.id OR preceding_layer_value<>expected) THEN
 RAISE EXCEPTION 'Return allocation ordering or basis mismatch'; END IF;
 RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER trg_return_stock_cost_complete AFTER INSERT ON erp.stock_movements
 DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
 WHEN(NEW.source_type='return_stock_action' AND NEW.quantity_delta<0)
 EXECUTE FUNCTION erp.check_return_cost_complete();
CREATE CONSTRAINT TRIGGER trg_return_cost_basis_complete
 AFTER INSERT ON erp.inventory_return_cost_basis
 DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION erp.check_return_cost_complete();
"""

REVERSE = r"""
DO $$ BEGIN
 IF EXISTS(SELECT 1 FROM erp.inventory_return_cost_basis) THEN
 RAISE EXCEPTION 'Cannot reverse retained return costing history'; END IF;
END $$;
DROP TRIGGER trg_return_stock_cost_complete ON erp.stock_movements;
DROP TRIGGER trg_adopted_stock_issue_complete ON erp.stock_movements;
CREATE CONSTRAINT TRIGGER trg_adopted_stock_issue_complete AFTER INSERT ON erp.stock_movements
 DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
 EXECUTE FUNCTION erp.check_inventory_layer_issue_complete();
DROP TRIGGER trg_cost_allocation_history ON erp.inventory_cost_allocations;
DROP TRIGGER trg_average_cost_allocation_insert ON erp.inventory_cost_allocations;
DROP TRIGGER trg_return_cost_allocation_insert ON erp.inventory_cost_allocations;
CREATE TRIGGER trg_cost_allocation_history BEFORE INSERT OR UPDATE OR DELETE
 ON erp.inventory_cost_allocations FOR EACH ROW
 EXECUTE FUNCTION erp.guard_inventory_cost_allocation();
DROP FUNCTION erp.guard_return_cost_allocation();
DROP INDEX erp.ix_cost_allocation_historical_basis;
ALTER TABLE erp.inventory_cost_allocations DROP COLUMN historical_basis_id;
DROP TABLE erp.inventory_return_cost_basis;
DROP FUNCTION erp.check_return_cost_complete();
DROP FUNCTION erp.guard_return_cost_basis();
DROP FUNCTION erp.return_layer_claim(uuid,uuid,uuid);
"""


class Migration(migrations.Migration):
    dependencies = [("database", "0026_cost_allocation_transaction_seal")]
    operations = [migrations.RunSQL(SQL, REVERSE)]
