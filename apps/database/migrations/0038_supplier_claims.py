from django.db import migrations

SQL = r"""
CREATE TABLE erp.supplier_return_claims(
 id uuid PRIMARY KEY DEFAULT uuidv7(),company_id uuid NOT NULL,
 claim_no text NOT NULL CHECK(length(btrim(claim_no))>0),
 source_bill_line_id uuid NOT NULL,warehouse_id uuid NOT NULL,
 disposal_case_id uuid,reason text NOT NULL CHECK(length(btrim(reason))>0),
 status text NOT NULL DEFAULT 'draft'
 CHECK(status IN ('draft','approved','settled','rejected')),
 supplier_credit_id uuid,approved_at timestamptz,approved_by uuid
 REFERENCES identity.users(id),settled_at timestamptz,
 row_version bigint NOT NULL DEFAULT 1 CHECK(row_version>0),
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 UNIQUE(company_id,id),UNIQUE(company_id,claim_no),
 UNIQUE(company_id,source_bill_line_id),UNIQUE(company_id,supplier_credit_id),
 UNIQUE(company_id,disposal_case_id),
 FOREIGN KEY(company_id,source_bill_line_id)
 REFERENCES erp.purchase_bill_lines(company_id,id),
 FOREIGN KEY(company_id,warehouse_id) REFERENCES erp.warehouses(company_id,id),
 FOREIGN KEY(company_id,disposal_case_id)
 REFERENCES erp.stock_disposal_cases(company_id,id),
 FOREIGN KEY(company_id,supplier_credit_id)
 REFERENCES erp.purchase_bills(company_id,id)
);
CREATE INDEX ix_supplier_claim_status ON erp.supplier_return_claims(company_id,status,id);
ALTER TABLE erp.supplier_return_claims ENABLE ROW LEVEL SECURITY;
CREATE POLICY company_scope ON erp.supplier_return_claims USING(EXISTS(
 SELECT 1 FROM erp.companies c WHERE c.id=company_id
 AND c.tenant_id=erp.current_tenant_id())) WITH CHECK(EXISTS(
 SELECT 1 FROM erp.companies c WHERE c.id=company_id
 AND c.tenant_id=erp.current_tenant_id()));
INSERT INTO identity.permissions(code,description) VALUES
 ('purchasing.claim.approve','Approve physical supplier return claims')
 ON CONFLICT DO NOTHING;

CREATE FUNCTION erp.guard_supplier_return_claim() RETURNS trigger LANGUAGE plpgsql
SET search_path=pg_catalog,erp,identity AS $$
DECLARE src record;origin record;cr record;cl record;sm record;dc record;
BEGIN
 IF TG_OP='DELETE' THEN RAISE EXCEPTION 'Supplier claim history immutable'; END IF;
 IF NOT identity.has_company_permission(NEW.company_id,'purchasing.bill.edit_draft') THEN
 RAISE EXCEPTION 'Supplier claim permission denied' USING ERRCODE='42501'; END IF;
 SELECT l.*,b.status bill_status,b.document_kind,b.supplier_id,b.id bill_id,
 i.item_kind,i.track_lots,i.track_serials INTO src
 FROM erp.purchase_bill_lines l JOIN erp.purchase_bills b
 ON b.company_id=l.company_id AND b.id=l.purchase_bill_id
 JOIN erp.items i ON i.company_id=l.company_id AND i.id=l.item_id
 WHERE l.company_id=NEW.company_id AND l.id=NEW.source_bill_line_id FOR SHARE OF l;
 IF src.bill_status IS DISTINCT FROM 'posted' OR src.document_kind<>'bill' OR
 src.item_kind<>'stock' OR src.track_lots OR src.track_serials THEN
 RAISE EXCEPTION 'Claim requires posted untracked supplier receipt'; END IF;
 SELECT * INTO origin FROM erp.stock_movements WHERE company_id=NEW.company_id
 AND source_type='purchase_bill' AND source_line_id=src.id AND quantity_delta>0;
 IF origin.id IS NULL OR origin.warehouse_id IS DISTINCT FROM NEW.warehouse_id OR
 origin.quantity_delta IS DISTINCT FROM src.quantity THEN
 RAISE EXCEPTION 'Claim must reference original physical receipt'; END IF;
 IF NEW.disposal_case_id IS NOT NULL THEN
 SELECT * INTO dc FROM erp.stock_disposal_cases WHERE company_id=NEW.company_id
 AND id=NEW.disposal_case_id FOR UPDATE;
 IF dc.method IS DISTINCT FROM 'supplier_claim' OR dc.status<>'approved' OR
 dc.item_id IS DISTINCT FROM src.item_id OR
 dc.warehouse_id IS DISTINCT FROM NEW.warehouse_id OR
 dc.quantity IS DISTINCT FROM src.quantity OR dc.sales_return_line_id IS NULL THEN
 RAISE EXCEPTION 'Supplier claim disposal case mismatch'; END IF;
 END IF;
 IF TG_OP='INSERT' THEN
 IF NEW.status<>'draft' OR NEW.supplier_credit_id IS NOT NULL OR
 NEW.approved_at IS NOT NULL OR NEW.approved_by IS NOT NULL OR NEW.settled_at IS NOT NULL
 THEN RAISE EXCEPTION 'Claim must start as nonfinancial draft'; END IF;
 ELSE
 IF (NEW.id,NEW.company_id,NEW.claim_no,NEW.source_bill_line_id,
 NEW.warehouse_id,NEW.disposal_case_id,NEW.reason) IS DISTINCT FROM
 (OLD.id,OLD.company_id,OLD.claim_no,OLD.source_bill_line_id,
 OLD.warehouse_id,OLD.disposal_case_id,OLD.reason) OR
 OLD.status IN ('settled','rejected') THEN
 RAISE EXCEPTION 'Claim source or final state immutable'; END IF;
 IF NEW.status<>OLD.status AND NOT ((OLD.status='draft' AND
 NEW.status IN ('approved','rejected')) OR (OLD.status='approved' AND
 NEW.status IN ('settled','rejected'))) THEN RAISE EXCEPTION 'Invalid claim transition'; END IF;
 NEW.row_version:=OLD.row_version+1;
 NEW.approved_at:=OLD.approved_at;NEW.approved_by:=OLD.approved_by;
 NEW.settled_at:=OLD.settled_at;
 IF OLD.status='draft' AND NEW.status='approved' THEN
 IF NOT identity.has_company_permission(NEW.company_id,'purchasing.claim.approve') THEN
 RAISE EXCEPTION 'Claim approval permission denied' USING ERRCODE='42501'; END IF;
 NEW.approved_at:=clock_timestamp();NEW.approved_by:=identity.current_user_id();
 END IF;
 END IF;
 IF NEW.status='settled' THEN
 IF NEW.approved_at IS NULL OR NEW.supplier_credit_id IS NULL THEN
 RAISE EXCEPTION 'Approve and link credit before settlement'; END IF;
 SELECT * INTO cr FROM erp.purchase_bills WHERE company_id=NEW.company_id
 AND id=NEW.supplier_credit_id FOR SHARE;
 SELECT * INTO cl FROM erp.purchase_bill_lines WHERE company_id=NEW.company_id
 AND purchase_bill_id=NEW.supplier_credit_id;
 SELECT * INTO sm FROM erp.stock_movements WHERE company_id=NEW.company_id
 AND source_type='purchase_bill' AND source_id=NEW.supplier_credit_id
 AND source_line_id=cl.id AND quantity_delta<0;
 IF cr.status IS DISTINCT FROM 'posted' OR cr.document_kind<>'supplier_credit' OR
 cr.credit_of_bill_id IS DISTINCT FROM src.bill_id OR
 cr.supplier_id IS DISTINCT FROM src.supplier_id OR
 cl.credit_of_bill_line_id IS DISTINCT FROM src.id OR
 cl.quantity IS DISTINCT FROM src.quantity OR
 sm.quantity_delta IS DISTINCT FROM -src.quantity OR
 sm.warehouse_id IS DISTINCT FROM NEW.warehouse_id OR
 sm.item_id IS DISTINCT FROM src.item_id OR
 (SELECT count(*) FROM erp.purchase_bill_lines WHERE company_id=NEW.company_id
 AND purchase_bill_id=NEW.supplier_credit_id)<>1 THEN
 RAISE EXCEPTION 'Claim requires matching posted physical supplier credit'; END IF;
 NEW.settled_at:=clock_timestamp();
 ELSIF NEW.supplier_credit_id IS NOT NULL THEN
 RAISE EXCEPTION 'Unsettled claim cannot link credit';
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER trg_supplier_claim_guard
 BEFORE INSERT OR UPDATE OR DELETE ON erp.supplier_return_claims
 FOR EACH ROW EXECUTE FUNCTION erp.guard_supplier_return_claim();
CREATE TRIGGER trg_supplier_claim_audit AFTER INSERT OR UPDATE
 ON erp.supplier_return_claims FOR EACH ROW
 EXECUTE FUNCTION erp.audit_sales_return();

CREATE OR REPLACE FUNCTION erp.guard_stock_disposal_case() RETURNS trigger
 LANGUAGE plpgsql SET search_path=pg_catalog,erp,identity AS $$
DECLARE d record;a record;v_qty numeric;v_destination uuid;
BEGIN
 IF TG_OP='DELETE' THEN RAISE EXCEPTION 'Disposal history cannot be deleted'; END IF;
 IF NOT identity.has_company_permission(NEW.company_id,'inventory.post') THEN
 RAISE EXCEPTION 'Disposal permission denied' USING ERRCODE='42501'; END IF;
 IF TG_OP='INSERT' THEN
 IF NEW.status<>'draft' OR NEW.stock_document_id IS NOT NULL OR
 NEW.return_stock_action_id IS NOT NULL OR NEW.sales_invoice_id IS NOT NULL OR
 NEW.approved_at IS NOT NULL OR NEW.approved_by IS NOT NULL OR
 NEW.quality_approved_at IS NOT NULL OR NEW.quality_approved_by IS NOT NULL THEN
 RAISE EXCEPTION 'Disposal must start draft without effects'; END IF;
 ELSE
 IF (NEW.id,NEW.company_id,NEW.case_no,NEW.item_id,NEW.warehouse_id,
 NEW.quantity,NEW.sales_return_line_id,NEW.method) IS DISTINCT FROM
 (OLD.id,OLD.company_id,OLD.case_no,OLD.item_id,OLD.warehouse_id,
 OLD.quantity,OLD.sales_return_line_id,OLD.method) OR OLD.status='void' OR
 (OLD.status='executed' AND (NEW.status<>OLD.status OR
 (NEW.stock_document_id,NEW.return_stock_action_id,NEW.approved_at,
 NEW.quality_approved_at) IS DISTINCT FROM
 (OLD.stock_document_id,OLD.return_stock_action_id,OLD.approved_at,
 OLD.quality_approved_at) OR (NEW.reason,NEW.qc_notes) IS DISTINCT FROM
 (OLD.reason,OLD.qc_notes) OR (OLD.sales_invoice_id IS NOT NULL AND
 NEW.sales_invoice_id IS DISTINCT FROM OLD.sales_invoice_id))) THEN
 RAISE EXCEPTION 'Disposal source/effects immutable'; END IF;
 IF OLD.status<>'draft' AND (NEW.reason,NEW.qc_notes)
 IS DISTINCT FROM (OLD.reason,OLD.qc_notes) THEN
 RAISE EXCEPTION 'Disposal source/effects immutable'; END IF;
 IF NEW.status<>OLD.status AND NOT ((OLD.status='draft' AND
 NEW.status IN ('approved','void')) OR (OLD.status='approved' AND
 NEW.status IN ('executed','void'))) THEN
 RAISE EXCEPTION 'Invalid disposal transition'; END IF;
 NEW.row_version:=OLD.row_version+1;
 NEW.approved_at:=OLD.approved_at;NEW.approved_by:=OLD.approved_by;
 NEW.quality_approved_at:=OLD.quality_approved_at;
 NEW.quality_approved_by:=OLD.quality_approved_by;
 END IF;
 IF TG_OP='UPDATE' AND NEW.status='approved' AND OLD.status='draft' THEN
 IF NEW.method='write_off' AND NOT identity.has_company_permission(
 NEW.company_id,'inventory.write_off') THEN
 RAISE EXCEPTION 'Loss approval permission denied'; END IF;
 NEW.approved_at:=clock_timestamp();NEW.approved_by:=identity.current_user_id();
 IF NEW.method='liquidation' THEN
 IF NOT identity.has_company_permission(NEW.company_id,
 'inventory.quality.approve') OR
 coalesce(length(btrim(NEW.qc_notes)),0)=0 THEN
 RAISE EXCEPTION 'Clearance requires safe-sale QC'; END IF;
 NEW.quality_approved_at:=clock_timestamp();
 NEW.quality_approved_by:=identity.current_user_id();
 END IF;
 END IF;
 IF NEW.sales_return_line_id IS NOT NULL AND NOT EXISTS(
 SELECT 1 FROM erp.sales_return_lines l JOIN erp.sales_returns r
 ON r.company_id=l.company_id AND r.id=l.sales_return_id
 JOIN erp.sales_invoice_lines i ON i.company_id=l.company_id
 AND i.id=l.sales_invoice_line_id WHERE l.company_id=NEW.company_id
 AND l.id=NEW.sales_return_line_id AND r.status='posted'
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
 IF NEW.sales_return_line_id IS NOT NULL OR
 (NEW.method='write_off' AND d.document_kind<>'adjustment_out') OR
 (NEW.method IN ('repair_later','liquidation') AND d.document_kind<>'transfer')
 OR (SELECT count(*) FROM erp.inventory_document_lines
 WHERE company_id=NEW.company_id AND inventory_document_id=d.id)<>1 OR
 NOT EXISTS(SELECT 1 FROM erp.inventory_document_lines l
 WHERE l.company_id=NEW.company_id AND l.inventory_document_id=d.id
 AND l.item_id=NEW.item_id AND l.from_warehouse_id=NEW.warehouse_id
 AND l.quantity=NEW.quantity AND l.lot_id IS NULL) THEN
 RAISE EXCEPTION 'Disposal stock document source mismatch'; END IF;
 IF NEW.status='executed' AND d.status<>'posted' THEN
 RAISE EXCEPTION 'Disposal requires posted stock document'; END IF;
 SELECT to_warehouse_id INTO v_destination FROM erp.inventory_document_lines
 WHERE company_id=NEW.company_id AND inventory_document_id=d.id;
 END IF;
 IF NEW.return_stock_action_id IS NOT NULL THEN
 SELECT * INTO a FROM erp.sales_return_stock_actions
 WHERE company_id=NEW.company_id AND id=NEW.return_stock_action_id;
 IF a.sales_return_line_id IS DISTINCT FROM NEW.sales_return_line_id OR
 a.from_warehouse_id IS DISTINCT FROM NEW.warehouse_id OR
 a.quantity IS DISTINCT FROM NEW.quantity THEN
 RAISE EXCEPTION 'Disposal return action mismatch'; END IF;
 v_destination:=a.to_warehouse_id;
 END IF;
 IF NEW.stock_document_id IS NOT NULL AND NEW.return_stock_action_id IS NOT NULL THEN
 RAISE EXCEPTION 'Disposal cannot have two stock effects'; END IF;
 IF NEW.stock_document_id IS NOT NULL OR NEW.return_stock_action_id IS NOT NULL THEN
 IF NEW.method='write_off' AND v_destination IS NOT NULL THEN
 RAISE EXCEPTION 'Write-off must remove stock'; END IF;
 IF NEW.method IN ('repair_later','liquidation') AND NOT EXISTS(
 SELECT 1 FROM erp.warehouses WHERE company_id=NEW.company_id
 AND id=v_destination AND operational_role=CASE NEW.method
 WHEN 'repair_later' THEN 'repair' ELSE 'clearance' END) THEN
 RAISE EXCEPTION 'Disposal destination role mismatch'; END IF;
 END IF;
 IF NEW.status='executed' AND NEW.method='supplier_claim' AND NOT EXISTS(
 SELECT 1 FROM erp.supplier_return_claims claim
 WHERE claim.company_id=NEW.company_id AND claim.disposal_case_id=NEW.id
 AND claim.status='settled') THEN
 RAISE EXCEPTION 'Supplier claim settlement workflow required'; END IF;
 IF NEW.status='executed' AND NEW.method<>'supplier_claim' AND
 NEW.stock_document_id IS NULL AND NEW.return_stock_action_id IS NULL THEN
 RAISE EXCEPTION 'Disposal requires committed stock effects'; END IF;
 IF NEW.sales_invoice_id IS NOT NULL THEN
 IF NEW.status<>'executed' OR NEW.method<>'liquidation' OR NOT EXISTS(
 SELECT 1 FROM erp.sales_invoices WHERE company_id=NEW.company_id
 AND id=NEW.sales_invoice_id AND status='posted'
 AND document_kind='invoice') THEN
 RAISE EXCEPTION 'Clearance requires posted sale'; END IF;
 SELECT coalesce(-sum(m.quantity_delta),0) INTO v_qty
 FROM erp.stock_movements m JOIN erp.sales_invoice_lines l
 ON l.company_id=m.company_id AND l.id=m.source_line_id
 WHERE m.company_id=NEW.company_id AND
 l.sales_invoice_id=NEW.sales_invoice_id AND
 m.source_type='sales_invoice' AND m.item_id=NEW.item_id AND
 m.warehouse_id=v_destination AND m.quantity_delta<0;
 IF v_qty<NEW.quantity+(SELECT coalesce(sum(quantity),0)
 FROM erp.stock_disposal_cases WHERE company_id=NEW.company_id
 AND sales_invoice_id=NEW.sales_invoice_id AND item_id=NEW.item_id
 AND id<>NEW.id) THEN
 RAISE EXCEPTION 'Clearance sale stock issue capacity exceeded'; END IF;
 END IF;
 RETURN NEW;
END $$;
"""


def reverse_sql() -> str:
    from importlib import import_module

    previous = str(import_module("apps.database.migrations.0036_stock_disposal_cases").SQL)
    start = previous.index("CREATE FUNCTION erp.guard_stock_disposal_case()")
    end = previous.index("CREATE TRIGGER trg_disposal_guard", start)
    restore = previous[start:end].replace("CREATE FUNCTION", "CREATE OR REPLACE FUNCTION", 1)
    return (
        "DO $$ BEGIN IF EXISTS(SELECT 1 FROM erp.supplier_return_claims) THEN "
        "RAISE EXCEPTION 'Cannot reverse retained supplier claim history'; END IF; END $$; "
        "DROP TABLE erp.supplier_return_claims; "
        "DROP FUNCTION erp.guard_supplier_return_claim(); " + restore
    )


class Migration(migrations.Migration):
    dependencies = [("database", "0037_sales_order_delivery")]
    operations = [migrations.RunSQL(SQL, reverse_sql())]
