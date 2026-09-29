from django.db import migrations

SQL = r"""
DO $$ BEGIN
 IF EXISTS(SELECT 1 FROM erp.boms WHERE status<>'draft')
 OR EXISTS(SELECT 1 FROM erp.production_orders) THEN
 RAISE EXCEPTION 'Manufacturing migration needs reviewed adoption of existing approved BOMs/orders';
 END IF;
 IF EXISTS(SELECT 1 FROM erp.bom_lines GROUP BY company_id,bom_id,component_item_id
 HAVING count(*)>1) THEN RAISE EXCEPTION 'Review duplicate BOM components before adoption'; END IF;
END $$;
ALTER TABLE erp.boms
 ADD COLUMN draft_edit_xid xid8 NOT NULL DEFAULT pg_current_xact_id(),
 ADD COLUMN activated_at timestamptz,ADD COLUMN activated_by uuid REFERENCES identity.users(id),
 ADD COLUMN retired_at timestamptz,ADD COLUMN retired_by uuid REFERENCES identity.users(id),
 ADD COLUMN retirement_reason text,
 ADD CONSTRAINT ck_bom_revision_text CHECK(length(trim(revision)) BETWEEN 1 AND 100),
 ADD CONSTRAINT ck_bom_finite_quantity CHECK(output_quantity<=99999999999999.999999);
ALTER TABLE erp.bom_lines ADD CONSTRAINT uq_bom_line_company_id UNIQUE(company_id,id);
CREATE UNIQUE INDEX uq_bom_component ON erp.bom_lines(company_id,bom_id,component_item_id);
ALTER TABLE erp.bom_lines ADD CONSTRAINT ck_bom_component_finite
 CHECK(quantity_per_output<=99999999999999.999999);
ALTER TABLE erp.production_orders
 ADD COLUMN planning_edit_xid xid8 NOT NULL DEFAULT pg_current_xact_id(),
 ADD COLUMN bom_revision_snapshot text NOT NULL,
 ADD COLUMN bom_row_version_snapshot bigint NOT NULL,
 ADD COLUMN bom_output_quantity_snapshot numeric(20,6) NOT NULL,
 ADD COLUMN output_uom_id_snapshot uuid NOT NULL REFERENCES erp.uoms(id),
 ADD COLUMN bom_effective_date date NOT NULL,
 ADD COLUMN cancelled_at timestamptz,ADD COLUMN cancelled_by uuid REFERENCES identity.users(id),
 ADD COLUMN cancellation_reason text,
 ADD CONSTRAINT ck_production_number_text CHECK(length(trim(production_no)) BETWEEN 1 AND 100),
 ADD CONSTRAINT ck_production_finite_quantity CHECK(planned_quantity<=99999999999999.999999),
 ADD CONSTRAINT ck_production_completed_limit CHECK(completed_quantity<=planned_quantity);
CREATE TABLE erp.production_order_requirements(
 id uuid PRIMARY KEY DEFAULT uuidv7(),company_id uuid NOT NULL,
 production_order_id uuid NOT NULL,bom_line_id uuid NOT NULL,line_no integer NOT NULL,
 component_item_id uuid NOT NULL,component_uom_id_snapshot uuid NOT NULL REFERENCES erp.uoms(id),
 quantity_per_output_snapshot numeric(20,6) NOT NULL CHECK(quantity_per_output_snapshot>0),
 scrap_percent_snapshot numeric(9,6) NOT NULL CHECK(scrap_percent_snapshot BETWEEN 0 AND 100),
 required_quantity numeric(20,6) NOT NULL CHECK(required_quantity>0
 AND required_quantity<=99999999999999.999999),
 UNIQUE(company_id,id),UNIQUE(company_id,production_order_id,line_no),
 UNIQUE(company_id,production_order_id,bom_line_id),
 FOREIGN KEY(company_id,production_order_id) REFERENCES erp.production_orders(company_id,id),
 FOREIGN KEY(company_id,bom_line_id) REFERENCES erp.bom_lines(company_id,id),
 FOREIGN KEY(company_id,component_item_id) REFERENCES erp.items(company_id,id)
);
CREATE INDEX ix_production_requirement_component
 ON erp.production_order_requirements(company_id,component_item_id);
CREATE INDEX ix_production_requirement_bom_line
 ON erp.production_order_requirements(company_id,bom_line_id);
ALTER TABLE erp.production_order_requirements ENABLE ROW LEVEL SECURITY;
CREATE POLICY company_scope ON erp.production_order_requirements
 USING(EXISTS(SELECT 1 FROM erp.companies c WHERE c.id=company_id
 AND c.tenant_id=erp.current_tenant_id()))
 WITH CHECK(EXISTS(SELECT 1 FROM erp.companies c WHERE c.id=company_id
 AND c.tenant_id=erp.current_tenant_id()));

CREATE FUNCTION erp.validate_bom_definition(p_company uuid,p_bom uuid,p_output uuid)
RETURNS void LANGUAGE plpgsql SET search_path=pg_catalog AS $$
DECLARE n integer;
BEGIN
 SELECT count(*) INTO n FROM erp.bom_lines WHERE company_id=p_company AND bom_id=p_bom;
 IF n NOT BETWEEN 1 AND 100 THEN
 RAISE EXCEPTION 'MFG_BOM_LINES_INVALID: A BOM requires 1 to 100 component lines'; END IF;
 PERFORM 1 FROM erp.items WHERE company_id=p_company AND (id=p_output OR id IN(
 SELECT component_item_id FROM erp.bom_lines WHERE company_id=p_company AND bom_id=p_bom))
 ORDER BY id FOR SHARE;
 IF NOT EXISTS(SELECT 1 FROM erp.items WHERE company_id=p_company AND id=p_output
 AND is_active AND item_kind='stock') OR EXISTS(
 SELECT 1 FROM erp.bom_lines l JOIN erp.items i ON i.company_id=l.company_id
 AND i.id=l.component_item_id WHERE l.company_id=p_company AND l.bom_id=p_bom
 AND (NOT i.is_active OR i.item_kind<>'stock' OR i.id=p_output)) THEN
 RAISE EXCEPTION 'MFG_BOM_ITEMS_INVALID: Output and distinct components require active stock items';
 END IF;
END $$;
CREATE FUNCTION erp.guard_manufacturing_bom() RETURNS trigger LANGUAGE plpgsql
SET search_path=pg_catalog AS $$
DECLARE v_cycle boolean;v_count integer;
BEGIN
 IF TG_OP='DELETE' THEN RAISE EXCEPTION 'MFG_BOM_FROZEN: Retire BOMs instead of deleting'; END IF;
 PERFORM pg_advisory_xact_lock(hashtextextended('manufacturing-planning:'||NEW.company_id,0));
 IF TG_OP='UPDATE' THEN
 IF NEW.id<>OLD.id OR NEW.company_id<>OLD.company_id THEN
 RAISE EXCEPTION 'MFG_BOM_FROZEN: BOM identity is immutable'; END IF;
 IF OLD.status='retired' THEN
 RAISE EXCEPTION 'MFG_BOM_FROZEN: Retired revisions are immutable'; END IF;
 IF (OLD.status='active' OR NEW.status='retired') AND (
 NEW.status<>'retired' OR (to_jsonb(NEW)-ARRAY['status','row_version','updated_at',
 'retired_at','retired_by','retirement_reason']) IS DISTINCT FROM
 (to_jsonb(OLD)-ARRAY['status','row_version','updated_at',
 'retired_at','retired_by','retirement_reason'])) THEN
 RAISE EXCEPTION 'MFG_BOM_FROZEN: Approved revisions are frozen'; END IF;
 ELSE
 IF NEW.status<>'draft' THEN RAISE EXCEPTION 'MFG_BOM_STATE_INVALID: Create a draft first'; END IF;
 NEW.row_version:=1;
 END IF;
 IF NEW.status='draft' THEN
 IF NEW.activated_at IS NOT NULL OR NEW.activated_by IS NOT NULL OR NEW.retired_at IS NOT NULL
 OR NEW.retired_by IS NOT NULL OR NEW.retirement_reason IS NOT NULL THEN
 RAISE EXCEPTION 'MFG_BOM_STATE_INVALID: Draft approval metadata is server-owned'; END IF;
 NEW.draft_edit_xid:=pg_current_xact_id();
 ELSIF NEW.status='active' THEN
 PERFORM erp.validate_bom_definition(NEW.company_id,NEW.id,NEW.output_item_id);
 SELECT count(*) INTO v_count FROM (
 SELECT 1 FROM erp.bom_lines WHERE company_id=NEW.company_id LIMIT 10001) x;
 IF v_count>10000 THEN RAISE EXCEPTION 'MFG_GRAPH_JOB_REQUIRED: BOM graph exceeds limit'; END IF;
 WITH RECURSIVE edges AS (
 SELECT b.output_item_id,l.component_item_id FROM erp.boms b JOIN erp.bom_lines l
 ON l.company_id=b.company_id AND l.bom_id=b.id WHERE b.company_id=NEW.company_id
 AND b.status='active' AND b.id<>NEW.id
 UNION SELECT NEW.output_item_id,l.component_item_id FROM erp.bom_lines l
 WHERE l.company_id=NEW.company_id AND l.bom_id=NEW.id
 ), reachable(item_id) AS (
 SELECT component_item_id FROM edges WHERE output_item_id=NEW.output_item_id
 UNION SELECT e.component_item_id FROM reachable r JOIN edges e ON e.output_item_id=r.item_id
 ) SELECT EXISTS(SELECT 1 FROM reachable WHERE item_id=NEW.output_item_id) INTO v_cycle;
 IF v_cycle THEN
 RAISE EXCEPTION 'MFG_BOM_CYCLE: Active BOM definitions cannot form a cycle'; END IF;
 NEW.activated_at:=clock_timestamp();NEW.activated_by:=identity.current_user_id();
 IF NEW.retired_at IS NOT NULL OR NEW.retired_by IS NOT NULL
 OR NEW.retirement_reason IS NOT NULL THEN
 RAISE EXCEPTION 'MFG_BOM_STATE_INVALID: Activation cannot include retirement metadata'; END IF;
 ELSIF NEW.status='retired' THEN
 IF NEW.retirement_reason IS NULL OR length(trim(NEW.retirement_reason)) NOT BETWEEN 1 AND 2000 THEN
 RAISE EXCEPTION 'MFG_REASON_REQUIRED: Retirement requires a reason'; END IF;
 NEW.retired_at:=clock_timestamp();NEW.retired_by:=identity.current_user_id();
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER trg_00_manufacturing_bom BEFORE INSERT OR UPDATE OR DELETE ON erp.boms
 FOR EACH ROW EXECUTE FUNCTION erp.guard_manufacturing_bom();
CREATE FUNCTION erp.guard_manufacturing_bom_line() RETURNS trigger LANGUAGE plpgsql
SET search_path=pg_catalog AS $$
DECLARE b record;v_company uuid;v_bom uuid;
BEGIN
 IF TG_OP='DELETE' THEN v_company:=OLD.company_id;v_bom:=OLD.bom_id;
 ELSE v_company:=NEW.company_id;v_bom:=NEW.bom_id; END IF;
 IF TG_OP='UPDATE' AND (NEW.id<>OLD.id OR NEW.company_id<>OLD.company_id
 OR NEW.bom_id<>OLD.bom_id) THEN
 RAISE EXCEPTION 'MFG_BOM_FROZEN: Component identity is immutable'; END IF;
 PERFORM pg_advisory_xact_lock(hashtextextended('manufacturing-planning:'||v_company,0));
 SELECT * INTO b FROM erp.boms WHERE company_id=v_company AND id=v_bom FOR UPDATE;
 IF b.status IS DISTINCT FROM 'draft' OR b.draft_edit_xid IS DISTINCT FROM pg_current_xact_id() THEN
 RAISE EXCEPTION 'MFG_BOM_FROZEN: Components require a revisioned draft aggregate edit'; END IF;
 IF TG_OP='DELETE' THEN RETURN OLD; END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER trg_manufacturing_bom_line BEFORE INSERT OR UPDATE OR DELETE ON erp.bom_lines
 FOR EACH ROW EXECUTE FUNCTION erp.guard_manufacturing_bom_line();
CREATE FUNCTION erp.check_manufacturing_bom_complete() RETURNS trigger LANGUAGE plpgsql
SET search_path=pg_catalog AS $$
DECLARE b record;v_company uuid;v_id uuid;
BEGIN
 IF TG_TABLE_NAME='boms' THEN v_company:=NEW.company_id;v_id:=NEW.id;
 ELSIF TG_OP='DELETE' THEN v_company:=OLD.company_id;v_id:=OLD.bom_id;
 ELSE v_company:=NEW.company_id;v_id:=NEW.bom_id; END IF;
 SELECT * INTO b FROM erp.boms WHERE company_id=v_company AND id=v_id;
 IF FOUND THEN PERFORM erp.validate_bom_definition(v_company,v_id,b.output_item_id); END IF;
 RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER trg_manufacturing_bom_complete AFTER INSERT OR UPDATE ON erp.boms
 DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION erp.check_manufacturing_bom_complete();
CREATE CONSTRAINT TRIGGER trg_manufacturing_bom_lines_complete
 AFTER INSERT OR UPDATE OR DELETE ON erp.bom_lines DEFERRABLE INITIALLY DEFERRED
 FOR EACH ROW EXECUTE FUNCTION erp.check_manufacturing_bom_complete();

CREATE FUNCTION erp.guard_production_plan() RETURNS trigger LANGUAGE plpgsql
SET search_path=pg_catalog AS $$
DECLARE b record;v_date date;
BEGIN
 IF TG_OP='DELETE' THEN
 RAISE EXCEPTION 'MFG_ORDER_FROZEN: Cancel plans instead of deleting'; END IF;
 PERFORM pg_advisory_xact_lock(hashtextextended('manufacturing-planning:'||NEW.company_id,0));
 IF TG_OP='UPDATE' THEN
 IF NEW.id<>OLD.id OR NEW.company_id<>OLD.company_id OR OLD.status<>'planned' THEN
 RAISE EXCEPTION 'MFG_ORDER_FROZEN: Only planned orders can be edited'; END IF;
 IF NEW.status='cancelled' THEN
 IF (to_jsonb(NEW)-ARRAY['status','row_version','updated_at','cancelled_at','cancelled_by',
 'cancellation_reason']) IS DISTINCT FROM (to_jsonb(OLD)-ARRAY['status','row_version',
 'updated_at','cancelled_at','cancelled_by','cancellation_reason'])
 OR NEW.cancellation_reason IS NULL
 OR length(trim(NEW.cancellation_reason)) NOT BETWEEN 1 AND 2000 THEN
 RAISE EXCEPTION 'MFG_REASON_REQUIRED: Cancellation preserves planning facts and needs a reason';
 END IF;
 NEW.cancelled_at:=clock_timestamp();NEW.cancelled_by:=identity.current_user_id();
 RETURN NEW;
 END IF;
 ELSE NEW.row_version:=1;
 END IF;
 IF NEW.status<>'planned' OR NEW.completed_quantity<>0 THEN
 RAISE EXCEPTION 'MFG_EXECUTION_NOT_AVAILABLE: Production execution belongs to P5.11'; END IF;
 IF NEW.cancelled_at IS NOT NULL OR NEW.cancelled_by IS NOT NULL
 OR NEW.cancellation_reason IS NOT NULL THEN
 RAISE EXCEPTION 'MFG_ORDER_STATE_INVALID: Cancellation metadata is server-owned'; END IF;
 SELECT * INTO b FROM erp.boms WHERE company_id=NEW.company_id AND id=NEW.bom_id FOR SHARE;
 v_date:=coalesce(NEW.planned_start,(clock_timestamp() AT TIME ZONE 'UTC')::date);
 IF b.status IS DISTINCT FROM 'active' OR b.activated_at IS NULL
 OR (b.effective_from IS NOT NULL AND v_date<b.effective_from)
 OR (b.effective_to IS NOT NULL AND v_date>b.effective_to) THEN
 RAISE EXCEPTION 'MFG_BOM_NOT_SELECTABLE: Select an active approved BOM effective at start';
 END IF;
 PERFORM erp.validate_bom_definition(NEW.company_id,b.id,b.output_item_id);
 PERFORM 1 FROM erp.warehouses WHERE company_id=NEW.company_id AND id=NEW.warehouse_id FOR SHARE;
 IF NOT EXISTS(SELECT 1 FROM erp.warehouses WHERE company_id=NEW.company_id
 AND id=NEW.warehouse_id AND is_active AND stock_category='sellable') THEN
 RAISE EXCEPTION 'MFG_WAREHOUSE_INVALID: Planning requires an active sellable warehouse'; END IF;
 IF EXISTS(SELECT 1 FROM erp.items WHERE company_id=NEW.company_id AND id=b.output_item_id
 AND track_serials AND NEW.planned_quantity<>trunc(NEW.planned_quantity)) THEN
 RAISE EXCEPTION 'MFG_SERIAL_QUANTITY_INVALID: Serial output plans require whole units'; END IF;
 NEW.output_item_id:=b.output_item_id;NEW.bom_revision_snapshot:=b.revision;
 NEW.bom_row_version_snapshot:=b.row_version;NEW.bom_output_quantity_snapshot:=b.output_quantity;
 SELECT base_uom_id INTO NEW.output_uom_id_snapshot FROM erp.items
 WHERE company_id=NEW.company_id AND id=b.output_item_id;
 NEW.bom_effective_date:=v_date;NEW.planning_edit_xid:=pg_current_xact_id();
 PERFORM 1 FROM erp.inventory_positions WHERE company_id=NEW.company_id
 AND warehouse_id=NEW.warehouse_id AND item_id IN(SELECT component_item_id FROM erp.bom_lines
 WHERE company_id=NEW.company_id AND bom_id=b.id) ORDER BY warehouse_id,item_id,lot_id FOR SHARE;
 IF EXISTS(SELECT 1 FROM erp.bom_lines l WHERE l.company_id=NEW.company_id AND l.bom_id=b.id
 AND l.quantity_per_output*NEW.planned_quantity*(100+l.scrap_percent)>
 coalesce((SELECT sum(v.available_quantity) FROM erp.v_inventory_availability v
 WHERE v.company_id=NEW.company_id AND v.warehouse_id=NEW.warehouse_id
 AND v.item_id=l.component_item_id),0)*b.output_quantity*100) THEN
 RAISE EXCEPTION 'MFG_MATERIAL_UNAVAILABLE: Current unreserved material does not cover this plan';
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER trg_00_production_plan BEFORE INSERT OR UPDATE OR DELETE ON erp.production_orders
 FOR EACH ROW EXECUTE FUNCTION erp.guard_production_plan();
CREATE FUNCTION erp.guard_production_requirement() RETURNS trigger LANGUAGE plpgsql
SET search_path=pg_catalog AS $$
DECLARE o record;l record;i record;v_company uuid;v_order uuid;
BEGIN
 IF TG_OP='DELETE' THEN v_company:=OLD.company_id;v_order:=OLD.production_order_id;
 ELSE v_company:=NEW.company_id;v_order:=NEW.production_order_id; END IF;
 SELECT * INTO o FROM erp.production_orders WHERE company_id=v_company AND id=v_order FOR UPDATE;
 IF o.status IS DISTINCT FROM 'planned' OR o.planning_edit_xid IS DISTINCT FROM pg_current_xact_id()
 THEN RAISE EXCEPTION 'MFG_ORDER_FROZEN: Requirements need a revisioned planned aggregate edit';
 END IF;
 IF TG_OP='DELETE' THEN RETURN OLD; END IF;
 IF TG_OP='UPDATE' AND (NEW.id<>OLD.id OR NEW.company_id<>OLD.company_id
 OR NEW.production_order_id<>OLD.production_order_id) THEN
 RAISE EXCEPTION 'MFG_ORDER_FROZEN: Requirement identity is immutable'; END IF;
 SELECT * INTO l FROM erp.bom_lines WHERE company_id=v_company AND id=NEW.bom_line_id;
 SELECT * INTO i FROM erp.items WHERE company_id=v_company AND id=NEW.component_item_id;
 IF l.bom_id IS DISTINCT FROM o.bom_id OR l.line_no IS DISTINCT FROM NEW.line_no
 OR l.component_item_id IS DISTINCT FROM NEW.component_item_id
 OR i.base_uom_id IS DISTINCT FROM NEW.component_uom_id_snapshot
 OR l.quantity_per_output IS DISTINCT FROM NEW.quantity_per_output_snapshot
 OR l.scrap_percent IS DISTINCT FROM NEW.scrap_percent_snapshot
 OR NEW.required_quantity*o.bom_output_quantity_snapshot*100<>
 o.planned_quantity*l.quantity_per_output*(100+l.scrap_percent)
 OR (i.track_serials AND NEW.required_quantity<>trunc(NEW.required_quantity)) THEN
 RAISE EXCEPTION 'MFG_REQUIREMENT_INVALID: Requirements must exactly match the selected BOM';
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER trg_production_requirement BEFORE INSERT OR UPDATE OR DELETE
 ON erp.production_order_requirements FOR EACH ROW
 EXECUTE FUNCTION erp.guard_production_requirement();
CREATE FUNCTION erp.check_production_plan_complete() RETURNS trigger LANGUAGE plpgsql
SET search_path=pg_catalog AS $$
DECLARE o record;b record;v_id uuid;v_company uuid;v_count integer;v_expected integer;
BEGIN
 IF TG_TABLE_NAME='production_orders' THEN v_id:=NEW.id;v_company:=NEW.company_id;
 ELSIF TG_OP='DELETE' THEN v_id:=OLD.production_order_id;v_company:=OLD.company_id;
 ELSE v_id:=NEW.production_order_id;v_company:=NEW.company_id; END IF;
 SELECT * INTO o FROM erp.production_orders WHERE company_id=v_company AND id=v_id;
 IF NOT FOUND THEN RETURN NULL; END IF;
 SELECT * INTO b FROM erp.boms WHERE company_id=v_company AND id=o.bom_id;
 SELECT count(*) INTO v_expected FROM erp.bom_lines WHERE company_id=v_company AND bom_id=o.bom_id;
 SELECT count(*) INTO v_count FROM erp.production_order_requirements
 WHERE company_id=v_company AND production_order_id=v_id;
 IF v_expected NOT BETWEEN 1 AND 100 OR v_count<>v_expected
 OR o.output_item_id IS DISTINCT FROM b.output_item_id
 OR o.bom_revision_snapshot IS DISTINCT FROM b.revision
 OR o.bom_output_quantity_snapshot IS DISTINCT FROM b.output_quantity
 OR EXISTS(SELECT 1 FROM erp.production_order_requirements r JOIN erp.bom_lines l
 ON l.company_id=r.company_id AND l.id=r.bom_line_id
 WHERE r.company_id=v_company AND r.production_order_id=v_id AND (
 l.bom_id<>o.bom_id OR l.line_no<>r.line_no OR l.component_item_id<>r.component_item_id
 OR l.quantity_per_output<>r.quantity_per_output_snapshot
 OR l.scrap_percent<>r.scrap_percent_snapshot
 OR r.required_quantity*o.bom_output_quantity_snapshot*100<>
 o.planned_quantity*l.quantity_per_output*(100+l.scrap_percent))) THEN
 RAISE EXCEPTION 'MFG_PLAN_INCOMPLETE: Plans require complete retained material requirements';
 END IF;
 RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER trg_production_plan_complete
 AFTER INSERT OR UPDATE ON erp.production_orders
 DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION erp.check_production_plan_complete();
CREATE CONSTRAINT TRIGGER trg_production_requirements_complete AFTER INSERT OR UPDATE OR DELETE
 ON erp.production_order_requirements DEFERRABLE INITIALLY DEFERRED
 FOR EACH ROW EXECUTE FUNCTION erp.check_production_plan_complete();
CREATE FUNCTION erp.guard_manufacturing_item_definition() RETURNS trigger LANGUAGE plpgsql
SET search_path=pg_catalog AS $$
BEGIN
 IF (NEW.item_kind IS DISTINCT FROM OLD.item_kind
 OR NEW.base_uom_id IS DISTINCT FROM OLD.base_uom_id
 OR NEW.track_lots IS DISTINCT FROM OLD.track_lots
 OR NEW.track_serials IS DISTINCT FROM OLD.track_serials
 OR (OLD.is_active AND NOT NEW.is_active)) AND (
 EXISTS(SELECT 1 FROM erp.boms b WHERE b.company_id=OLD.company_id AND b.status='active'
 AND (b.output_item_id=OLD.id OR EXISTS(SELECT 1 FROM erp.bom_lines l
 WHERE l.company_id=b.company_id
 AND l.bom_id=b.id AND l.component_item_id=OLD.id))) OR
 EXISTS(SELECT 1 FROM erp.production_orders o WHERE o.company_id=OLD.company_id
 AND o.status<>'cancelled'
 AND (o.output_item_id=OLD.id OR EXISTS(SELECT 1 FROM erp.production_order_requirements r
 WHERE r.company_id=o.company_id AND r.production_order_id=o.id
 AND r.component_item_id=OLD.id)))) THEN
 RAISE EXCEPTION 'MFG_ITEM_REFERENCED: Approved BOMs and production plans retain item definitions';
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER trg_manufacturing_item_definition BEFORE UPDATE ON erp.items
 FOR EACH ROW EXECUTE FUNCTION erp.guard_manufacturing_item_definition();
CREATE FUNCTION erp.reject_unimplemented_production_effect() RETURNS trigger LANGUAGE plpgsql
SET search_path=pg_catalog AS $$
BEGIN RAISE EXCEPTION 'MFG_EXECUTION_NOT_AVAILABLE: Production execution belongs to P5.11'; END $$;
CREATE TRIGGER trg_production_issue_disabled BEFORE INSERT OR UPDATE OR DELETE
 ON erp.production_material_issues FOR EACH ROW
 EXECUTE FUNCTION erp.reject_unimplemented_production_effect();
CREATE TRIGGER trg_production_output_disabled BEFORE INSERT OR UPDATE OR DELETE
 ON erp.production_outputs FOR EACH ROW
 EXECUTE FUNCTION erp.reject_unimplemented_production_effect();
CREATE FUNCTION erp.audit_manufacturing_plan() RETURNS trigger LANGUAGE plpgsql
SECURITY DEFINER SET search_path=pg_catalog AS $$
DECLARE v_company uuid;v_id uuid;v_tenant uuid;
BEGIN
 IF TG_OP='DELETE' THEN v_company:=OLD.company_id;v_id:=OLD.id;
 ELSE v_company:=NEW.company_id;v_id:=NEW.id; END IF;
 SELECT tenant_id INTO STRICT v_tenant FROM erp.companies WHERE id=v_company;
 IF v_tenant IS DISTINCT FROM erp.current_tenant_id() THEN
 RAISE EXCEPTION 'Manufacturing audit tenant context mismatch' USING ERRCODE='42501'; END IF;
 INSERT INTO erp.company_audit_events(tenant_id,company_id,actor_user_id,actor_service_id,
 action,object_type,object_id,old_data,new_data,request_id)
 VALUES(v_tenant,v_company,identity.current_user_id(),identity.current_service_id(),
 'manufacturing.'||TG_TABLE_NAME||'.'||lower(TG_OP),TG_TABLE_NAME,v_id::text,
 CASE WHEN TG_OP<>'INSERT' THEN to_jsonb(OLD) END,
 CASE WHEN TG_OP<>'DELETE' THEN to_jsonb(NEW) END,
 nullif(current_setting('app.request_id',true),''));
 IF TG_OP='DELETE' THEN RETURN OLD; END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER trg_manufacturing_bom_audit AFTER INSERT OR UPDATE ON erp.boms
 FOR EACH ROW EXECUTE FUNCTION erp.audit_manufacturing_plan();
CREATE TRIGGER trg_manufacturing_bom_line_audit AFTER INSERT OR UPDATE OR DELETE ON erp.bom_lines
 FOR EACH ROW EXECUTE FUNCTION erp.audit_manufacturing_plan();
CREATE TRIGGER trg_production_plan_audit AFTER INSERT OR UPDATE ON erp.production_orders
 FOR EACH ROW EXECUTE FUNCTION erp.audit_manufacturing_plan();
CREATE TRIGGER trg_production_requirement_audit AFTER INSERT OR UPDATE OR DELETE
 ON erp.production_order_requirements FOR EACH ROW EXECUTE FUNCTION erp.audit_manufacturing_plan();
INSERT INTO identity.permissions(code,description) VALUES
 ('manufacturing.bom.view','View company BOM revisions'),
 ('manufacturing.bom.manage','Create, edit and retire BOM revisions'),
 ('manufacturing.bom.activate','Approve and activate BOM revisions'),
 ('manufacturing.order.view','View production plans'),
 ('manufacturing.order.manage','Create, edit and cancel production plans') ON CONFLICT DO NOTHING;
"""

