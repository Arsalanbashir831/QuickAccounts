from django.db import migrations

SQL = r"""
ALTER TABLE erp.warehouses
    ADD COLUMN stock_category text NOT NULL DEFAULT 'sellable'
        CHECK(stock_category IN ('sellable','quarantine','damaged','supplier_return')),
    ADD COLUMN row_version bigint NOT NULL DEFAULT 1 CHECK(row_version>0);

CREATE FUNCTION erp.guard_warehouse_configuration() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,erp AS $$
BEGIN
    IF TG_OP='DELETE' THEN
        RAISE EXCEPTION 'Deactivate warehouses; do not delete their history'
            USING ERRCODE='23514', CONSTRAINT='warehouse_history_required';
    END IF;
    IF NEW.company_id IS DISTINCT FROM OLD.company_id OR NEW.id IS DISTINCT FROM OLD.id THEN
        RAISE EXCEPTION 'Warehouse scope is immutable'
            USING ERRCODE='23514', CONSTRAINT='warehouse_scope_immutable';
    END IF;
    IF NEW.stock_category IS DISTINCT FROM OLD.stock_category AND (
        EXISTS(SELECT 1 FROM erp.stock_movements WHERE company_id=OLD.company_id
            AND warehouse_id=OLD.id) OR
        EXISTS(SELECT 1 FROM erp.inventory_positions WHERE company_id=OLD.company_id
            AND warehouse_id=OLD.id AND (on_hand_quantity<>0 OR reserved_quantity<>0
                OR value_company<>0)) OR
        EXISTS(SELECT 1 FROM erp.inventory_reservations WHERE company_id=OLD.company_id
            AND warehouse_id=OLD.id)
    ) THEN
        RAISE EXCEPTION 'Use stock transfers rather than relabeling warehouse history'
            USING ERRCODE='23514', CONSTRAINT='warehouse_category_in_use';
    END IF;
    IF NOT NEW.is_active AND OLD.is_active AND EXISTS(
        SELECT 1 FROM erp.inventory_positions WHERE company_id=OLD.company_id
            AND warehouse_id=OLD.id AND (on_hand_quantity<>0 OR reserved_quantity<>0
                OR value_company<>0)
    ) THEN
        RAISE EXCEPTION 'Warehouse still holds stock, reservations or value'
            USING ERRCODE='23514', CONSTRAINT='warehouse_balance_in_use';
    END IF;
    NEW.row_version:=OLD.row_version+1;
    RETURN NEW;
END $$;
CREATE TRIGGER trg_warehouse_configuration BEFORE UPDATE OR DELETE ON erp.warehouses
    FOR EACH ROW EXECUTE FUNCTION erp.guard_warehouse_configuration();

CREATE FUNCTION erp.audit_warehouse_configuration() RETURNS trigger LANGUAGE plpgsql
SECURITY DEFINER SET search_path=pg_catalog,erp,identity AS $$
DECLARE v_tenant uuid;
BEGIN
    SELECT tenant_id INTO STRICT v_tenant FROM erp.companies WHERE id=NEW.company_id;
    INSERT INTO erp.company_audit_events(tenant_id,company_id,actor_user_id,actor_service_id,
        action,object_type,object_id,old_data,new_data,request_id)
    VALUES(v_tenant,NEW.company_id,identity.current_user_id(),identity.current_service_id(),
        CASE WHEN TG_OP='INSERT' THEN 'warehouse.created' ELSE 'warehouse.updated' END,
        'warehouse',NEW.id::text,CASE WHEN TG_OP='INSERT' THEN NULL ELSE to_jsonb(OLD) END,
        to_jsonb(NEW),nullif(current_setting('app.request_id',true),''));
    RETURN NEW;
END $$;
CREATE TRIGGER trg_warehouse_audit AFTER INSERT OR UPDATE ON erp.warehouses
    FOR EACH ROW EXECUTE FUNCTION erp.audit_warehouse_configuration();

CREATE FUNCTION erp.guard_stock_warehouse() RETURNS trigger LANGUAGE plpgsql
SET search_path=pg_catalog,erp AS $$
DECLARE v_active boolean; v_category text; v_sale boolean;
BEGIN
    -- The share lock serializes movements/reservations against classification changes.
    SELECT is_active,stock_category INTO v_active,v_category FROM erp.warehouses
        WHERE company_id=NEW.company_id AND id=NEW.warehouse_id FOR SHARE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'Warehouse unavailable' USING ERRCODE='23514';
    END IF;
    IF TG_TABLE_NAME='inventory_reservations' THEN
        IF NEW.status='active' AND (NOT v_active OR v_category<>'sellable') THEN
            RAISE EXCEPTION 'Cannot reserve non-sellable stock' USING ERRCODE='23514';
        END IF;
    ELSE
        IF NOT v_active THEN
            RAISE EXCEPTION 'Cannot move stock in an inactive warehouse' USING ERRCODE='23514';
        END IF;
        v_sale:=NEW.source_type='sales_invoice' OR EXISTS(
            SELECT 1 FROM erp.inventory_documents d
            JOIN erp.inventory_document_lines l ON l.company_id=d.company_id
                AND l.inventory_document_id=d.id
            WHERE l.company_id=NEW.company_id AND l.id=NEW.inventory_document_line_id
                AND d.document_kind='shipment');
        IF NEW.quantity_delta<0 AND coalesce(v_sale,false) AND v_category<>'sellable' THEN
            RAISE EXCEPTION 'Cannot sell non-sellable stock' USING ERRCODE='23514';
        END IF;
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER trg_stock_warehouse BEFORE INSERT ON erp.stock_movements
    FOR EACH ROW EXECUTE FUNCTION erp.guard_stock_warehouse();
CREATE TRIGGER trg_reservation_warehouse BEFORE INSERT OR UPDATE ON erp.inventory_reservations
    FOR EACH ROW EXECUTE FUNCTION erp.guard_stock_warehouse();

CREATE OR REPLACE VIEW erp.v_inventory_availability WITH(security_invoker=true) AS
SELECT p.company_id,p.warehouse_id,p.item_id,p.lot_id,p.on_hand_quantity,p.reserved_quantity,
    CASE WHEN w.is_active AND w.stock_category='sellable'
        THEN p.on_hand_quantity-p.reserved_quantity ELSE 0::numeric END AS available_quantity,
    p.value_company,p.updated_at
FROM erp.inventory_positions p JOIN erp.warehouses w
    ON w.company_id=p.company_id AND w.id=p.warehouse_id;

INSERT INTO identity.permissions(code,description) VALUES
    ('inventory.view','View warehouses and inventory balances'),
    ('inventory.manage','Manage company warehouses') ON CONFLICT DO NOTHING;
"""

REVERSE = r"""
CREATE OR REPLACE VIEW erp.v_inventory_availability WITH(security_invoker=true) AS
SELECT company_id,warehouse_id,item_id,lot_id,on_hand_quantity,reserved_quantity,
    on_hand_quantity-reserved_quantity AS available_quantity,value_company,updated_at
FROM erp.inventory_positions;
DROP TRIGGER trg_reservation_warehouse ON erp.inventory_reservations;
DROP TRIGGER trg_stock_warehouse ON erp.stock_movements;
DROP FUNCTION erp.guard_stock_warehouse();
DROP TRIGGER trg_warehouse_audit ON erp.warehouses;
DROP FUNCTION erp.audit_warehouse_configuration();
DROP TRIGGER trg_warehouse_configuration ON erp.warehouses;
DROP FUNCTION erp.guard_warehouse_configuration();
ALTER TABLE erp.warehouses DROP COLUMN stock_category,DROP COLUMN row_version;
"""


class Migration(migrations.Migration):
    dependencies = [("database", "0015_phase5_payment_settlement")]
    operations = [migrations.RunSQL(SQL, REVERSE)]
