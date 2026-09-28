from django.db import migrations

SQL = r"""
CREATE TABLE erp.inventory_cost_layers(
 id uuid PRIMARY KEY DEFAULT uuidv7(),company_id uuid NOT NULL,
 warehouse_id uuid NOT NULL,item_id uuid NOT NULL,lot_id uuid,
 receipt_movement_id uuid,opening_checkpoint_id uuid,
 quantity numeric(20,6) NOT NULL CHECK(quantity>0),
 value_company numeric(20,6) NOT NULL CHECK(value_company>=0),
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 CHECK(num_nonnulls(receipt_movement_id,opening_checkpoint_id)=1),
 UNIQUE(company_id,id),UNIQUE(company_id,receipt_movement_id),
 UNIQUE(company_id,opening_checkpoint_id),
 FOREIGN KEY(company_id,receipt_movement_id) REFERENCES erp.stock_movements(company_id,id),
 FOREIGN KEY(company_id,opening_checkpoint_id)
 REFERENCES erp.inventory_cost_checkpoints(company_id,id)
);
CREATE INDEX ix_cost_layer_scope ON erp.inventory_cost_layers
 (company_id,warehouse_id,item_id,lot_id,id);
ALTER TABLE erp.inventory_cost_layers ENABLE ROW LEVEL SECURITY;
CREATE POLICY company_scope ON erp.inventory_cost_layers
 USING(EXISTS(SELECT 1 FROM erp.companies c WHERE c.id=company_id
 AND c.tenant_id=erp.current_tenant_id()))
 WITH CHECK(EXISTS(SELECT 1 FROM erp.companies c WHERE c.id=company_id
 AND c.tenant_id=erp.current_tenant_id()));
ALTER TABLE erp.inventory_cost_allocations ALTER COLUMN receipt_movement_id DROP NOT NULL;
ALTER TABLE erp.inventory_cost_allocations
 ADD COLUMN opening_checkpoint_id uuid,
 ADD COLUMN cost_layer_id uuid,
 ADD COLUMN basis_layer_quantity numeric(20,6),
 ADD COLUMN basis_layer_value numeric(20,6),
 ADD COLUMN preceding_layer_value numeric(20,6),
 ADD CONSTRAINT ck_cost_allocation_origin
 CHECK(num_nonnulls(receipt_movement_id,opening_checkpoint_id)=1),
 ADD CONSTRAINT fk_cost_allocation_checkpoint FOREIGN KEY(company_id,opening_checkpoint_id)
 REFERENCES erp.inventory_cost_checkpoints(company_id,id),
 ADD CONSTRAINT fk_cost_allocation_layer FOREIGN KEY(company_id,cost_layer_id)
 REFERENCES erp.inventory_cost_layers(company_id,id),
 ADD CONSTRAINT ck_cost_allocation_basis CHECK(cost_layer_id IS NULL OR
 (basis_layer_quantity IS NOT NULL AND basis_layer_quantity>0
 AND basis_layer_value IS NOT NULL AND basis_layer_value>=0
 AND preceding_layer_value IS NOT NULL AND preceding_layer_value>=0));
CREATE UNIQUE INDEX ix_cost_allocation_issue_layer
 ON erp.inventory_cost_allocations(company_id,issue_movement_id,cost_layer_id);
CREATE INDEX ix_cost_allocation_layer ON erp.inventory_cost_allocations(company_id,cost_layer_id)
 WHERE cost_layer_id IS NOT NULL;
CREATE FUNCTION erp.guard_inventory_cost_layer() RETURNS trigger LANGUAGE plpgsql
SET search_path=pg_catalog,erp AS $$
DECLARE s record;c record;
BEGIN
 IF TG_OP<>'INSERT' THEN RAISE EXCEPTION 'Cost layers are immutable'; END IF;
 IF NEW.opening_checkpoint_id IS NOT NULL THEN
 SELECT warehouse_id,item_id,lot_id,quantity,value_company INTO s
 FROM erp.inventory_cost_checkpoints WHERE company_id=NEW.company_id
 AND id=NEW.opening_checkpoint_id;
 ELSE
 SELECT warehouse_id,item_id,lot_id,quantity_delta quantity,value_delta_company value_company
 INTO s FROM erp.stock_movements WHERE company_id=NEW.company_id AND id=NEW.receipt_movement_id;
 SELECT * INTO c FROM erp.inventory_cost_checkpoints WHERE company_id=NEW.company_id
 AND warehouse_id=NEW.warehouse_id AND item_id=NEW.item_id
 AND lot_id IS NOT DISTINCT FROM NEW.lot_id;
 IF NOT FOUND OR EXISTS(SELECT 1 FROM erp.inventory_cost_checkpoint_movements
 WHERE company_id=NEW.company_id AND checkpoint_id=c.id
 AND movement_id=NEW.receipt_movement_id) THEN
 RAISE EXCEPTION 'Receipt layer requires an uncovered receipt in an adopted scope'; END IF;
 END IF;
 IF s.quantity IS DISTINCT FROM NEW.quantity OR s.value_company IS DISTINCT FROM NEW.value_company
 OR s.warehouse_id IS DISTINCT FROM NEW.warehouse_id OR s.item_id IS DISTINCT FROM NEW.item_id
 OR s.lot_id IS DISTINCT FROM NEW.lot_id THEN
 RAISE EXCEPTION 'Layer origin does not reconcile'; END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER trg_cost_layer_history BEFORE INSERT OR UPDATE OR DELETE
 ON erp.inventory_cost_layers FOR EACH ROW EXECUTE FUNCTION erp.guard_inventory_cost_layer();
CREATE FUNCTION erp.guard_inventory_cost_allocation() RETURNS trigger LANGUAGE plpgsql
SET search_path=pg_catalog,erp AS $$
DECLARE l record;b record;m record;u record;v numeric;v_xid text;
BEGIN
 IF TG_OP<>'INSERT' THEN RAISE EXCEPTION 'Cost allocations are immutable'; END IF;
 IF NEW.cost_layer_id IS NULL THEN
 RAISE EXCEPTION 'New cost allocations require a retained layer'; END IF;
 SELECT * INTO l FROM erp.inventory_cost_layers
 WHERE company_id=NEW.company_id AND id=NEW.cost_layer_id FOR UPDATE;
 SELECT * INTO m FROM erp.stock_movements
 WHERE company_id=NEW.company_id AND id=NEW.issue_movement_id;
 SELECT * INTO b FROM erp.inventory_cost_basis_snapshots
 WHERE company_id=NEW.company_id AND movement_id=NEW.issue_movement_id;
 IF NOT FOUND THEN RAISE EXCEPTION 'Allocation requires issue cost basis'; END IF;
 SELECT xmin::text INTO v_xid FROM erp.inventory_cost_basis_snapshots
 WHERE company_id=NEW.company_id AND movement_id=NEW.issue_movement_id;
 IF v_xid IS DISTINCT FROM pg_current_xact_id()::xid::text THEN
 RAISE EXCEPTION 'Issue allocations are sealed at posting'; END IF;
 SELECT coalesce(sum(quantity),0) q,coalesce(sum(value_company),0) v INTO u
 FROM erp.inventory_cost_allocations WHERE company_id=NEW.company_id AND cost_layer_id=l.id;
 IF l.receipt_movement_id IS DISTINCT FROM NEW.receipt_movement_id
 OR l.opening_checkpoint_id IS DISTINCT FROM NEW.opening_checkpoint_id
 OR l.warehouse_id IS DISTINCT FROM m.warehouse_id OR l.item_id IS DISTINCT FROM m.item_id
 OR l.lot_id IS DISTINCT FROM m.lot_id OR m.quantity_delta>=0
 OR NEW.basis_layer_quantity IS DISTINCT FROM l.quantity-u.q
 OR NEW.basis_layer_value IS DISTINCT FROM l.value_company-u.v
 OR NEW.quantity<>NEW.basis_layer_quantity*b.issue_quantity/b.basis_quantity
 OR NEW.quantity>NEW.basis_layer_quantity OR NEW.value_company>NEW.basis_layer_value
 OR NEW.unit_cost_company<>round(NEW.value_company/NEW.quantity,6) THEN
 RAISE EXCEPTION 'Allocation scope, basis or layer capacity mismatch'; END IF;
 v:=CASE WHEN b.basis_value_company=0 THEN 0 ELSE
 round((NEW.preceding_layer_value+NEW.basis_layer_value)*b.issue_value_company/
 b.basis_value_company,6)-round(NEW.preceding_layer_value*b.issue_value_company/
 b.basis_value_company,6) END;
 IF NEW.value_company<>v OR (NEW.quantity=NEW.basis_layer_quantity
 AND NEW.value_company<>NEW.basis_layer_value) THEN
 RAISE EXCEPTION 'Allocation does not match proportional cost formula'; END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER trg_cost_allocation_history BEFORE INSERT OR UPDATE OR DELETE
 ON erp.inventory_cost_allocations FOR EACH ROW
 EXECUTE FUNCTION erp.guard_inventory_cost_allocation();
CREATE FUNCTION erp.check_inventory_layer_issue_complete() RETURNS trigger LANGUAGE plpgsql
SET search_path=pg_catalog,erp AS $$
DECLARE m record;a record;b record;v_id uuid;
BEGIN
 IF TG_TABLE_NAME='stock_movements' THEN v_id:=NEW.id; ELSE v_id:=NEW.movement_id; END IF;
 SELECT * INTO m FROM erp.stock_movements WHERE company_id=NEW.company_id AND id=v_id;
 IF m.quantity_delta>0 THEN RETURN NULL; END IF;
 IF NOT EXISTS(SELECT 1 FROM erp.inventory_cost_checkpoints c WHERE c.company_id=m.company_id
 AND c.warehouse_id=m.warehouse_id AND c.item_id=m.item_id
 AND c.lot_id IS NOT DISTINCT FROM m.lot_id) THEN RETURN NULL; END IF;
 SELECT * INTO b FROM erp.inventory_cost_basis_snapshots WHERE company_id=NEW.company_id
 AND movement_id=v_id;
 IF NOT FOUND THEN RAISE EXCEPTION 'Adopted stock issue requires retained cost basis'; END IF;
 SELECT coalesce(sum(quantity),0) q,coalesce(sum(value_company),0) v,
 coalesce(sum(basis_layer_quantity),0) bq,coalesce(sum(basis_layer_value),0) bv INTO a
 FROM erp.inventory_cost_allocations WHERE company_id=NEW.company_id
 AND issue_movement_id=v_id;
 IF a.q<>b.issue_quantity OR a.v<>b.issue_value_company
 OR a.bq<>b.basis_quantity OR a.bv<>b.basis_value_company THEN
 RAISE EXCEPTION 'Issue requires complete layer allocations'; END IF;
 IF EXISTS(SELECT 1 FROM (
 SELECT preceding_layer_value,coalesce(sum(basis_layer_value) OVER
 (ORDER BY cost_layer_id ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING),0) expected
 FROM erp.inventory_cost_allocations WHERE company_id=NEW.company_id
 AND issue_movement_id=v_id) x WHERE preceding_layer_value<>expected) THEN
 RAISE EXCEPTION 'Allocation layer ordering is inconsistent'; END IF;
 RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER trg_layer_issue_complete
 AFTER INSERT ON erp.inventory_cost_basis_snapshots DEFERRABLE INITIALLY DEFERRED
 FOR EACH ROW EXECUTE FUNCTION erp.check_inventory_layer_issue_complete();
CREATE CONSTRAINT TRIGGER trg_adopted_stock_issue_complete
 AFTER INSERT ON erp.stock_movements DEFERRABLE INITIALLY DEFERRED
 FOR EACH ROW EXECUTE FUNCTION erp.check_inventory_layer_issue_complete();
CREATE TRIGGER trg_cost_layer_audit AFTER INSERT ON erp.inventory_cost_layers
 FOR EACH ROW EXECUTE FUNCTION erp.audit_inventory_command();
CREATE TRIGGER trg_cost_allocation_audit AFTER INSERT ON erp.inventory_cost_allocations
 FOR EACH ROW EXECUTE FUNCTION erp.audit_inventory_command();
"""

