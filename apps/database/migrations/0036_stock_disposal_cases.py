from django.db import migrations

SQL = r"""
CREATE TABLE erp.stock_disposal_cases(
 id uuid PRIMARY KEY DEFAULT uuidv7(),company_id uuid NOT NULL,case_no text NOT NULL
 CHECK(length(btrim(case_no))>0),item_id uuid NOT NULL,warehouse_id uuid NOT NULL,
 quantity numeric(20,6) NOT NULL CHECK(quantity>0),reason text NOT NULL
 CHECK(length(btrim(reason))>0),sales_return_line_id uuid,
 method text NOT NULL CHECK(method IN ('repair_later','supplier_claim','liquidation','write_off')),
 status text NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','approved','executed','void')),
 approved_at timestamptz,approved_by uuid REFERENCES identity.users(id),qc_notes text,
 quality_approved_at timestamptz,quality_approved_by uuid REFERENCES identity.users(id),
 stock_document_id uuid,return_stock_action_id uuid,sales_invoice_id uuid,
 row_version bigint NOT NULL DEFAULT 1 CHECK(row_version>0),created_at timestamptz NOT NULL
 DEFAULT clock_timestamp(),UNIQUE(company_id,id),UNIQUE(company_id,case_no),
 UNIQUE(company_id,stock_document_id),UNIQUE(company_id,return_stock_action_id),
 FOREIGN KEY(company_id,item_id) REFERENCES erp.items(company_id,id),
 FOREIGN KEY(company_id,warehouse_id) REFERENCES erp.warehouses(company_id,id),
 FOREIGN KEY(company_id,sales_return_line_id) REFERENCES erp.sales_return_lines(company_id,id),
 FOREIGN KEY(company_id,stock_document_id) REFERENCES erp.inventory_documents(company_id,id),
 FOREIGN KEY(company_id,return_stock_action_id)
 REFERENCES erp.sales_return_stock_actions(company_id,id),
 FOREIGN KEY(company_id,sales_invoice_id) REFERENCES erp.sales_invoices(company_id,id)
);
CREATE INDEX ix_disposal_scope ON erp.stock_disposal_cases(company_id,warehouse_id,item_id);
ALTER TABLE erp.stock_disposal_cases ENABLE ROW LEVEL SECURITY;
CREATE POLICY company_scope ON erp.stock_disposal_cases USING(EXISTS(SELECT 1 FROM erp.companies c
 WHERE c.id=company_id AND c.tenant_id=erp.current_tenant_id())) WITH CHECK(EXISTS(
 SELECT 1 FROM erp.companies c WHERE c.id=company_id AND c.tenant_id=erp.current_tenant_id()));
CREATE FUNCTION erp.guard_stock_disposal_case() RETURNS trigger LANGUAGE plpgsql
SET search_path=pg_catalog,erp,identity AS $$
DECLARE d record;a record;v_qty numeric;v_destination uuid;
BEGIN
 IF TG_OP='DELETE' THEN RAISE EXCEPTION 'Disposal history cannot be deleted'; END IF;
 IF NOT identity.has_company_permission(NEW.company_id,'inventory.post') THEN
 RAISE EXCEPTION 'Disposal permission denied' USING ERRCODE='42501'; END IF;
 IF TG_OP='INSERT' THEN
 IF NEW.status<>'draft' OR NEW.stock_document_id IS NOT NULL
 OR NEW.return_stock_action_id IS NOT NULL
 OR NEW.sales_invoice_id IS NOT NULL OR NEW.approved_at IS NOT NULL OR NEW.approved_by IS NOT NULL
 OR NEW.quality_approved_at IS NOT NULL OR NEW.quality_approved_by IS NOT NULL THEN
 RAISE EXCEPTION 'Disposal must start draft without effects'; END IF;
 ELSE
 IF (NEW.id,NEW.company_id,NEW.case_no,NEW.item_id,NEW.warehouse_id,NEW.quantity,
 NEW.sales_return_line_id,NEW.method) IS DISTINCT FROM
 (OLD.id,OLD.company_id,OLD.case_no,OLD.item_id,OLD.warehouse_id,OLD.quantity,
 OLD.sales_return_line_id,OLD.method) OR OLD.status='void' OR
 (OLD.status='executed' AND (NEW.status<>OLD.status OR
 (NEW.stock_document_id,NEW.return_stock_action_id,NEW.approved_at,NEW.quality_approved_at)
 IS DISTINCT FROM (OLD.stock_document_id,OLD.return_stock_action_id,OLD.approved_at,
 OLD.quality_approved_at) OR (NEW.reason,NEW.qc_notes) IS DISTINCT FROM (OLD.reason,OLD.qc_notes)
 OR (OLD.sales_invoice_id IS NOT NULL
 AND NEW.sales_invoice_id IS DISTINCT FROM OLD.sales_invoice_id)))
 OR (OLD.status<>'draft' AND (NEW.reason,NEW.qc_notes)
 IS DISTINCT FROM (OLD.reason,OLD.qc_notes))
 THEN RAISE EXCEPTION 'Disposal source/effects immutable'; END IF;
 IF NEW.status<>OLD.status AND NOT ((OLD.status='draft' AND NEW.status IN ('approved','void'))
 OR (OLD.status='approved' AND NEW.status IN ('executed','void'))) THEN
 RAISE EXCEPTION 'Invalid disposal transition'; END IF;
 NEW.row_version:=OLD.row_version+1;
 NEW.approved_at:=OLD.approved_at;NEW.approved_by:=OLD.approved_by;
 NEW.quality_approved_at:=OLD.quality_approved_at;NEW.quality_approved_by:=OLD.quality_approved_by;
 END IF;
 IF TG_OP='UPDATE' AND NEW.status='approved' AND OLD.status='draft' THEN
 IF NEW.method='write_off' AND NOT identity.has_company_permission(NEW.company_id,
 'inventory.write_off') THEN RAISE EXCEPTION 'Loss approval permission denied'; END IF;
 NEW.approved_at:=clock_timestamp();NEW.approved_by:=identity.current_user_id();
 IF NEW.method='liquidation' THEN
 IF NOT identity.has_company_permission(NEW.company_id,'inventory.quality.approve') OR
 coalesce(length(btrim(NEW.qc_notes)),0)=0 THEN
 RAISE EXCEPTION 'Clearance requires safe-sale QC'; END IF;
 NEW.quality_approved_at:=clock_timestamp();NEW.quality_approved_by:=identity.current_user_id();
 END IF;
 END IF;
 IF NEW.sales_return_line_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM erp.sales_return_lines l
 JOIN erp.sales_returns r ON r.company_id=l.company_id AND r.id=l.sales_return_id
 JOIN erp.sales_invoice_lines i ON i.company_id=l.company_id AND i.id=l.sales_invoice_line_id
 WHERE l.company_id=NEW.company_id AND l.id=NEW.sales_return_line_id AND r.status='posted'
 AND r.kind='return' AND i.item_id=NEW.item_id) THEN
 RAISE EXCEPTION 'Disposal must reference matching posted return stock'; END IF;
 IF NEW.sales_return_line_id IS NOT NULL THEN
 PERFORM 1 FROM erp.sales_return_lines WHERE company_id=NEW.company_id
 AND id=NEW.sales_return_line_id FOR UPDATE;
 IF NOT EXISTS(SELECT 1 FROM erp.sales_return_lines l
 WHERE l.company_id=NEW.company_id AND l.id=NEW.sales_return_line_id
 AND l.warehouse_id=NEW.warehouse_id AND NEW.quantity+
 (SELECT coalesce(sum(c.quantity),0) FROM erp.stock_disposal_cases c
 WHERE c.company_id=NEW.company_id AND c.sales_return_line_id=l.id
 AND c.id<>NEW.id AND c.status<>'void')<=l.quantity) THEN
 RAISE EXCEPTION 'Disposal exceeds linked return stock quantity/location'; END IF;
 END IF;
 IF NEW.stock_document_id IS NOT NULL THEN
 SELECT * INTO d FROM erp.inventory_documents WHERE company_id=NEW.company_id
 AND id=NEW.stock_document_id;
 IF NEW.sales_return_line_id IS NOT NULL
 OR (NEW.method='write_off' AND d.document_kind<>'adjustment_out')
 OR (NEW.method IN ('repair_later','liquidation') AND d.document_kind<>'transfer')
 OR (SELECT count(*) FROM erp.inventory_document_lines WHERE company_id=NEW.company_id
 AND inventory_document_id=d.id)<>1 OR NOT EXISTS(SELECT 1 FROM erp.inventory_document_lines l
 WHERE l.company_id=NEW.company_id AND l.inventory_document_id=d.id AND l.item_id=NEW.item_id
 AND l.from_warehouse_id=NEW.warehouse_id AND l.quantity=NEW.quantity AND l.lot_id IS NULL)
 THEN RAISE EXCEPTION 'Disposal stock document source mismatch'; END IF;
 IF NEW.status='executed' AND d.status<>'posted' THEN
 RAISE EXCEPTION 'Disposal requires posted stock document'; END IF;
 END IF;
 IF NEW.stock_document_id IS NOT NULL THEN
 SELECT to_warehouse_id INTO v_destination FROM erp.inventory_document_lines
 WHERE company_id=NEW.company_id AND inventory_document_id=NEW.stock_document_id;
 END IF;
 IF NEW.return_stock_action_id IS NOT NULL THEN
 SELECT * INTO a FROM erp.sales_return_stock_actions WHERE company_id=NEW.company_id
 AND id=NEW.return_stock_action_id;
 IF a.sales_return_line_id IS DISTINCT FROM NEW.sales_return_line_id OR
 a.from_warehouse_id IS DISTINCT FROM NEW.warehouse_id OR a.quantity IS DISTINCT FROM NEW.quantity
 THEN RAISE EXCEPTION 'Disposal return action mismatch'; END IF;
 v_destination:=a.to_warehouse_id;
 END IF;
 IF NEW.stock_document_id IS NOT NULL AND NEW.return_stock_action_id IS NOT NULL THEN
 RAISE EXCEPTION 'Disposal cannot have two stock effects'; END IF;
 IF NEW.stock_document_id IS NOT NULL OR NEW.return_stock_action_id IS NOT NULL THEN
 IF NEW.method='write_off' AND v_destination IS NOT NULL THEN
 RAISE EXCEPTION 'Write-off must remove stock'; END IF;
 IF NEW.method IN ('repair_later','liquidation') AND NOT EXISTS(SELECT 1 FROM erp.warehouses
 WHERE company_id=NEW.company_id AND id=v_destination AND operational_role=
 CASE NEW.method WHEN 'repair_later' THEN 'repair' ELSE 'clearance' END) THEN
 RAISE EXCEPTION 'Disposal destination role mismatch'; END IF;
 END IF;
 IF NEW.status='executed' AND NEW.method='supplier_claim' THEN
 RAISE EXCEPTION 'Supplier claim settlement workflow required'; END IF;
 IF NEW.status='executed' AND NEW.method<>'supplier_claim' AND
 NEW.stock_document_id IS NULL AND NEW.return_stock_action_id IS NULL THEN
 RAISE EXCEPTION 'Disposal requires committed stock effects'; END IF;
 IF NEW.sales_invoice_id IS NOT NULL THEN
 IF NEW.status<>'executed' OR NEW.method<>'liquidation' OR NOT EXISTS(
 SELECT 1 FROM erp.sales_invoices WHERE company_id=NEW.company_id AND id=NEW.sales_invoice_id
 AND status='posted' AND document_kind='invoice') THEN
 RAISE EXCEPTION 'Clearance requires posted sale'; END IF;
 SELECT coalesce(-sum(m.quantity_delta),0) INTO v_qty FROM erp.stock_movements m
 JOIN erp.sales_invoice_lines l ON l.company_id=m.company_id AND l.id=m.source_line_id
 WHERE m.company_id=NEW.company_id AND l.sales_invoice_id=NEW.sales_invoice_id
 AND m.source_type='sales_invoice' AND m.item_id=NEW.item_id
 AND m.warehouse_id=v_destination AND m.quantity_delta<0;
 IF v_qty<NEW.quantity+(SELECT coalesce(sum(quantity),0) FROM erp.stock_disposal_cases
 WHERE company_id=NEW.company_id AND sales_invoice_id=NEW.sales_invoice_id
 AND item_id=NEW.item_id AND id<>NEW.id) THEN
 RAISE EXCEPTION 'Clearance sale stock issue capacity exceeded'; END IF;
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER trg_disposal_guard BEFORE INSERT OR UPDATE OR DELETE ON erp.stock_disposal_cases
 FOR EACH ROW EXECUTE FUNCTION erp.guard_stock_disposal_case();
CREATE TRIGGER trg_disposal_audit AFTER INSERT OR UPDATE ON erp.stock_disposal_cases
 FOR EACH ROW EXECUTE FUNCTION erp.audit_sales_return();

CREATE OR REPLACE FUNCTION erp.block_unchecked_quality_transfer() RETURNS trigger LANGUAGE plpgsql
SET search_path=pg_catalog,erp AS $$
DECLARE l record;
BEGIN
 IF NEW.quantity_delta>0 AND NEW.inventory_document_line_id IS NOT NULL THEN
 SELECT line.* INTO l FROM erp.inventory_document_lines line WHERE line.company_id=NEW.company_id
 AND line.id=NEW.inventory_document_line_id;
 IF EXISTS(SELECT 1 FROM erp.warehouses WHERE company_id=NEW.company_id AND id=l.from_warehouse_id
 AND stock_category<>'sellable') AND EXISTS(SELECT 1 FROM erp.warehouses
 WHERE company_id=NEW.company_id
 AND id=l.to_warehouse_id AND stock_category='sellable') AND NOT EXISTS(
 SELECT 1 FROM erp.stock_disposal_cases c JOIN erp.warehouses w ON w.company_id=c.company_id
 AND w.id=l.to_warehouse_id WHERE c.company_id=NEW.company_id AND c.stock_document_id=
 l.inventory_document_id AND c.status='approved' AND c.method='liquidation'
 AND c.quality_approved_at IS NOT NULL AND c.quality_approved_by IS NOT NULL
 AND c.sales_return_line_id IS NULL AND c.item_id=l.item_id AND c.warehouse_id=l.from_warehouse_id
 AND c.quantity=l.quantity AND w.operational_role='clearance') THEN
 RAISE EXCEPTION 'Use typed QC release for segregated stock'; END IF;
 END IF;
 RETURN NEW;
END $$;
"""


def reverse_sql() -> str:
    from importlib import import_module

    previous = str(
        import_module("apps.database.migrations.0034_return_repairs_and_credit_targets").SQL
    )
    start = previous.index("CREATE FUNCTION erp.block_unchecked_quality_transfer()")
    end = previous.index("CREATE TRIGGER trg_stock_quality_transfer", start)
    return (
        "DO $$ BEGIN IF EXISTS(SELECT 1 FROM erp.stock_disposal_cases) THEN "
        "RAISE EXCEPTION 'Cannot reverse retained disposal case history'; END IF; END $$; "
        "DROP TABLE erp.stock_disposal_cases; DROP FUNCTION erp.guard_stock_disposal_case();\n"
        + previous[start:end].replace("CREATE FUNCTION", "CREATE OR REPLACE FUNCTION", 1)
    )


class Migration(migrations.Migration):
    dependencies = [("database", "0035_return_custody_intake")]
    operations = [migrations.RunSQL(SQL, reverse_sql())]
