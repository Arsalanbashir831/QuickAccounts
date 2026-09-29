"""Material-only execution with source-linked stock and WIP reconciliation."""

from importlib import import_module

from django.db import migrations

draft_sql = import_module("apps.database.migrations.0031_manufacturing_drafts").SQL
reservation_sql = import_module("apps.database.migrations.0020_phase58_stock_commands").SQL
consumed_sql = import_module("apps.database.migrations.0022_inventory_posting_guards").SQL


def definition(sql: str, name: str) -> str:
    start = sql.index(f"CREATE FUNCTION erp.{name}()")
    end = sql.index("END $$;", start) + len("END $$;")
    return sql[start:end].replace("CREATE FUNCTION", "CREATE OR REPLACE FUNCTION", 1)


old_plan = definition(draft_sql, "guard_production_plan")
old_reservation = definition(reservation_sql, "guard_typed_reservation")
old_consumed = definition(consumed_sql, "check_reservation_consumed")

SQL = r"""
DO $$ BEGIN IF EXISTS(SELECT 1 FROM erp.production_material_issues)
 OR EXISTS(SELECT 1 FROM erp.production_outputs) THEN
 RAISE EXCEPTION 'Manufacturing execution history requires reviewed adoption'; END IF; END $$;
ALTER TABLE erp.production_orders
 ADD COLUMN execution_edit_xid xid8,
 ADD COLUMN wip_account_id_snapshot uuid,
 ADD COLUMN output_inventory_account_id_snapshot uuid,
 ADD COLUMN released_at timestamptz,ADD COLUMN released_by uuid REFERENCES identity.users(id),
 ADD COLUMN completed_at timestamptz,
 ADD COLUMN execution_policy text NOT NULL DEFAULT 'material_only_actual_v1'
 CHECK(execution_policy='material_only_actual_v1'),
 ADD CONSTRAINT fk_production_wip FOREIGN KEY(company_id,wip_account_id_snapshot)
 REFERENCES erp.accounts(company_id,id),
 ADD CONSTRAINT fk_production_output_account FOREIGN
KEY(company_id,output_inventory_account_id_snapshot)
 REFERENCES erp.accounts(company_id,id);
ALTER TABLE erp.inventory_reservations ADD CONSTRAINT uq_production_reservation_id
UNIQUE(company_id,id);
CREATE TABLE erp.production_execution_batches(
 id uuid PRIMARY KEY DEFAULT uuidv7(),company_id uuid NOT NULL,
 production_order_id uuid NOT NULL,kind text NOT NULL CHECK(kind IN('issue','output','cancel')),
 posting_date date NOT NULL,journal_entry_id uuid,currency_precision integer NOT NULL
 CHECK(currency_precision BETWEEN 0 AND 6),created_at timestamptz NOT NULL DEFAULT
clock_timestamp(),
 write_xid xid8 NOT NULL DEFAULT pg_current_xact_id(),
 basis_completed_quantity numeric(20,6),basis_allocated_cost numeric(20,6),
 basis_material_cost numeric(20,6),
 CHECK((kind='output' AND
 num_nonnulls(basis_completed_quantity,basis_allocated_cost,basis_material_cost)=3)
 OR (kind<>'output' AND
 num_nonnulls(basis_completed_quantity,basis_allocated_cost,basis_material_cost)=0)),
 UNIQUE(company_id,id),FOREIGN KEY(company_id,production_order_id)
 REFERENCES erp.production_orders(company_id,id),FOREIGN KEY(company_id,journal_entry_id)
 REFERENCES erp.journal_entries(company_id,id)
);
CREATE INDEX ix_production_batch_order ON
erp.production_execution_batches(company_id,production_order_id);
CREATE INDEX ix_production_batch_journal ON
erp.production_execution_batches(company_id,journal_entry_id);
ALTER TABLE erp.production_material_issues
 ADD COLUMN batch_id uuid NOT NULL,ADD COLUMN reservation_id uuid NOT NULL,
 ADD COLUMN inventory_account_id_snapshot uuid NOT NULL,
 ADD CONSTRAINT uq_production_issue_id UNIQUE(company_id,id),
 ADD CONSTRAINT uq_production_issue_reservation UNIQUE(company_id,reservation_id),
 ADD CONSTRAINT fk_production_issue_batch FOREIGN KEY(company_id,batch_id)
 REFERENCES erp.production_execution_batches(company_id,id),
 ADD CONSTRAINT fk_production_issue_reservation FOREIGN KEY(company_id,reservation_id)
 REFERENCES erp.inventory_reservations(company_id,id),
 ADD CONSTRAINT fk_production_issue_account FOREIGN KEY(company_id,inventory_account_id_snapshot)
 REFERENCES erp.accounts(company_id,id);
ALTER TABLE erp.production_outputs ADD COLUMN batch_id uuid NOT NULL,
 ADD CONSTRAINT uq_production_output_id UNIQUE(company_id,id),
 ADD CONSTRAINT uq_production_output_batch UNIQUE(company_id,batch_id),
 ADD CONSTRAINT fk_production_output_batch FOREIGN KEY(company_id,batch_id)
 REFERENCES erp.production_execution_batches(company_id,id);
CREATE TABLE erp.production_material_returns(
 id uuid PRIMARY KEY DEFAULT uuidv7(),company_id uuid NOT NULL,
 production_order_id uuid NOT NULL,batch_id uuid NOT NULL,material_issue_id uuid NOT NULL,
 stock_movement_id uuid NOT NULL,created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 UNIQUE(company_id,id),UNIQUE(company_id,material_issue_id),UNIQUE(company_id,stock_movement_id),
 FOREIGN KEY(company_id,production_order_id) REFERENCES erp.production_orders(company_id,id),
 FOREIGN KEY(company_id,batch_id) REFERENCES erp.production_execution_batches(company_id,id),
 FOREIGN KEY(company_id,material_issue_id) REFERENCES
erp.production_material_issues(company_id,id),
 FOREIGN KEY(company_id,stock_movement_id) REFERENCES erp.stock_movements(company_id,id)
);
CREATE INDEX ix_production_return_order ON
erp.production_material_returns(company_id,production_order_id);
CREATE INDEX ix_production_return_batch ON erp.production_material_returns(company_id,batch_id);
CREATE INDEX ix_production_issue_order ON
erp.production_material_issues(company_id,production_order_id);
CREATE INDEX ix_production_issue_batch ON erp.production_material_issues(company_id,batch_id);
CREATE INDEX ix_production_issue_account ON
erp.production_material_issues(company_id,inventory_account_id_snapshot);
CREATE INDEX ix_production_output_order ON erp.production_outputs(company_id,production_order_id);
CREATE INDEX ix_production_output_batch ON erp.production_outputs(company_id,batch_id);
ALTER TABLE erp.production_execution_batches ENABLE ROW LEVEL SECURITY;
ALTER TABLE erp.production_material_returns ENABLE ROW LEVEL SECURITY;
CREATE POLICY company_scope ON erp.production_execution_batches
 USING(EXISTS(SELECT 1 FROM erp.companies c WHERE c.id=company_id AND
c.tenant_id=erp.current_tenant_id()))
 WITH CHECK(EXISTS(SELECT 1 FROM erp.companies c WHERE c.id=company_id AND
c.tenant_id=erp.current_tenant_id()));
CREATE POLICY company_scope ON erp.production_material_returns
 USING(EXISTS(SELECT 1 FROM erp.companies c WHERE c.id=company_id AND
c.tenant_id=erp.current_tenant_id()))
 WITH CHECK(EXISTS(SELECT 1 FROM erp.companies c WHERE c.id=company_id AND
c.tenant_id=erp.current_tenant_id()));
DROP TRIGGER trg_production_issue_disabled ON erp.production_material_issues;
DROP TRIGGER trg_production_output_disabled ON erp.production_outputs;
CREATE FUNCTION erp.guard_production_execution_history() RETURNS trigger LANGUAGE plpgsql
SET search_path=pg_catalog AS $$
DECLARE o record;b record;r record;m record;i record;
BEGIN
 IF TG_OP<>'INSERT' THEN RAISE EXCEPTION 'MFG_HISTORY_FROZEN: Execution facts are immutable'; END
IF;
 SELECT * INTO o FROM erp.production_orders WHERE company_id=NEW.company_id
 AND id=NEW.production_order_id FOR UPDATE;
 IF o.status NOT IN('released','in_progress') OR o.execution_edit_xid IS DISTINCT FROM
pg_current_xact_id() THEN
 RAISE EXCEPTION 'MFG_ORDER_STATE_INVALID: Execution requires an authorized released order'; END
IF;
 IF TG_TABLE_NAME='production_execution_batches' THEN
 IF NEW.kind='output' AND (NEW.basis_completed_quantity IS DISTINCT FROM
 (SELECT coalesce(sum(quantity_completed),0) FROM erp.production_outputs
 WHERE company_id=o.company_id AND production_order_id=o.id)
 OR NEW.basis_allocated_cost IS DISTINCT FROM
 (SELECT coalesce(sum(cost_company),0) FROM erp.production_outputs
 WHERE company_id=o.company_id AND production_order_id=o.id)
 OR NEW.basis_material_cost IS DISTINCT FROM
 (SELECT coalesce(sum(cost_company),0) FROM erp.production_material_issues
 WHERE company_id=o.company_id AND production_order_id=o.id)) THEN
 RAISE EXCEPTION 'MFG_WIP_DRIFT: Output must snapshot the locked actual-cost basis'; END IF;
 NEW.write_xid:=pg_current_xact_id();RETURN NEW; END IF;
 SELECT * INTO b FROM erp.production_execution_batches WHERE company_id=NEW.company_id AND
id=NEW.batch_id;
 IF b.production_order_id IS DISTINCT FROM o.id OR b.write_xid IS DISTINCT FROM
pg_current_xact_id() THEN
 RAISE EXCEPTION 'MFG_BATCH_INVALID: Execution batch is sealed'; END IF;
 SELECT * INTO m FROM erp.stock_movements WHERE company_id=NEW.company_id AND
id=NEW.stock_movement_id;
 IF m.source_type IS DISTINCT FROM 'production_execution' OR m.source_id IS DISTINCT FROM b.id
 OR m.source_line_id IS DISTINCT FROM NEW.id OR m.warehouse_id IS DISTINCT FROM o.warehouse_id
 OR m.journal_entry_id IS DISTINCT FROM b.journal_entry_id
 OR (m.occurred_at AT TIME ZONE 'UTC')::date IS DISTINCT FROM b.posting_date THEN
 RAISE EXCEPTION 'MFG_MOVEMENT_INVALID: Production stock links do not reconcile'; END IF;
 IF TG_TABLE_NAME='production_material_issues' THEN
 SELECT * INTO r FROM erp.inventory_reservations WHERE company_id=NEW.company_id AND
id=NEW.reservation_id;
 IF b.kind<>'issue' OR r.status IS DISTINCT FROM 'consumed'
 OR r.source_type IS DISTINCT FROM 'production_requirement'
 OR NOT EXISTS(SELECT 1 FROM erp.production_order_requirements q WHERE q.company_id=r.company_id
 AND q.id=r.source_id AND q.production_order_id=o.id)
 OR r.item_id IS DISTINCT FROM NEW.component_item_id OR r.quantity IS DISTINCT FROM
NEW.quantity_issued
 OR m.item_id IS DISTINCT FROM r.item_id OR m.lot_id IS DISTINCT FROM r.lot_id
 OR m.quantity_delta IS DISTINCT FROM -NEW.quantity_issued
 OR m.value_delta_company IS DISTINCT FROM -NEW.cost_company
 OR NEW.inventory_account_id_snapshot=o.wip_account_id_snapshot THEN
 RAISE EXCEPTION 'MFG_ISSUE_INVALID: Material issue differs from its reservation or cost'; END IF;
 ELSIF TG_TABLE_NAME='production_outputs' THEN
 IF b.kind<>'output' OR m.item_id IS DISTINCT FROM o.output_item_id
 OR NEW.output_item_id IS DISTINCT FROM o.output_item_id
 OR m.quantity_delta IS DISTINCT FROM NEW.quantity_completed
 OR m.value_delta_company IS DISTINCT FROM NEW.cost_company
 OR m.unit_cost_company<>round(NEW.cost_company/NEW.quantity_completed,6)
 OR NEW.cost_company<>round(b.basis_material_cost*
 (b.basis_completed_quantity+NEW.quantity_completed)/o.planned_quantity,b.currency_precision)
 -b.basis_allocated_cost THEN
 RAISE EXCEPTION 'MFG_OUTPUT_INVALID: Output stock does not reconcile'; END IF;
 ELSE
 SELECT * INTO i FROM erp.production_material_issues WHERE company_id=NEW.company_id AND
id=NEW.material_issue_id;
 SELECT * INTO r FROM erp.stock_movements WHERE company_id=i.company_id AND
id=i.stock_movement_id;
 IF b.kind<>'cancel' OR i.production_order_id IS DISTINCT FROM o.id
 OR m.item_id IS DISTINCT FROM r.item_id OR m.lot_id IS DISTINCT FROM r.lot_id
 OR m.quantity_delta IS DISTINCT FROM -r.quantity_delta
 OR m.value_delta_company IS DISTINCT FROM -r.value_delta_company
 OR m.unit_cost_company IS DISTINCT FROM r.unit_cost_company THEN
 RAISE EXCEPTION 'MFG_RETURN_INVALID: Cancellation must restore original quantity and cost'; END
IF;
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER trg_production_batch_history BEFORE INSERT OR UPDATE OR DELETE ON
erp.production_execution_batches
 FOR EACH ROW EXECUTE FUNCTION erp.guard_production_execution_history();
CREATE TRIGGER trg_production_issue_history BEFORE INSERT OR UPDATE OR DELETE ON
erp.production_material_issues
 FOR EACH ROW EXECUTE FUNCTION erp.guard_production_execution_history();
CREATE TRIGGER trg_production_output_history BEFORE INSERT OR UPDATE OR DELETE ON
erp.production_outputs
 FOR EACH ROW EXECUTE FUNCTION erp.guard_production_execution_history();
CREATE TRIGGER trg_production_return_history BEFORE INSERT OR UPDATE OR DELETE ON
erp.production_material_returns
 FOR EACH ROW EXECUTE FUNCTION erp.guard_production_execution_history();
CREATE TRIGGER trg_production_batch_audit AFTER INSERT ON erp.production_execution_batches
 FOR EACH ROW EXECUTE FUNCTION erp.audit_manufacturing_plan();
CREATE TRIGGER trg_production_issue_audit AFTER INSERT ON erp.production_material_issues
 FOR EACH ROW EXECUTE FUNCTION erp.audit_manufacturing_plan();
CREATE TRIGGER trg_production_output_audit AFTER INSERT ON erp.production_outputs
 FOR EACH ROW EXECUTE FUNCTION erp.audit_manufacturing_plan();
CREATE TRIGGER trg_production_return_audit AFTER INSERT ON erp.production_material_returns
 FOR EACH ROW EXECUTE FUNCTION erp.audit_manufacturing_plan();
CREATE FUNCTION erp.check_production_execution_complete() RETURNS trigger LANGUAGE plpgsql
SET search_path=pg_catalog AS $$
DECLARE o record;b record;v_order uuid;v_cost numeric;v_quantity numeric;v_allocated
numeric;v_value numeric;v_precision integer;
BEGIN
 IF TG_TABLE_NAME='production_orders' THEN v_order:=NEW.id;
 ELSIF TG_TABLE_NAME='stock_movements' THEN
 SELECT production_order_id INTO v_order FROM erp.production_execution_batches
 WHERE company_id=NEW.company_id AND id=NEW.source_id;
 IF v_order IS NULL THEN RAISE EXCEPTION 'MFG_BATCH_INVALID: Orphan production movement'; END IF;
 ELSE v_order:=NEW.production_order_id; END IF;
 SELECT * INTO o FROM erp.production_orders WHERE company_id=NEW.company_id AND id=v_order;
 SELECT coalesce(sum(cost_company),0) INTO v_cost FROM erp.production_material_issues
 WHERE company_id=o.company_id AND production_order_id=o.id;
 SELECT coalesce(sum(quantity_completed),0),coalesce(sum(cost_company),0) INTO
v_quantity,v_allocated
 FROM erp.production_outputs WHERE company_id=o.company_id AND production_order_id=o.id;
 SELECT minor_units INTO v_precision FROM erp.currencies WHERE code=(SELECT functional_currency
FROM erp.companies WHERE id=o.company_id);
 IF o.completed_quantity<>v_quantity OR v_quantity>o.planned_quantity
 OR v_allocated<>round(v_cost*v_quantity/o.planned_quantity,v_precision) THEN
 RAISE EXCEPTION 'MFG_WIP_DRIFT: Output quantity or cumulative actual cost differs'; END IF;
 IF EXISTS(SELECT 1 FROM erp.production_order_requirements q WHERE q.company_id=o.company_id
 AND q.production_order_id=o.id AND (
 coalesce((SELECT sum(i.quantity_issued) FROM erp.production_material_issues i
 JOIN erp.inventory_reservations r ON r.company_id=i.company_id AND r.id=i.reservation_id
 WHERE i.company_id=q.company_id AND r.source_id=q.id),0)>q.required_quantity
 OR (o.status IN('released','in_progress','completed') AND
 coalesce((SELECT sum(r.quantity) FROM erp.inventory_reservations r WHERE
r.company_id=q.company_id
 AND r.source_type='production_requirement' AND r.source_id=q.id AND r.status
IN('active','consumed')),0)<>q.required_quantity)
 OR (v_quantity>0 AND coalesce((SELECT sum(i.quantity_issued) FROM erp.production_material_issues
i
 JOIN erp.inventory_reservations r ON r.company_id=i.company_id AND r.id=i.reservation_id
 WHERE i.company_id=q.company_id AND r.source_id=q.id),0)<>q.required_quantity))) THEN
 RAISE EXCEPTION 'MFG_MATERIAL_INCOMPLETE: Requirements must be reserved, and fully issued before
output'; END IF;
 IF o.status='completed' AND (v_quantity<>o.planned_quantity OR v_cost<>v_allocated) THEN
 RAISE EXCEPTION 'MFG_COMPLETION_INVALID: Completion requires planned output and zero WIP'; END
IF;
 IF o.status='cancelled' AND (v_quantity<>0 OR EXISTS(SELECT 1 FROM erp.production_material_issues
 i
 WHERE i.company_id=o.company_id AND i.production_order_id=o.id AND NOT EXISTS(
 SELECT 1 FROM erp.production_material_returns r WHERE r.company_id=i.company_id AND
r.material_issue_id=i.id))
 OR EXISTS(SELECT 1 FROM erp.inventory_reservations r JOIN erp.production_order_requirements q
 ON q.company_id=r.company_id AND q.id=r.source_id WHERE q.company_id=o.company_id
 AND q.production_order_id=o.id AND r.source_type='production_requirement' AND r.status='active'))
 THEN
 RAISE EXCEPTION 'MFG_CANCELLATION_INVALID: Cancellation needs complete historical returns and
released reservations'; END IF;
 FOR b IN SELECT * FROM erp.production_execution_batches WHERE company_id=o.company_id AND
production_order_id=o.id LOOP
 SELECT coalesce(sum(amount),0) INTO v_value FROM (
 SELECT cost_company amount FROM erp.production_material_issues WHERE company_id=b.company_id AND
batch_id=b.id
 UNION ALL SELECT cost_company FROM erp.production_outputs WHERE company_id=b.company_id AND
batch_id=b.id
 UNION ALL SELECT i.cost_company FROM erp.production_material_returns r JOIN
erp.production_material_issues i
 ON i.company_id=r.company_id AND i.id=r.material_issue_id WHERE r.company_id=b.company_id AND
r.batch_id=b.id) values_fact;
 IF NOT EXISTS(SELECT 1 FROM erp.stock_movements WHERE company_id=b.company_id AND
source_type='production_execution' AND source_id=b.id)
 OR EXISTS(SELECT 1 FROM erp.stock_movements m WHERE m.company_id=b.company_id AND
m.source_type='production_execution'
 AND m.source_id=b.id AND NOT EXISTS(SELECT 1 FROM erp.production_material_issues WHERE
company_id=m.company_id AND stock_movement_id=m.id)
 AND NOT EXISTS(SELECT 1 FROM erp.production_outputs WHERE company_id=m.company_id AND
stock_movement_id=m.id)
 AND NOT EXISTS(SELECT 1 FROM erp.production_material_returns WHERE company_id=m.company_id AND
stock_movement_id=m.id)) THEN
 RAISE EXCEPTION 'MFG_MOVEMENT_INVALID: Every production movement requires its execution fact';
END IF;
 IF b.currency_precision<>v_precision OR round(v_value,v_precision)<>v_value
 OR (v_value=0 AND b.journal_entry_id IS NOT NULL)
 OR (v_value>0 AND NOT EXISTS(SELECT 1 FROM erp.journal_entries j WHERE j.company_id=b.company_id
 AND j.id=b.journal_entry_id AND j.status='posted' AND j.source_type='production_execution'
 AND j.source_id=b.id AND j.entry_date=b.posting_date)) THEN
 RAISE EXCEPTION 'MFG_JOURNAL_INVALID: Production requires a posted source-linked journal'; END
IF;
 IF EXISTS(WITH expected AS (
 SELECT inventory_account_id_snapshot account_id,-cost_company amount FROM
erp.production_material_issues
 WHERE company_id=b.company_id AND batch_id=b.id
 UNION ALL SELECT o.wip_account_id_snapshot,cost_company FROM erp.production_material_issues WHERE
 company_id=b.company_id AND batch_id=b.id
 UNION ALL SELECT o.output_inventory_account_id_snapshot,cost_company FROM erp.production_outputs
WHERE company_id=b.company_id AND batch_id=b.id
 UNION ALL SELECT o.wip_account_id_snapshot,-cost_company FROM erp.production_outputs WHERE
company_id=b.company_id AND batch_id=b.id
 UNION ALL SELECT i.inventory_account_id_snapshot,i.cost_company FROM
erp.production_material_returns r
 JOIN erp.production_material_issues i ON i.company_id=r.company_id AND i.id=r.material_issue_id
WHERE r.company_id=b.company_id AND r.batch_id=b.id
 UNION ALL SELECT o.wip_account_id_snapshot,-i.cost_company FROM erp.production_material_returns r
 JOIN erp.production_material_issues i ON i.company_id=r.company_id AND i.id=r.material_issue_id
WHERE r.company_id=b.company_id AND r.batch_id=b.id),
 e AS(SELECT account_id,sum(amount) amount FROM expected GROUP BY account_id),
 a AS(SELECT account_id,sum(debit_amount-credit_amount) amount FROM erp.journal_lines WHERE
company_id=b.company_id
 AND journal_entry_id=b.journal_entry_id GROUP BY account_id)
 SELECT 1 FROM e FULL JOIN a USING(account_id) WHERE coalesce(e.amount,0)<>coalesce(a.amount,0))
THEN
 RAISE EXCEPTION 'MFG_JOURNAL_INVALID: WIP and inventory journal amounts differ'; END IF;
 IF EXISTS(SELECT 1 FROM erp.production_material_issues i WHERE i.company_id=b.company_id AND
i.batch_id=b.id
 AND NOT EXISTS(SELECT 1 FROM erp.inventory_cost_basis_snapshots s WHERE s.company_id=i.company_id
 AND s.movement_id=i.stock_movement_id)) THEN
 RAISE EXCEPTION 'MFG_COST_BASIS_REQUIRED: Material issues require retained average cost'; END IF;
 END LOOP;
 RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER trg_production_execution_complete AFTER INSERT OR UPDATE ON
erp.production_orders
 DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION
erp.check_production_execution_complete();
CREATE CONSTRAINT TRIGGER trg_production_batch_complete AFTER INSERT ON
erp.production_execution_batches
 DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION
erp.check_production_execution_complete();
CREATE CONSTRAINT TRIGGER trg_production_movement_complete AFTER INSERT ON erp.stock_movements
 DEFERRABLE INITIALLY DEFERRED FOR EACH ROW WHEN(NEW.source_type='production_execution')
 EXECUTE FUNCTION erp.check_production_execution_complete();
INSERT INTO identity.permissions(code,description) VALUES
 ('manufacturing.order.release','Release production and reserve material'),
 ('manufacturing.order.cancel','Cancel execution and return materials') ON CONFLICT DO NOTHING;
"""

