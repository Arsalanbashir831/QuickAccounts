from django.db import migrations

SQL = r"""
ALTER TABLE erp.warehouses ADD COLUMN operational_role text CHECK(operational_role IN
 ('sellable','inspection','repair','dead_stock','clearance','supplier_return','scrap'));
CREATE FUNCTION erp.guard_warehouse_role() RETURNS trigger LANGUAGE plpgsql
SET search_path=pg_catalog,erp AS $$
BEGIN
 IF NEW.operational_role IS NOT NULL AND NEW.stock_category IS DISTINCT FROM
 (CASE NEW.operational_role WHEN 'sellable' THEN 'sellable' WHEN 'clearance' THEN 'sellable'
 WHEN 'inspection' THEN 'quarantine' WHEN 'supplier_return' THEN 'supplier_return'
 ELSE 'damaged' END) THEN RAISE EXCEPTION 'Warehouse role/category mismatch'; END IF;
 IF TG_OP='UPDATE' AND NEW.operational_role IS DISTINCT FROM OLD.operational_role AND
 EXISTS(SELECT 1 FROM erp.stock_movements WHERE company_id=OLD.company_id AND warehouse_id=OLD.id)
 THEN RAISE EXCEPTION 'Move stock; do not relabel warehouse role history'; END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER trg_warehouse_role BEFORE INSERT OR UPDATE ON erp.warehouses
 FOR EACH ROW EXECUTE FUNCTION erp.guard_warehouse_role();

CREATE TABLE erp.return_custody_intakes(
 id uuid PRIMARY KEY DEFAULT uuidv7(),company_id uuid NOT NULL, intake_no text NOT NULL
 CHECK(length(btrim(intake_no))>0),item_id uuid NOT NULL,warehouse_id uuid NOT NULL,
 quantity numeric(20,6) NOT NULL CHECK(quantity>0),received_date date NOT NULL,
 reason text NOT NULL CHECK(length(btrim(reason))>0),customer_reference text,
 batch_serial_reference text, status text NOT NULL DEFAULT 'received'
 CHECK(status IN ('received','inspected','matched','rejected')),
 condition text CHECK(condition IN ('resellable','damaged','unusable')),
 requested_resolution text CHECK(requested_resolution IN ('bill_back','replacement','credit')),
 inspection_notes text,sales_return_line_id uuid,
 row_version bigint NOT NULL DEFAULT 1 CHECK(row_version>0),created_at timestamptz NOT NULL
 DEFAULT clock_timestamp(), UNIQUE(company_id,id),UNIQUE(company_id,intake_no),
 FOREIGN KEY(company_id,item_id) REFERENCES erp.items(company_id,id),
 FOREIGN KEY(company_id,warehouse_id) REFERENCES erp.warehouses(company_id,id),
 FOREIGN KEY(company_id,sales_return_line_id) REFERENCES erp.sales_return_lines(company_id,id),
 CHECK((status='matched')=(sales_return_line_id IS NOT NULL))
);
CREATE INDEX ix_custody_return_line ON erp.return_custody_intakes(company_id,sales_return_line_id);
CREATE INDEX ix_custody_warehouse ON erp.return_custody_intakes(company_id,warehouse_id,id);
ALTER TABLE erp.return_custody_intakes ENABLE ROW LEVEL SECURITY;
CREATE POLICY company_scope ON erp.return_custody_intakes USING(EXISTS(SELECT 1
 FROM erp.companies c WHERE c.id=company_id AND c.tenant_id=erp.current_tenant_id()))
 WITH CHECK(EXISTS(SELECT 1 FROM erp.companies c WHERE c.id=company_id
 AND c.tenant_id=erp.current_tenant_id()));
CREATE FUNCTION erp.guard_return_custody() RETURNS trigger LANGUAGE plpgsql
SET search_path=pg_catalog,erp,identity AS $$
DECLARE l record;r record;i record;v_quantity numeric;
BEGIN
 IF TG_OP='DELETE' THEN RAISE EXCEPTION 'Custody history cannot be deleted'; END IF;
 IF NOT identity.has_company_permission(NEW.company_id,'inventory.post') THEN
 RAISE EXCEPTION 'Custody permission denied' USING ERRCODE='42501'; END IF;
 PERFORM 1 FROM erp.items WHERE company_id=NEW.company_id AND id=NEW.item_id
 AND is_active AND item_kind='stock';
 IF NOT FOUND THEN RAISE EXCEPTION 'Custody requires an active stock item'; END IF;
 PERFORM 1 FROM erp.warehouses WHERE company_id=NEW.company_id AND id=NEW.warehouse_id
 AND is_active AND stock_category='quarantine' FOR SHARE;
 IF NOT FOUND THEN RAISE EXCEPTION 'Custody requires an inspection warehouse'; END IF;
 IF TG_OP='INSERT' THEN
 IF NEW.status<>'received' THEN RAISE EXCEPTION 'Custody must start received'; END IF;
 ELSE
 IF OLD.status IN ('matched','rejected') OR
 (NEW.id,NEW.company_id,NEW.item_id,NEW.warehouse_id,NEW.quantity,NEW.received_date,NEW.intake_no)
 IS DISTINCT FROM (OLD.id,OLD.company_id,OLD.item_id,OLD.warehouse_id,OLD.quantity,
 OLD.received_date,OLD.intake_no) THEN
 RAISE EXCEPTION 'Custody source/final state immutable'; END IF;
 IF NOT ((OLD.status='received' AND NEW.status IN ('inspected','rejected')) OR
 (OLD.status='inspected' AND NEW.status IN ('matched','rejected'))) THEN
 RAISE EXCEPTION 'Invalid custody transition'; END IF;
 NEW.row_version:=OLD.row_version+1;
 END IF;
 IF NEW.status IN ('inspected','matched') AND (NEW.condition IS NULL OR
 NEW.requested_resolution IS NULL OR coalesce(length(btrim(NEW.inspection_notes)),0)=0) THEN
 RAISE EXCEPTION 'Custody requires an explicit inspection and resolution'; END IF;
 IF NEW.status='matched' THEN
 SELECT * INTO l FROM erp.sales_return_lines WHERE company_id=NEW.company_id
 AND id=NEW.sales_return_line_id FOR UPDATE;
 SELECT * INTO r FROM erp.sales_returns WHERE company_id=NEW.company_id AND id=l.sales_return_id;
 SELECT * INTO i FROM erp.sales_invoice_lines WHERE company_id=NEW.company_id
 AND id=l.sales_invoice_line_id;
 IF r.kind IS DISTINCT FROM 'return' OR r.status NOT IN ('draft','inspected')
 OR i.item_id IS DISTINCT FROM NEW.item_id OR NEW.received_date<
 (SELECT issue_date FROM erp.sales_invoices WHERE company_id=i.company_id AND id=i.sales_invoice_id)
 OR NEW.received_date>r.return_date THEN
 RAISE EXCEPTION 'Custody does not match verified sale'; END IF;
 SELECT coalesce(sum(quantity),0) INTO v_quantity FROM erp.return_custody_intakes
 WHERE company_id=NEW.company_id AND sales_return_line_id=l.id AND status='matched';
 IF v_quantity+NEW.quantity>l.quantity THEN
 RAISE EXCEPTION 'Custody exceeds return quantity'; END IF;
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER trg_custody_guard BEFORE INSERT OR UPDATE OR DELETE ON erp.return_custody_intakes
 FOR EACH ROW EXECUTE FUNCTION erp.guard_return_custody();
CREATE TRIGGER trg_custody_audit AFTER INSERT OR UPDATE ON erp.return_custody_intakes
 FOR EACH ROW EXECUTE FUNCTION erp.audit_sales_return();

CREATE FUNCTION erp.check_return_custody_match() RETURNS trigger LANGUAGE plpgsql
SET search_path=pg_catalog,erp AS $$
DECLARE l record;v_quantity numeric;
BEGIN
 IF NEW.status='void' AND EXISTS(SELECT 1 FROM erp.return_custody_intakes c
 JOIN erp.sales_return_lines source_line ON source_line.company_id=c.company_id
 AND source_line.id=c.sales_return_line_id WHERE source_line.company_id=NEW.company_id
 AND source_line.sales_return_id=NEW.id AND c.status='matched')
 THEN RAISE EXCEPTION 'Matched custody requires completion, not a voided return'; END IF;
 IF NEW.status='posted' THEN
 FOR l IN SELECT * FROM erp.sales_return_lines WHERE company_id=NEW.company_id
 AND sales_return_id=NEW.id LOOP
 IF EXISTS(SELECT 1 FROM erp.return_custody_intakes WHERE company_id=NEW.company_id
 AND sales_return_line_id=l.id) THEN
 SELECT sum(quantity) INTO v_quantity FROM erp.return_custody_intakes
 WHERE company_id=NEW.company_id
 AND sales_return_line_id=l.id AND status='matched';
 IF v_quantity IS DISTINCT FROM l.quantity OR EXISTS(SELECT 1 FROM erp.return_custody_intakes
 WHERE company_id=NEW.company_id AND sales_return_line_id=l.id AND condition IS DISTINCT FROM
 l.condition) OR EXISTS(SELECT 1 FROM erp.return_custody_intakes
 WHERE company_id=NEW.company_id AND sales_return_line_id=l.id
 AND warehouse_id IS DISTINCT FROM l.warehouse_id) THEN
 RAISE EXCEPTION 'Custody and return inspection do not reconcile'; END IF;
 END IF;
 END LOOP;
 END IF;
 RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER trg_return_custody_match AFTER INSERT OR UPDATE ON erp.sales_returns
 DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION erp.check_return_custody_match();

CREATE FUNCTION erp.guard_custody_warehouse() RETURNS trigger LANGUAGE plpgsql
SET search_path=pg_catalog,erp AS $$
BEGIN
 IF (NOT NEW.is_active OR NEW.stock_category IS DISTINCT FROM OLD.stock_category) AND EXISTS(
 SELECT 1 FROM erp.return_custody_intakes c LEFT JOIN erp.sales_return_lines l
 ON l.company_id=c.company_id AND l.id=c.sales_return_line_id LEFT JOIN erp.sales_returns r
 ON r.company_id=l.company_id AND r.id=l.sales_return_id WHERE c.company_id=OLD.company_id
 AND c.warehouse_id=OLD.id AND c.status<>'rejected' AND r.status IS DISTINCT FROM 'posted')
 THEN RAISE EXCEPTION 'Warehouse has unresolved custody'; END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER trg_custody_warehouse BEFORE UPDATE ON erp.warehouses
 FOR EACH ROW EXECUTE FUNCTION erp.guard_custody_warehouse();
"""

REVERSE = r"""
DO $$ BEGIN IF EXISTS(SELECT 1 FROM erp.return_custody_intakes) THEN
 RAISE EXCEPTION 'Cannot reverse retained custody intake history'; END IF; END $$;
DROP TRIGGER trg_custody_warehouse ON erp.warehouses;
DROP FUNCTION erp.guard_custody_warehouse();
DROP TRIGGER trg_return_custody_match ON erp.sales_returns;
DROP FUNCTION erp.check_return_custody_match();
DROP TABLE erp.return_custody_intakes;
DROP FUNCTION erp.guard_return_custody();
DROP TRIGGER trg_warehouse_role ON erp.warehouses;
DROP FUNCTION erp.guard_warehouse_role();
ALTER TABLE erp.warehouses DROP COLUMN operational_role;
"""


class Migration(migrations.Migration):
    dependencies = [("database", "0034_return_repairs_and_credit_targets")]
    operations = [migrations.RunSQL(SQL, REVERSE)]
