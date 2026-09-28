from django.db import migrations

SQL = r"""
CREATE TABLE erp.inventory_cost_checkpoints(
 id uuid PRIMARY KEY DEFAULT uuidv7(),company_id uuid NOT NULL,
 warehouse_id uuid NOT NULL,item_id uuid NOT NULL,lot_id uuid,
 policy_id uuid NOT NULL,quantity numeric(20,6) NOT NULL CHECK(quantity>=0),
 value_company numeric(20,6) NOT NULL CHECK(value_company>=0),
 movement_count integer NOT NULL CHECK(movement_count BETWEEN 0 AND 10000),
 reason text NOT NULL CHECK(length(trim(reason)) BETWEEN 1 AND 2000),
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 UNIQUE(company_id,id),UNIQUE NULLS NOT DISTINCT(company_id,warehouse_id,item_id,lot_id),
 CHECK(quantity>0 OR value_company=0),
 FOREIGN KEY(company_id,warehouse_id) REFERENCES erp.warehouses(company_id,id),
 FOREIGN KEY(company_id,item_id) REFERENCES erp.items(company_id,id),
 FOREIGN KEY(company_id,item_id,lot_id) REFERENCES erp.inventory_lots(company_id,item_id,id),
 FOREIGN KEY(company_id,policy_id) REFERENCES erp.inventory_cost_policies(company_id,id)
);
CREATE TABLE erp.inventory_cost_checkpoint_movements(
 company_id uuid NOT NULL,checkpoint_id uuid NOT NULL,movement_id uuid NOT NULL,
 PRIMARY KEY(company_id,checkpoint_id,movement_id),
 FOREIGN KEY(company_id,checkpoint_id) REFERENCES erp.inventory_cost_checkpoints(company_id,id),
 FOREIGN KEY(company_id,movement_id) REFERENCES erp.stock_movements(company_id,id)
);
ALTER TABLE erp.inventory_cost_checkpoints ENABLE ROW LEVEL SECURITY;
ALTER TABLE erp.inventory_cost_checkpoint_movements ENABLE ROW LEVEL SECURITY;
CREATE POLICY company_scope ON erp.inventory_cost_checkpoints
 USING(EXISTS(SELECT 1 FROM erp.companies c WHERE c.id=company_id
 AND c.tenant_id=erp.current_tenant_id()))
 WITH CHECK(EXISTS(SELECT 1 FROM erp.companies c WHERE c.id=company_id
 AND c.tenant_id=erp.current_tenant_id()));
CREATE POLICY company_scope ON erp.inventory_cost_checkpoint_movements
 USING(EXISTS(SELECT 1 FROM erp.companies c WHERE c.id=company_id
 AND c.tenant_id=erp.current_tenant_id()))
 WITH CHECK(EXISTS(SELECT 1 FROM erp.companies c WHERE c.id=company_id
 AND c.tenant_id=erp.current_tenant_id()));
CREATE FUNCTION erp.guard_inventory_cost_checkpoint() RETURNS trigger LANGUAGE plpgsql
SET search_path=pg_catalog,erp AS $$
DECLARE p record;h record;v_xid text;
BEGIN
 IF TG_OP<>'INSERT' THEN RAISE EXCEPTION 'Cost checkpoints are immutable'; END IF;
 IF TG_TABLE_NAME='inventory_cost_checkpoint_movements' THEN
 SELECT xmin::text INTO v_xid FROM erp.inventory_cost_checkpoints
 WHERE company_id=NEW.company_id AND id=NEW.checkpoint_id;
 IF v_xid IS DISTINCT FROM pg_current_xact_id()::xid::text THEN
 RAISE EXCEPTION 'Checkpoint membership is sealed at creation'; END IF;
 RETURN NEW; END IF;
 SELECT * INTO p FROM erp.inventory_positions WHERE company_id=NEW.company_id
 AND warehouse_id=NEW.warehouse_id AND item_id=NEW.item_id
 AND lot_id IS NOT DISTINCT FROM NEW.lot_id FOR UPDATE;
 IF NOT FOUND THEN RAISE EXCEPTION 'Checkpoint requires an existing stock scope'; END IF;
 SELECT count(*) n,coalesce(sum(quantity_delta),0) q,coalesce(sum(value_delta_company),0) v INTO h
 FROM erp.stock_movements WHERE company_id=NEW.company_id AND warehouse_id=NEW.warehouse_id
 AND item_id=NEW.item_id AND lot_id IS NOT DISTINCT FROM NEW.lot_id;
 IF h.n<>NEW.movement_count OR h.q<>NEW.quantity OR h.v<>NEW.value_company
 OR p.on_hand_quantity<>h.q OR p.value_company<>h.v THEN
 RAISE EXCEPTION 'Checkpoint does not reconcile with ledger and position'; END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER trg_cost_checkpoint_history BEFORE INSERT OR UPDATE OR DELETE
 ON erp.inventory_cost_checkpoints FOR EACH ROW
 EXECUTE FUNCTION erp.guard_inventory_cost_checkpoint();
CREATE TRIGGER trg_cost_checkpoint_member_history BEFORE INSERT OR UPDATE OR DELETE
 ON erp.inventory_cost_checkpoint_movements FOR EACH ROW
 EXECUTE FUNCTION erp.guard_inventory_cost_checkpoint();
CREATE FUNCTION erp.check_inventory_cost_checkpoint_members() RETURNS trigger LANGUAGE plpgsql
SET search_path=pg_catalog,erp AS $$
DECLARE c record;h record;
BEGIN
 SELECT * INTO c FROM erp.inventory_cost_checkpoints WHERE company_id=NEW.company_id
 AND id=NEW.id;
 SELECT count(*) n,coalesce(sum(m.quantity_delta),0) q,coalesce(sum(m.value_delta_company),0) v,
 coalesce(bool_and(m.warehouse_id=c.warehouse_id AND m.item_id=c.item_id
 AND m.lot_id IS NOT DISTINCT FROM c.lot_id),true) valid INTO h
 FROM erp.inventory_cost_checkpoint_movements x JOIN erp.stock_movements m
 ON m.company_id=x.company_id AND m.id=x.movement_id
 WHERE x.company_id=c.company_id AND x.checkpoint_id=c.id;
 IF h.n<>c.movement_count OR h.q<>c.quantity OR h.v<>c.value_company OR NOT h.valid THEN
 RAISE EXCEPTION 'Checkpoint membership is incomplete or inconsistent'; END IF;
 RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER trg_cost_checkpoint_complete
 AFTER INSERT ON erp.inventory_cost_checkpoints DEFERRABLE INITIALLY DEFERRED
 FOR EACH ROW EXECUTE FUNCTION erp.check_inventory_cost_checkpoint_members();
CREATE TRIGGER trg_cost_checkpoint_audit AFTER INSERT ON erp.inventory_cost_checkpoints
 FOR EACH ROW EXECUTE FUNCTION erp.audit_inventory_command();
"""

REVERSE = r"""
DROP TABLE erp.inventory_cost_checkpoint_movements,erp.inventory_cost_checkpoints;
DROP FUNCTION erp.check_inventory_cost_checkpoint_members();
DROP FUNCTION erp.guard_inventory_cost_checkpoint();
"""


class Migration(migrations.Migration):
    dependencies = [("database", "0023_inventory_cost_basis")]
    operations = [migrations.RunSQL(SQL, REVERSE)]