execution_branch = r"""
 IF TG_OP='UPDATE' AND OLD.status='planned' AND NEW.status IN('planned','cancelled') AND
 (NEW.wip_account_id_snapshot IS DISTINCT FROM OLD.wip_account_id_snapshot
 OR NEW.output_inventory_account_id_snapshot IS DISTINCT FROM
OLD.output_inventory_account_id_snapshot
 OR NEW.execution_edit_xid IS DISTINCT FROM OLD.execution_edit_xid
 OR NEW.released_at IS DISTINCT FROM OLD.released_at OR NEW.released_by IS DISTINCT FROM
OLD.released_by
 OR NEW.completed_at IS DISTINCT FROM OLD.completed_at) THEN
 RAISE EXCEPTION 'MFG_ORDER_STATE_INVALID: Execution metadata is server-owned'; END IF;
 IF TG_OP='INSERT' AND (NEW.wip_account_id_snapshot IS NOT NULL OR NEW.released_at IS NOT NULL
 OR NEW.execution_edit_xid IS NOT NULL OR NEW.output_inventory_account_id_snapshot IS NOT NULL
 OR NEW.completed_at IS NOT NULL) THEN RAISE EXCEPTION 'MFG_ORDER_STATE_INVALID: Execution
metadata is server-owned'; END IF;
 IF TG_OP='UPDATE' AND (OLD.status<>'planned' OR NEW.status
IN('released','in_progress','completed')) THEN
 IF OLD.status IN('completed','cancelled') OR NEW.execution_edit_xid IS DISTINCT FROM
pg_current_xact_id()
 OR
(to_jsonb(NEW)-ARRAY['status','completed_quantity','row_version','updated_at','execution_edit_xid',

'wip_account_id_snapshot','output_inventory_account_id_snapshot','released_at','released_by','completed_at',
 'cancelled_at','cancelled_by','cancellation_reason']) IS DISTINCT FROM

(to_jsonb(OLD)-ARRAY['status','completed_quantity','row_version','updated_at','execution_edit_xid',

'wip_account_id_snapshot','output_inventory_account_id_snapshot','released_at','released_by','completed_at',
 'cancelled_at','cancelled_by','cancellation_reason']) THEN
 RAISE EXCEPTION 'MFG_ORDER_FROZEN: Execution preserves all planning facts'; END IF;
 IF OLD.status='planned' THEN
 IF NEW.status<>'released' OR NEW.completed_quantity<>0 OR NOT EXISTS(SELECT 1 FROM erp.boms
 WHERE company_id=NEW.company_id AND id=NEW.bom_id AND status='active' AND activated_at IS NOT
NULL
 AND (effective_from IS NULL OR effective_from<=NEW.bom_effective_date)
 AND (effective_to IS NULL OR effective_to>=NEW.bom_effective_date)) THEN
 RAISE EXCEPTION 'MFG_RELEASE_INVALID: Release requires an active approved BOM'; END IF;
 IF NOT EXISTS(SELECT 1 FROM erp.accounts WHERE company_id=NEW.company_id AND
id=NEW.wip_account_id_snapshot
 AND account_type='asset' AND is_active AND allow_posting) OR NOT EXISTS(SELECT 1 FROM
erp.accounts
 WHERE company_id=NEW.company_id AND id=NEW.output_inventory_account_id_snapshot
 AND account_type='asset' AND is_active AND allow_posting)
 OR NEW.wip_account_id_snapshot=NEW.output_inventory_account_id_snapshot THEN
 RAISE EXCEPTION 'MFG_ACCOUNT_INVALID: Configure distinct posting WIP and output inventory
accounts'; END IF;
 NEW.released_at:=clock_timestamp();NEW.released_by:=identity.current_user_id();
 ELSE
 IF NEW.wip_account_id_snapshot IS DISTINCT FROM OLD.wip_account_id_snapshot
 OR NEW.output_inventory_account_id_snapshot IS DISTINCT FROM
OLD.output_inventory_account_id_snapshot
 OR NEW.released_at IS DISTINCT FROM OLD.released_at OR NEW.released_by IS DISTINCT FROM
OLD.released_by
 OR NEW.status NOT IN('released','in_progress','completed','cancelled')
 OR NEW.completed_quantity<OLD.completed_quantity THEN
 RAISE EXCEPTION 'MFG_ORDER_FROZEN: Released accounting policy is immutable'; END IF;
 END IF;
 IF NEW.status='completed' THEN NEW.completed_at:=clock_timestamp();
 ELSIF NEW.completed_at IS DISTINCT FROM OLD.completed_at THEN
 RAISE EXCEPTION 'MFG_ORDER_STATE_INVALID: Completion time is server-owned'; END IF;
 IF NEW.status='cancelled' THEN
 IF NEW.cancellation_reason IS NULL OR length(trim(NEW.cancellation_reason)) NOT BETWEEN 1 AND
2000 THEN
 RAISE EXCEPTION 'MFG_REASON_REQUIRED: Cancellation requires a reason'; END IF;
 NEW.cancelled_at:=clock_timestamp();NEW.cancelled_by:=identity.current_user_id();
 ELSE
 IF NEW.cancelled_at IS NOT NULL OR NEW.cancelled_by IS NOT NULL OR NEW.cancellation_reason IS NOT
 NULL THEN
 RAISE EXCEPTION 'MFG_ORDER_STATE_INVALID: Cancellation metadata is server-owned'; END IF;
 END IF;
 RETURN NEW;
 END IF;
"""
SQL += old_plan.replace("BEGIN\n", "BEGIN\n" + execution_branch, 1)