REVERSE = r"""
DO $$ BEGIN
 IF EXISTS(SELECT 1 FROM erp.inventory_cost_allocations WHERE cost_layer_id IS NOT NULL)
 OR EXISTS(SELECT 1 FROM erp.inventory_cost_layers) THEN
 RAISE EXCEPTION 'Cannot reverse migration with retained costing history'; END IF;
END $$;
DROP TRIGGER trg_layer_issue_complete ON erp.inventory_cost_basis_snapshots;
DROP TRIGGER trg_adopted_stock_issue_complete ON erp.stock_movements;
DROP FUNCTION erp.check_inventory_layer_issue_complete();
DROP TRIGGER trg_cost_allocation_history ON erp.inventory_cost_allocations;
DROP TRIGGER trg_cost_allocation_audit ON erp.inventory_cost_allocations;
DROP FUNCTION erp.guard_inventory_cost_allocation();
DROP INDEX erp.ix_cost_allocation_issue_layer,erp.ix_cost_allocation_layer;
ALTER TABLE erp.inventory_cost_allocations
 DROP CONSTRAINT ck_cost_allocation_origin,DROP CONSTRAINT fk_cost_allocation_checkpoint,
 DROP CONSTRAINT fk_cost_allocation_layer,DROP CONSTRAINT ck_cost_allocation_basis,
 DROP COLUMN opening_checkpoint_id,DROP COLUMN cost_layer_id,
 DROP COLUMN basis_layer_quantity,DROP COLUMN basis_layer_value,DROP COLUMN preceding_layer_value;
ALTER TABLE erp.inventory_cost_allocations ALTER COLUMN receipt_movement_id SET NOT NULL;
DROP TABLE erp.inventory_cost_layers;
DROP FUNCTION erp.guard_inventory_cost_layer();
"""


class Migration(migrations.Migration):
    dependencies = [("database", "0024_inventory_cost_checkpoints")]
    operations = [migrations.RunSQL(SQL, REVERSE)]
