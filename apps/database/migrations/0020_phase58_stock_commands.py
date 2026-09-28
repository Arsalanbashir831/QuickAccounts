from django.db import migrations

SQL = r"""
ALTER TABLE erp.sales_invoices ADD COLUMN stock_fulfillment text NOT NULL DEFAULT 'immediate'
    CHECK(stock_fulfillment IN ('immediate','deferred'));
ALTER TABLE erp.inventory_documents ADD COLUMN reason text NOT NULL DEFAULT '';
ALTER TABLE erp.inventory_document_lines ADD COLUMN sales_invoice_line_id uuid;
ALTER TABLE erp.inventory_document_lines ADD CONSTRAINT fk_inventory_invoice_line
    FOREIGN KEY(company_id,sales_invoice_line_id) REFERENCES erp.sales_invoice_lines(company_id,id);
CREATE INDEX ix_inventory_invoice_line ON erp.inventory_document_lines
    (company_id,sales_invoice_line_id) WHERE sales_invoice_line_id IS NOT NULL;
ALTER TABLE erp.inventory_reservations ADD COLUMN row_version bigint NOT NULL DEFAULT 1;

CREATE FUNCTION erp.guard_typed_reservation() RETURNS trigger LANGUAGE plpgsql
SET search_path=pg_catalog,erp AS $$
DECLARE l record;d record;v_reserved numeric;
BEGIN
    IF TG_OP='DELETE' THEN RAISE EXCEPTION 'Release reservations instead of deleting'; END IF;
    IF TG_OP='UPDATE' THEN
        IF OLD.status<>'active' OR NEW.quantity<>OLD.quantity OR NEW.status='active' THEN
            RAISE EXCEPTION 'Only active reservation release/consumption is allowed'; END IF;
        NEW.row_version:=OLD.row_version+1;
        RETURN NEW;
    END IF;
    IF NEW.source_type<>'inventory_document_line' OR NEW.status<>'active' THEN
        RAISE EXCEPTION 'Reservation requires a typed draft stock line'; END IF;
    SELECT * INTO l FROM erp.inventory_document_lines WHERE company_id=NEW.company_id
        AND id=NEW.source_id;
    SELECT * INTO d FROM erp.inventory_documents WHERE company_id=l.company_id
        AND id=l.inventory_document_id FOR UPDATE;
    IF d.status IS DISTINCT FROM 'draft' OR l.from_warehouse_id IS DISTINCT FROM NEW.warehouse_id
        OR l.item_id IS DISTINCT FROM NEW.item_id OR l.lot_id IS DISTINCT FROM NEW.lot_id THEN
        RAISE EXCEPTION 'Reservation source/scope mismatch'; END IF;
    PERFORM 1 FROM erp.inventory_positions WHERE company_id=NEW.company_id
        AND warehouse_id=NEW.warehouse_id AND item_id=NEW.item_id
        AND lot_id IS NOT DISTINCT FROM NEW.lot_id FOR UPDATE;
    SELECT coalesce(sum(quantity),0) INTO v_reserved FROM erp.inventory_reservations
        WHERE company_id=NEW.company_id AND source_type=NEW.source_type AND source_id=NEW.source_id
        AND status='active';
    IF v_reserved+NEW.quantity>l.quantity THEN RAISE EXCEPTION 'Source over-reserved'; END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER trg_typed_reservation BEFORE INSERT OR UPDATE OR DELETE
    ON erp.inventory_reservations FOR EACH ROW EXECUTE FUNCTION erp.guard_typed_reservation();

CREATE FUNCTION erp.guard_stock_tracking() RETURNS trigger LANGUAGE plpgsql
SET search_path=pg_catalog,erp AS $$
DECLARE i record;l record;
BEGIN
    SELECT * INTO i FROM erp.items WHERE company_id=NEW.company_id AND id=NEW.item_id FOR SHARE;
    IF i.item_kind<>'stock' OR NOT i.is_active THEN
        RAISE EXCEPTION 'Stock movements require active stock items'; END IF;
    IF (i.track_lots OR i.track_serials) AND NEW.lot_id IS NULL THEN
        RAISE EXCEPTION 'Tracked stock requires a lot'; END IF;
    IF NOT (i.track_lots OR i.track_serials) AND NEW.lot_id IS NOT NULL THEN
        RAISE EXCEPTION 'Untracked stock cannot select a lot'; END IF;
    IF i.track_serials THEN
        SELECT * INTO l FROM erp.inventory_lots WHERE company_id=NEW.company_id AND id=NEW.lot_id;
        IF l.serial_code IS NULL OR abs(NEW.quantity_delta)<>1 THEN
            RAISE EXCEPTION 'Serial movement requires one serial unit'; END IF;
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER trg_stock_tracking BEFORE INSERT ON erp.stock_movements
    FOR EACH ROW EXECUTE FUNCTION erp.guard_stock_tracking();
CREATE UNIQUE INDEX uq_inventory_serial ON erp.inventory_lots(company_id,item_id,serial_code)
    WHERE serial_code IS NOT NULL;

CREATE FUNCTION erp.audit_inventory_command() RETURNS trigger LANGUAGE plpgsql
SECURITY DEFINER SET search_path=pg_catalog,erp AS $$
DECLARE v_tenant uuid;
BEGIN
    SELECT tenant_id INTO STRICT v_tenant FROM erp.companies WHERE id=NEW.company_id;
    IF v_tenant IS DISTINCT FROM erp.current_tenant_id() THEN
        RAISE EXCEPTION 'Inventory audit tenant context mismatch' USING ERRCODE='42501'; END IF;
    INSERT INTO erp.company_audit_events(tenant_id,company_id,actor_user_id,action,
        object_type,object_id,old_data,new_data,request_id)
    VALUES(v_tenant,NEW.company_id,identity.current_user_id(),TG_TABLE_NAME||'.changed',
        TG_TABLE_NAME,NEW.id::text,CASE WHEN TG_OP='UPDATE' THEN to_jsonb(OLD) END,
        to_jsonb(NEW),nullif(current_setting('app.request_id',true),''));
    RETURN NEW;
END $$;
CREATE TRIGGER trg_inventory_document_audit AFTER INSERT OR UPDATE ON erp.inventory_documents
    FOR EACH ROW EXECUTE FUNCTION erp.audit_inventory_command();
CREATE TRIGGER trg_inventory_reservation_audit AFTER INSERT OR UPDATE ON erp.inventory_reservations
    FOR EACH ROW EXECUTE FUNCTION erp.audit_inventory_command();
CREATE TRIGGER trg_inventory_lot_audit AFTER INSERT ON erp.inventory_lots
    FOR EACH ROW EXECUTE FUNCTION erp.audit_inventory_command();
INSERT INTO identity.permissions(code,description) VALUES
    ('inventory.reserve','Reserve and release stock'),
    ('inventory.reconcile','Reconcile inventory projections') ON CONFLICT DO NOTHING;
"""

REVERSE = r"""
DROP TRIGGER trg_inventory_document_audit ON erp.inventory_documents;
DROP TRIGGER trg_inventory_reservation_audit ON erp.inventory_reservations;
DROP TRIGGER trg_inventory_lot_audit ON erp.inventory_lots;
DROP FUNCTION erp.audit_inventory_command();
DROP TRIGGER trg_typed_reservation ON erp.inventory_reservations;
DROP FUNCTION erp.guard_typed_reservation();
DROP TRIGGER trg_stock_tracking ON erp.stock_movements;
DROP FUNCTION erp.guard_stock_tracking();
DROP INDEX erp.uq_inventory_serial;
ALTER TABLE erp.inventory_reservations DROP COLUMN row_version;
ALTER TABLE erp.inventory_document_lines DROP COLUMN sales_invoice_line_id;
ALTER TABLE erp.inventory_documents DROP COLUMN reason;
ALTER TABLE erp.sales_invoices DROP COLUMN stock_fulfillment;
"""


class Migration(migrations.Migration):
    dependencies = [("database", "0019_return_open_item_views")]
    operations = [migrations.RunSQL(SQL, REVERSE)]