production_reservation = r"""
 IF TG_OP='UPDATE' AND OLD.source_type='production_requirement' THEN
 SELECT * INTO l FROM erp.production_order_requirements WHERE company_id=OLD.company_id AND
id=OLD.source_id;
 SELECT * INTO d FROM erp.production_orders WHERE company_id=l.company_id AND
id=l.production_order_id FOR UPDATE;
 IF d.status NOT IN('released','in_progress') OR d.execution_edit_xid IS DISTINCT FROM
pg_current_xact_id() THEN
 RAISE EXCEPTION 'MFG_RESERVATION_INVALID: Use the production command to consume or release'; END
IF;
 END IF;
 IF TG_OP='INSERT' AND NEW.source_type='production_requirement' THEN
 SELECT * INTO l FROM erp.production_order_requirements WHERE company_id=NEW.company_id AND
id=NEW.source_id;
 SELECT * INTO d FROM erp.production_orders WHERE company_id=l.company_id AND
id=l.production_order_id FOR UPDATE;
 IF d.status IS DISTINCT FROM 'released' OR d.execution_edit_xid IS DISTINCT FROM
pg_current_xact_id()
 OR l.component_item_id IS DISTINCT FROM NEW.item_id OR d.warehouse_id IS DISTINCT FROM
NEW.warehouse_id
 OR NEW.status<>'active' THEN RAISE EXCEPTION 'MFG_RESERVATION_INVALID: Reserve released
requirements only'; END IF;
 PERFORM 1 FROM erp.inventory_positions WHERE company_id=NEW.company_id AND
warehouse_id=NEW.warehouse_id
 AND item_id=NEW.item_id AND lot_id IS NOT DISTINCT FROM NEW.lot_id FOR UPDATE;
 SELECT coalesce(sum(quantity),0) INTO v_reserved FROM erp.inventory_reservations WHERE
company_id=NEW.company_id
 AND source_type=NEW.source_type AND source_id=NEW.source_id AND status IN('active','consumed');
 IF v_reserved+NEW.quantity>l.required_quantity THEN RAISE EXCEPTION 'MFG_RESERVATION_INVALID:
Requirement over-reserved'; END IF;
 RETURN NEW;
 END IF;
"""
SQL += old_reservation.replace("BEGIN\n", "BEGIN\n" + production_reservation, 1)
SQL += old_consumed.replace(
    "BEGIN\n",
    r"""BEGIN
 IF NEW.source_type='production_requirement' THEN
 IF NEW.status='released' AND NOT EXISTS(SELECT 1 FROM erp.production_order_requirements q
 JOIN erp.production_orders o ON o.company_id=q.company_id AND o.id=q.production_order_id
 WHERE q.company_id=NEW.company_id AND q.id=NEW.source_id AND o.status='cancelled') THEN
 RAISE EXCEPTION 'MFG_CANCELLATION_INVALID: Production reservations release only on cancellation';
 END IF;
 IF NEW.status='consumed' AND NOT EXISTS(SELECT 1 FROM erp.production_material_issues
 WHERE company_id=NEW.company_id AND reservation_id=NEW.id) THEN
 RAISE EXCEPTION 'MFG_ISSUE_REQUIRED: Consumed production reservation needs an immutable issue';
END IF;
 RETURN NULL; END IF;
""",
    1,
)

