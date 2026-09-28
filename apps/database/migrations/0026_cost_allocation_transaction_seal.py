from django.db import migrations

# A row's xmin can be a subtransaction ID. Retain the owning transaction ID
# explicitly so nested replacement posting can create its allocations atomically.
SQL = r"""
ALTER TABLE erp.inventory_cost_basis_snapshots
 ADD COLUMN allocation_seal_xid xid8 NOT NULL DEFAULT pg_current_xact_id();
CREATE FUNCTION erp.guard_cost_basis_transaction_seal() RETURNS trigger LANGUAGE plpgsql
SET search_path=pg_catalog,erp AS $$
BEGIN
 IF NEW.allocation_seal_xid IS DISTINCT FROM pg_current_xact_id() THEN
 RAISE EXCEPTION 'Cost basis seal must match the owning transaction'; END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER trg_cost_basis_transaction_seal BEFORE INSERT
 ON erp.inventory_cost_basis_snapshots FOR EACH ROW
 EXECUTE FUNCTION erp.guard_cost_basis_transaction_seal();
CREATE OR REPLACE FUNCTION erp.guard_inventory_cost_allocation() RETURNS trigger LANGUAGE plpgsql
SET search_path=pg_catalog,erp AS $$
DECLARE l record;b record;m record;u record;v numeric;
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
 IF b.allocation_seal_xid IS DISTINCT FROM pg_current_xact_id() THEN
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
"""

# Restore the exact pre-upgrade guard; the seal column is technical metadata,
# not a financial posting fact. Previously committed allocations remain sealed.
REVERSE = r"""
CREATE OR REPLACE FUNCTION erp.guard_inventory_cost_allocation() RETURNS trigger LANGUAGE plpgsql
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
DROP TRIGGER trg_cost_basis_transaction_seal ON erp.inventory_cost_basis_snapshots;
DROP FUNCTION erp.guard_cost_basis_transaction_seal();
ALTER TABLE erp.inventory_cost_basis_snapshots DROP COLUMN allocation_seal_xid;
"""


class Migration(migrations.Migration):
    dependencies = [("database", "0025_inventory_cost_layers")]
    operations = [migrations.RunSQL(SQL, REVERSE)]