REVERSE = r"""
DO $$ BEGIN
 IF EXISTS(SELECT 1 FROM erp.boms WHERE status<>'draft')
 OR EXISTS(SELECT 1 FROM erp.production_orders)
 OR EXISTS(SELECT 1 FROM erp.production_order_requirements) THEN
 RAISE EXCEPTION 'Cannot reverse retained manufacturing approvals or planning snapshots'; END IF;
END $$;
DROP TRIGGER trg_manufacturing_bom_audit ON erp.boms;
DROP TRIGGER trg_manufacturing_bom_line_audit ON erp.bom_lines;
DROP TRIGGER trg_production_plan_audit ON erp.production_orders;
DROP TRIGGER trg_production_requirement_audit ON erp.production_order_requirements;
DROP FUNCTION erp.audit_manufacturing_plan();
DROP TRIGGER trg_production_issue_disabled ON erp.production_material_issues;
DROP TRIGGER trg_production_output_disabled ON erp.production_outputs;
DROP FUNCTION erp.reject_unimplemented_production_effect();
DROP TRIGGER trg_manufacturing_item_definition ON erp.items;
DROP FUNCTION erp.guard_manufacturing_item_definition();
DROP TRIGGER trg_production_plan_complete ON erp.production_orders;
DROP TRIGGER trg_production_requirements_complete ON erp.production_order_requirements;
DROP FUNCTION erp.check_production_plan_complete();
DROP TRIGGER trg_production_requirement ON erp.production_order_requirements;
DROP FUNCTION erp.guard_production_requirement();
DROP TRIGGER trg_00_production_plan ON erp.production_orders;
DROP FUNCTION erp.guard_production_plan();
DROP TABLE erp.production_order_requirements;
ALTER TABLE erp.production_orders DROP COLUMN planning_edit_xid,DROP COLUMN bom_revision_snapshot,
 DROP COLUMN bom_row_version_snapshot,DROP COLUMN bom_output_quantity_snapshot,
 DROP COLUMN output_uom_id_snapshot,DROP COLUMN bom_effective_date,DROP COLUMN cancelled_at,
 DROP COLUMN cancelled_by,DROP COLUMN cancellation_reason,
 DROP CONSTRAINT ck_production_number_text,DROP CONSTRAINT ck_production_finite_quantity,
 DROP CONSTRAINT ck_production_completed_limit;
DROP TRIGGER trg_manufacturing_bom_complete ON erp.boms;
DROP TRIGGER trg_manufacturing_bom_lines_complete ON erp.bom_lines;
DROP FUNCTION erp.check_manufacturing_bom_complete();
DROP TRIGGER trg_manufacturing_bom_line ON erp.bom_lines;
DROP FUNCTION erp.guard_manufacturing_bom_line();
DROP TRIGGER trg_00_manufacturing_bom ON erp.boms;
DROP FUNCTION erp.guard_manufacturing_bom(),erp.validate_bom_definition(uuid,uuid,uuid);
DROP INDEX erp.uq_bom_component;
ALTER TABLE erp.bom_lines DROP CONSTRAINT uq_bom_line_company_id,
 DROP CONSTRAINT ck_bom_component_finite;
ALTER TABLE erp.boms DROP COLUMN draft_edit_xid,DROP COLUMN activated_at,DROP COLUMN activated_by,
 DROP COLUMN retired_at,DROP COLUMN retired_by,DROP COLUMN retirement_reason,
 DROP CONSTRAINT ck_bom_revision_text,DROP CONSTRAINT ck_bom_finite_quantity;
"""


class Migration(migrations.Migration):
    dependencies = [("database", "0030_checkpoint_scope_rebuild")]
    operations = [migrations.RunSQL(SQL, REVERSE)]