REVERSE = (
    r"""
DO $$ BEGIN IF EXISTS(SELECT 1 FROM erp.production_execution_batches)
 OR EXISTS(SELECT 1 FROM erp.production_orders WHERE released_at IS NOT NULL)
 OR EXISTS(SELECT 1 FROM erp.inventory_reservations WHERE source_type='production_requirement')
THEN
 RAISE EXCEPTION 'Retained production execution prevents rollback'; END IF; END $$;
DROP TRIGGER trg_production_execution_complete ON erp.production_orders;
DROP TRIGGER trg_production_movement_complete ON erp.stock_movements;
DROP TRIGGER trg_production_batch_complete ON erp.production_execution_batches;
DROP FUNCTION erp.check_production_execution_complete();
DROP TRIGGER trg_production_issue_history ON erp.production_material_issues;
DROP TRIGGER trg_production_output_history ON erp.production_outputs;
DROP TRIGGER trg_production_issue_audit ON erp.production_material_issues;
DROP TRIGGER trg_production_output_audit ON erp.production_outputs;
DROP INDEX erp.ix_production_issue_order,erp.ix_production_output_order;
DROP TABLE erp.production_material_returns;
ALTER TABLE erp.production_material_issues DROP COLUMN batch_id,DROP COLUMN reservation_id,
 DROP COLUMN inventory_account_id_snapshot,DROP CONSTRAINT uq_production_issue_id;
ALTER TABLE erp.production_outputs DROP COLUMN batch_id,DROP CONSTRAINT uq_production_output_id;
DROP TABLE erp.production_execution_batches;
ALTER TABLE erp.inventory_reservations DROP CONSTRAINT uq_production_reservation_id;
DROP FUNCTION erp.guard_production_execution_history();
ALTER TABLE erp.production_orders DROP COLUMN execution_edit_xid,DROP COLUMN
wip_account_id_snapshot,
 DROP COLUMN execution_policy,DROP COLUMN output_inventory_account_id_snapshot,
 DROP COLUMN released_at,DROP COLUMN
released_by,DROP COLUMN completed_at;
CREATE TRIGGER trg_production_issue_disabled BEFORE INSERT OR UPDATE OR DELETE ON
erp.production_material_issues
 FOR EACH ROW EXECUTE FUNCTION erp.reject_unimplemented_production_effect();
CREATE TRIGGER trg_production_output_disabled BEFORE INSERT OR UPDATE OR DELETE ON
erp.production_outputs
 FOR EACH ROW EXECUTE FUNCTION erp.reject_unimplemented_production_effect();
"""
    + old_plan
    + old_reservation
    + old_consumed
)


class Migration(migrations.Migration):
    dependencies = [("database", "0031_manufacturing_drafts")]
    operations = [migrations.RunSQL(SQL, REVERSE)]
