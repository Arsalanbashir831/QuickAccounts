from django.db import migrations

SQL = r"""
ALTER TABLE erp.sales_orders ADD COLUMN confirmed_at timestamptz;
ALTER TABLE erp.sales_channels ADD COLUMN row_version bigint NOT NULL DEFAULT 1
 CHECK(row_version>0);
CREATE UNIQUE INDEX uq_order_invoice ON erp.sales_invoices(company_id,sales_order_id)
 WHERE sales_order_id IS NOT NULL AND document_kind='invoice' AND status<>'void';
CREATE TABLE erp.sales_deliveries(
 id uuid PRIMARY KEY DEFAULT uuidv7(),company_id uuid NOT NULL,sales_invoice_id uuid NOT NULL,
 delivered_date date NOT NULL,delivery_reference text NOT NULL
 CHECK(length(btrim(delivery_reference))>0),received_by text NOT NULL
 CHECK(length(btrim(received_by))>0),notes text NOT NULL CHECK(length(btrim(notes))>0),
 confirmed_by uuid NOT NULL REFERENCES identity.users(id),confirmed_at timestamptz NOT NULL
 DEFAULT clock_timestamp(),UNIQUE(company_id,id),UNIQUE(company_id,sales_invoice_id),
 FOREIGN KEY(company_id,sales_invoice_id) REFERENCES erp.sales_invoices(company_id,id)
);
ALTER TABLE erp.sales_deliveries ENABLE ROW LEVEL SECURITY;
CREATE POLICY company_scope ON erp.sales_deliveries USING(EXISTS(SELECT 1 FROM erp.companies c
 WHERE c.id=company_id AND c.tenant_id=erp.current_tenant_id())) WITH CHECK(EXISTS(
 SELECT 1 FROM erp.companies c WHERE c.id=company_id AND c.tenant_id=erp.current_tenant_id()));
INSERT INTO identity.permissions(code,description) VALUES
 ('sales.order.manage','Manage customer sales orders'),
 ('sales.delivery.confirm','Confirm completed customer delivery') ON CONFLICT DO NOTHING;

CREATE FUNCTION erp.guard_sales_order() RETURNS trigger LANGUAGE plpgsql
SET search_path=pg_catalog,erp,identity AS $$
BEGIN
 IF TG_OP='DELETE' THEN RAISE EXCEPTION 'Cancel orders instead of deleting history'; END IF;
 IF TG_OP='INSERT' THEN
 IF NEW.status<>'draft' OR NEW.confirmed_at IS NOT NULL THEN
 RAISE EXCEPTION 'Order must start draft'; END IF;
 ELSIF NEW.status='fulfilled' THEN
 IF OLD.status<>'confirmed' OR NOT identity.has_company_permission(NEW.company_id,
 'sales.delivery.confirm') OR NOT EXISTS(SELECT 1 FROM erp.sales_deliveries d
 JOIN erp.sales_invoices i ON i.company_id=d.company_id AND i.id=d.sales_invoice_id
 WHERE i.company_id=NEW.company_id AND i.sales_order_id=NEW.id AND i.status='posted') THEN
 RAISE EXCEPTION 'Fulfilled order requires confirmed delivery'; END IF;
 END IF;
 IF NEW.status<>'fulfilled' AND NOT identity.has_company_permission(NEW.company_id,
 'sales.order.manage') THEN RAISE EXCEPTION 'Order permission denied' USING ERRCODE='42501'; END IF;
 IF TG_OP='UPDATE' THEN
 IF NEW.id<>OLD.id OR NEW.company_id<>OLD.company_id OR OLD.status IN ('fulfilled','cancelled') OR
 (OLD.status<>'draft' AND (NEW.order_no,NEW.partner_id,NEW.channel_id,NEW.order_date,
 NEW.currency_code,NEW.external_ref) IS DISTINCT FROM (OLD.order_no,OLD.partner_id,OLD.channel_id,
 OLD.order_date,OLD.currency_code,OLD.external_ref)) THEN RAISE EXCEPTION 'Order source immutable';
 END IF;
 IF NEW.status<>OLD.status AND NOT ((OLD.status='draft' AND NEW.status IN ('confirmed','cancelled'))
 OR (OLD.status='confirmed' AND NEW.status IN ('fulfilled','cancelled'))) THEN
 RAISE EXCEPTION 'Invalid order transition'; END IF;
 IF NEW.status='cancelled' AND EXISTS(SELECT 1 FROM erp.sales_invoices
 WHERE company_id=NEW.company_id
 AND sales_order_id=NEW.id AND status<>'void') THEN
 RAISE EXCEPTION 'Cannot cancel invoiced order'; END IF;
 NEW.confirmed_at:=OLD.confirmed_at;
 IF OLD.status='draft' AND NEW.status='confirmed' THEN
 NEW.confirmed_at:=clock_timestamp(); END IF;
 NEW.row_version:=OLD.row_version+1;NEW.updated_at:=clock_timestamp();
 END IF;
 IF NEW.status IN ('draft','confirmed') THEN
 IF NOT EXISTS(SELECT 1 FROM erp.business_partners WHERE company_id=NEW.company_id
 AND id=NEW.partner_id AND is_active AND partner_kind IN ('customer','both'))
 OR NOT EXISTS(SELECT 1 FROM erp.sales_channels WHERE company_id=NEW.company_id
 AND id=NEW.channel_id AND is_active) THEN
 RAISE EXCEPTION 'Active customer and channel required'; END IF;
 END IF;
 IF NEW.status='confirmed' AND NOT EXISTS(SELECT 1 FROM erp.sales_order_lines
 WHERE company_id=NEW.company_id AND sales_order_id=NEW.id) THEN
 RAISE EXCEPTION 'Cannot confirm empty order'; END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER trg_sales_order_guard BEFORE INSERT OR UPDATE OR DELETE ON erp.sales_orders
 FOR EACH ROW EXECUTE FUNCTION erp.guard_sales_order();
CREATE TRIGGER trg_sales_order_audit AFTER INSERT OR UPDATE ON erp.sales_orders
 FOR EACH ROW EXECUTE FUNCTION erp.audit_sales_return();

CREATE FUNCTION erp.guard_sales_order_line() RETURNS trigger LANGUAGE plpgsql
SET search_path=pg_catalog,erp,identity AS $$
DECLARE c uuid;o uuid;
BEGIN
 IF TG_OP='DELETE' THEN c:=OLD.company_id;o:=OLD.sales_order_id;
 ELSE c:=NEW.company_id;o:=NEW.sales_order_id; END IF;
 IF TG_OP='UPDATE' AND (NEW.company_id,NEW.sales_order_id,NEW.id) IS DISTINCT FROM
 (OLD.company_id,OLD.sales_order_id,OLD.id) THEN
 RAISE EXCEPTION 'Order line identity immutable'; END IF;
 IF NOT identity.has_company_permission(c,'sales.order.manage') THEN
 RAISE EXCEPTION 'Order line permission denied' USING ERRCODE='42501'; END IF;
 PERFORM 1 FROM erp.sales_orders WHERE company_id=c AND id=o AND status='draft' FOR UPDATE;
 IF NOT FOUND THEN RAISE EXCEPTION 'Only draft order lines can change'; END IF;
 IF TG_OP='DELETE' THEN RETURN OLD; END IF;
 IF NOT EXISTS(SELECT 1 FROM erp.items WHERE company_id=c AND id=NEW.item_id AND is_active) THEN
 RAISE EXCEPTION 'Order item inactive'; END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER trg_sales_order_line_guard BEFORE INSERT OR UPDATE OR DELETE ON erp.sales_order_lines
 FOR EACH ROW EXECUTE FUNCTION erp.guard_sales_order_line();

CREATE FUNCTION erp.guard_order_invoice() RETURNS trigger LANGUAGE plpgsql
SET search_path=pg_catalog,erp AS $$
DECLARE o record;minor integer;
BEGIN
 IF TG_OP='UPDATE' AND OLD.status='posted' AND NEW.sales_order_id IS DISTINCT FROM
 OLD.sales_order_id THEN RAISE EXCEPTION 'Posted invoice order immutable'; END IF;
 IF NEW.sales_order_id IS NOT NULL AND NEW.document_kind='invoice' THEN
 SELECT * INTO o FROM erp.sales_orders WHERE company_id=NEW.company_id AND id=NEW.sales_order_id
 FOR SHARE;
 IF o.status IS DISTINCT FROM 'confirmed' OR o.partner_id IS DISTINCT FROM NEW.partner_id OR
 o.currency_code<>NEW.currency_code OR NEW.issue_date<o.order_date THEN
 RAISE EXCEPTION 'Invoice must match confirmed order'; END IF;
 IF NEW.status='posted' THEN
 SELECT minor_units INTO minor FROM erp.currencies WHERE code=NEW.currency_code;
 IF (SELECT count(*) FROM erp.sales_order_lines WHERE company_id=NEW.company_id
 AND sales_order_id=o.id)<>(SELECT count(*) FROM erp.sales_invoice_lines
 WHERE company_id=NEW.company_id AND sales_invoice_id=NEW.id) OR EXISTS(
 SELECT 1 FROM erp.sales_order_lines l LEFT JOIN erp.sales_invoice_lines i
 ON i.company_id=l.company_id AND i.sales_invoice_id=NEW.id AND i.line_no=l.line_no
 WHERE l.company_id=NEW.company_id AND l.sales_order_id=o.id AND
 (i.item_id,i.quantity,i.unit_price,i.discount_amount,i.tax_code_id) IS DISTINCT FROM
 (l.item_id,l.ordered_quantity,l.unit_price,
 round(l.ordered_quantity*l.unit_price*l.discount_percent/100,minor),l.tax_code_id)) THEN
 RAISE EXCEPTION 'Invoice lines differ from confirmed order'; END IF;
 END IF;
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER trg_order_invoice_guard BEFORE INSERT OR UPDATE ON erp.sales_invoices
 FOR EACH ROW EXECUTE FUNCTION erp.guard_order_invoice();

CREATE FUNCTION erp.guard_sales_delivery() RETURNS trigger LANGUAGE plpgsql
SET search_path=pg_catalog,erp,identity AS $$
DECLARE i record;
BEGIN
 IF TG_OP<>'INSERT' THEN RAISE EXCEPTION 'Delivery evidence immutable'; END IF;
 IF NOT identity.has_company_permission(NEW.company_id,'sales.delivery.confirm') THEN
 RAISE EXCEPTION 'Delivery permission denied' USING ERRCODE='42501'; END IF;
 SELECT * INTO i FROM erp.sales_invoices WHERE company_id=NEW.company_id AND id=NEW.sales_invoice_id
 FOR UPDATE;
 IF i.status IS DISTINCT FROM 'posted' OR i.document_kind<>'invoice' OR
 NEW.delivered_date<i.issue_date THEN RAISE EXCEPTION 'Delivery requires posted sale'; END IF;
 IF EXISTS(SELECT 1 FROM erp.sales_invoice_lines l JOIN erp.items item
 ON item.company_id=l.company_id AND item.id=l.item_id WHERE l.company_id=NEW.company_id
 AND l.sales_invoice_id=i.id AND item.item_kind='stock' AND l.quantity>
 (SELECT coalesce(-sum(quantity_delta),0) FROM erp.stock_movements WHERE company_id=l.company_id
 AND source_type='sales_invoice' AND source_id=i.id AND source_line_id=l.id AND quantity_delta<0))
 OR EXISTS(SELECT 1 FROM erp.stock_movements WHERE company_id=NEW.company_id
 AND source_type='sales_invoice' AND source_id=i.id AND quantity_delta<0
 AND occurred_at::date>NEW.delivered_date) THEN
 RAISE EXCEPTION 'Delivery requires completed stock fulfillment before delivery date'; END IF;
 NEW.confirmed_by:=identity.current_user_id();NEW.confirmed_at:=clock_timestamp();
 RETURN NEW;
END $$;
CREATE TRIGGER trg_sales_delivery_guard BEFORE INSERT OR UPDATE OR DELETE ON erp.sales_deliveries
 FOR EACH ROW EXECUTE FUNCTION erp.guard_sales_delivery();
CREATE TRIGGER trg_sales_delivery_audit AFTER INSERT ON erp.sales_deliveries
 FOR EACH ROW EXECUTE FUNCTION erp.audit_sales_return();

CREATE FUNCTION erp.guard_sales_channel() RETURNS trigger LANGUAGE plpgsql
SET search_path=pg_catalog,identity AS $$
BEGIN
 IF TG_OP='DELETE' THEN RAISE EXCEPTION 'Deactivate sales channels instead'; END IF;
 IF NOT identity.has_company_permission(NEW.company_id,'sales.order.manage') THEN
 RAISE EXCEPTION 'Channel permission denied' USING ERRCODE='42501'; END IF;
 IF TG_OP='UPDATE' THEN
 IF (NEW.id,NEW.company_id) IS DISTINCT FROM (OLD.id,OLD.company_id) THEN
 RAISE EXCEPTION 'Channel identity immutable'; END IF;
 NEW.row_version:=OLD.row_version+1;NEW.updated_at:=clock_timestamp(); END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER trg_sales_channel_guard BEFORE INSERT OR UPDATE OR DELETE ON erp.sales_channels
 FOR EACH ROW EXECUTE FUNCTION erp.guard_sales_channel();
CREATE TRIGGER trg_sales_channel_audit AFTER INSERT OR UPDATE ON erp.sales_channels
 FOR EACH ROW EXECUTE FUNCTION erp.audit_sales_return();
"""

