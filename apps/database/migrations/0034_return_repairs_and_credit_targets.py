from django.db import migrations

SQL = r"""
CREATE TABLE erp.return_repair_jobs(
 id uuid PRIMARY KEY DEFAULT uuidv7(),company_id uuid NOT NULL,
 sales_return_line_id uuid NOT NULL,warehouse_id uuid NOT NULL,
 quantity numeric(20,6) NOT NULL CHECK(quantity>0),
 diagnosis text NOT NULL CHECK(length(btrim(diagnosis))>0),
 estimated_cost numeric(20,6) NOT NULL DEFAULT 0 CHECK(estimated_cost>=0),
 status text NOT NULL DEFAULT 'received' CHECK(status IN
 ('received','in_progress','waiting_parts','on_hold','repaired','qc_failed','qc_passed',
 'not_repairable')),
 qc_notes text, qc_at timestamptz, qc_by uuid REFERENCES identity.users(id),
 row_version bigint NOT NULL DEFAULT 1 CHECK(row_version>0),
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 UNIQUE(company_id,id),
 FOREIGN KEY(company_id,sales_return_line_id) REFERENCES erp.sales_return_lines(company_id,id),
 FOREIGN KEY(company_id,warehouse_id) REFERENCES erp.warehouses(company_id,id)
);
CREATE INDEX ix_return_repair_line ON erp.return_repair_jobs(company_id,sales_return_line_id);
ALTER TABLE erp.return_repair_jobs ENABLE ROW LEVEL SECURITY;
CREATE POLICY company_scope ON erp.return_repair_jobs USING(EXISTS(
 SELECT 1 FROM erp.companies c WHERE c.id=company_id AND c.tenant_id=erp.current_tenant_id()))
 WITH CHECK(EXISTS(SELECT 1 FROM erp.companies c WHERE c.id=company_id
 AND c.tenant_id=erp.current_tenant_id()));
INSERT INTO identity.permissions(code,description) VALUES
 ('inventory.repair.manage','Manage returned-item repairs'),
 ('inventory.quality.approve','Approve returned-stock quality checks') ON CONFLICT DO NOTHING;

CREATE FUNCTION erp.guard_return_repair_job() RETURNS trigger LANGUAGE plpgsql
SET search_path=pg_catalog,erp,identity AS $$
DECLARE l record;r record;v_quantity numeric;
BEGIN
 IF TG_OP='DELETE' THEN RAISE EXCEPTION 'Repair history cannot be deleted'; END IF;
 IF NOT identity.has_company_permission(NEW.company_id,'inventory.repair.manage') THEN
  RAISE EXCEPTION 'Repair permission denied' USING ERRCODE='42501'; END IF;
 SELECT * INTO l FROM erp.sales_return_lines WHERE company_id=NEW.company_id
 AND id=NEW.sales_return_line_id FOR UPDATE;
 SELECT * INTO r FROM erp.sales_returns WHERE company_id=NEW.company_id AND id=l.sales_return_id;
 IF r.status IS DISTINCT FROM 'posted' OR r.kind<>'return' OR l.disposition='write_off' THEN
  RAISE EXCEPTION 'Repair requires retained posted return stock'; END IF;
 PERFORM 1 FROM erp.warehouses WHERE company_id=NEW.company_id AND id=NEW.warehouse_id
 AND is_active AND stock_category<>'sellable' FOR SHARE;
 IF NOT FOUND THEN RAISE EXCEPTION 'Repair warehouse must be active and segregated'; END IF;
 IF TG_OP='INSERT' THEN
  IF NEW.status<>'received' OR NEW.qc_at IS NOT NULL OR NEW.qc_by IS NOT NULL THEN
   RAISE EXCEPTION 'New repairs must start received'; END IF;
  SELECT coalesce(sum(CASE WHEN to_warehouse_id=NEW.warehouse_id THEN quantity
   WHEN from_warehouse_id=NEW.warehouse_id THEN -quantity ELSE 0 END),0)
  INTO v_quantity FROM erp.sales_return_stock_actions WHERE company_id=NEW.company_id
  AND sales_return_line_id=l.id;
  v_quantity:=v_quantity+CASE WHEN l.warehouse_id=NEW.warehouse_id THEN l.quantity ELSE 0 END;
  IF NEW.quantity>v_quantity THEN RAISE EXCEPTION 'Repair exceeds retained return quantity'; END IF;
 ELSE
  IF (NEW.id,NEW.company_id,NEW.sales_return_line_id,NEW.warehouse_id,NEW.quantity)
   IS DISTINCT FROM (OLD.id,OLD.company_id,OLD.sales_return_line_id,OLD.warehouse_id,OLD.quantity)
   OR OLD.status IN ('qc_passed','not_repairable') THEN
   RAISE EXCEPTION 'Repair source and final decisions are immutable'; END IF;
  IF NEW.status<>OLD.status AND NOT (
   (OLD.status IN ('received','in_progress','waiting_parts','on_hold','qc_failed') AND
    NEW.status IN ('in_progress','waiting_parts','on_hold','not_repairable','repaired')) OR
   (OLD.status='repaired' AND NEW.status IN ('qc_passed','qc_failed')) OR
   (OLD.status='received' AND l.condition='resellable' AND NEW.status IN ('qc_passed','qc_failed'))
  ) THEN RAISE EXCEPTION 'Invalid repair transition'; END IF;
  NEW.row_version:=OLD.row_version+1;
 END IF;
 IF NEW.status IN ('qc_passed','qc_failed') AND
    (TG_OP='INSERT' OR NEW.status IS DISTINCT FROM OLD.status) THEN
  IF NOT identity.has_company_permission(NEW.company_id,'inventory.quality.approve')
   OR identity.current_user_id() IS NULL OR coalesce(length(btrim(NEW.qc_notes)),0)=0 THEN
   RAISE EXCEPTION 'Explicit authorized QC decision required' USING ERRCODE='42501'; END IF;
  NEW.qc_at:=clock_timestamp();NEW.qc_by:=identity.current_user_id();
 ELSIF TG_OP='UPDATE' THEN
  NEW.qc_at:=OLD.qc_at;NEW.qc_by:=OLD.qc_by;NEW.qc_notes:=OLD.qc_notes;
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER trg_return_repair_guard BEFORE INSERT OR UPDATE OR DELETE ON erp.return_repair_jobs
 FOR EACH ROW EXECUTE FUNCTION erp.guard_return_repair_job();
CREATE TRIGGER trg_return_repair_audit AFTER INSERT OR UPDATE ON erp.return_repair_jobs
 FOR EACH ROW EXECUTE FUNCTION erp.audit_sales_return();
ALTER TABLE erp.sales_return_stock_actions ADD COLUMN repair_job_id uuid,
 ADD CONSTRAINT fk_return_action_repair FOREIGN KEY(company_id,repair_job_id)
 REFERENCES erp.return_repair_jobs(company_id,id);

CREATE FUNCTION erp.require_return_quality_release() RETURNS trigger LANGUAGE plpgsql
SET search_path=pg_catalog,erp AS $$
DECLARE j record;v_used numeric;
BEGIN
 IF NEW.to_warehouse_id IS NOT NULL AND EXISTS(SELECT 1 FROM erp.warehouses
 WHERE company_id=NEW.company_id AND id=NEW.to_warehouse_id AND stock_category='sellable') THEN
  SELECT * INTO j FROM erp.return_repair_jobs WHERE company_id=NEW.company_id
  AND id=NEW.repair_job_id FOR UPDATE;
  IF j.status IS DISTINCT FROM 'qc_passed' OR j.sales_return_line_id<>NEW.sales_return_line_id
   OR j.warehouse_id<>NEW.from_warehouse_id OR NEW.action_date<j.qc_at::date THEN
   RAISE EXCEPTION 'Sellable release requires matching passed QC'; END IF;
  SELECT coalesce(sum(quantity),0) INTO v_used FROM erp.sales_return_stock_actions
  WHERE company_id=NEW.company_id AND repair_job_id=j.id;
  IF v_used+NEW.quantity>j.quantity THEN RAISE EXCEPTION 'QC quantity already consumed'; END IF;
 ELSIF NEW.repair_job_id IS NOT NULL THEN
  RAISE EXCEPTION 'QC approval applies only to sellable releases';
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER trg_return_quality_release BEFORE INSERT ON erp.sales_return_stock_actions
 FOR EACH ROW EXECUTE FUNCTION erp.require_return_quality_release();

-- Ordinary transfer commands cannot bypass the typed returned-stock QC protocol.
CREATE FUNCTION erp.block_unchecked_quality_transfer() RETURNS trigger LANGUAGE plpgsql
SET search_path=pg_catalog,erp AS $$
BEGIN
 IF NEW.quantity_delta>0 AND NEW.inventory_document_line_id IS NOT NULL AND EXISTS(
  SELECT 1 FROM erp.inventory_document_lines l JOIN erp.warehouses source
  ON source.company_id=l.company_id AND source.id=l.from_warehouse_id
  JOIN erp.warehouses destination ON destination.company_id=l.company_id
  AND destination.id=l.to_warehouse_id WHERE l.company_id=NEW.company_id
  AND l.id=NEW.inventory_document_line_id AND source.stock_category<>'sellable'
  AND destination.stock_category='sellable') THEN
  RAISE EXCEPTION 'Use typed QC release for segregated stock';
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER trg_stock_quality_transfer BEFORE INSERT ON erp.stock_movements
 FOR EACH ROW EXECUTE FUNCTION erp.block_unchecked_quality_transfer();

CREATE FUNCTION erp.guard_credit_target_currency() RETURNS trigger LANGUAGE plpgsql
SET search_path=pg_catalog,erp AS $$
DECLARE original record;target record;
BEGIN
 SELECT i.* INTO original FROM erp.sales_returns r JOIN erp.sales_invoices i
 ON i.company_id=r.company_id AND i.id=r.sales_invoice_id WHERE r.company_id=NEW.company_id
 AND r.id=NEW.sales_return_id;
 SELECT * INTO target FROM erp.sales_invoices WHERE company_id=NEW.company_id
 AND id=NEW.sales_invoice_id;
 IF (original.partner_id,original.currency_code,original.exchange_rate) IS DISTINCT FROM
    (target.partner_id,target.currency_code,target.exchange_rate)
 OR (original.id<>target.id AND original.partner_id IS NULL) THEN
  RAISE EXCEPTION 'Credit target customer/currency mismatch'; END IF;
 IF ARRAY(SELECT l.account_id FROM erp.journal_lines l JOIN erp.accounts a
 ON a.company_id=l.company_id AND a.id=l.account_id WHERE l.company_id=NEW.company_id
 AND l.journal_entry_id=original.journal_entry_id AND a.account_type='receivable' ORDER BY l.id)
 IS DISTINCT FROM ARRAY(SELECT l.account_id FROM erp.journal_lines l JOIN erp.accounts a
 ON a.company_id=l.company_id AND a.id=l.account_id WHERE l.company_id=NEW.company_id
 AND l.journal_entry_id=target.journal_entry_id AND a.account_type='receivable' ORDER BY l.id) THEN
  RAISE EXCEPTION 'Credit target receivable mismatch'; END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER trg_credit_target_currency BEFORE INSERT ON erp.sales_return_credit_applications
 FOR EACH ROW EXECUTE FUNCTION erp.guard_credit_target_currency();
"""

REVERSE = r"""
DROP TRIGGER trg_credit_target_currency ON erp.sales_return_credit_applications;
DROP FUNCTION erp.guard_credit_target_currency();
DROP TRIGGER trg_stock_quality_transfer ON erp.stock_movements;
DROP FUNCTION erp.block_unchecked_quality_transfer();
DROP TRIGGER trg_return_quality_release ON erp.sales_return_stock_actions;
DROP FUNCTION erp.require_return_quality_release();
ALTER TABLE erp.sales_return_stock_actions DROP COLUMN repair_job_id;
DROP TABLE erp.return_repair_jobs;
DROP FUNCTION erp.guard_return_repair_job();
"""


class Migration(migrations.Migration):
    dependencies = [("database", "0033_runtime_release_guards")]
    operations = [migrations.RunSQL(SQL, REVERSE)]