REVERSE = r"""
DO $$ BEGIN IF EXISTS(SELECT 1 FROM erp.sales_deliveries) OR
 EXISTS(SELECT 1 FROM erp.sales_orders WHERE confirmed_at IS NOT NULL) OR
 EXISTS(SELECT 1 FROM erp.sales_channels WHERE row_version>1) THEN
 RAISE EXCEPTION 'Cannot reverse retained order/delivery history'; END IF; END $$;
DROP TRIGGER trg_sales_channel_audit ON erp.sales_channels;
DROP TRIGGER trg_sales_channel_guard ON erp.sales_channels;
DROP FUNCTION erp.guard_sales_channel();
DROP TRIGGER trg_order_invoice_guard ON erp.sales_invoices;
DROP FUNCTION erp.guard_order_invoice();
DROP TRIGGER trg_sales_order_audit ON erp.sales_orders;
DROP TRIGGER trg_sales_order_guard ON erp.sales_orders;
DROP FUNCTION erp.guard_sales_order();
DROP TRIGGER trg_sales_order_line_guard ON erp.sales_order_lines;
DROP FUNCTION erp.guard_sales_order_line();
DROP TABLE erp.sales_deliveries;
DROP FUNCTION erp.guard_sales_delivery();
DROP INDEX erp.uq_order_invoice;
ALTER TABLE erp.sales_orders DROP COLUMN confirmed_at;
ALTER TABLE erp.sales_channels DROP COLUMN row_version;
"""


class Migration(migrations.Migration):
    dependencies = [("database", "0036_stock_disposal_cases")]
    operations = [migrations.RunSQL(SQL, REVERSE)]
