from django.db import migrations

SQL = r"""
CREATE FUNCTION erp.stock_maintenance_lock() RETURNS trigger LANGUAGE plpgsql
SET search_path=pg_catalog AS $$
BEGIN
    PERFORM pg_advisory_xact_lock_shared(hashtextextended('inventory-rebuild:'||NEW.company_id,0));
    RETURN NEW;
END $$;
CREATE TRIGGER trg_00_stock_maintenance BEFORE INSERT ON erp.stock_movements
    FOR EACH ROW EXECUTE FUNCTION erp.stock_maintenance_lock();
CREATE TRIGGER trg_00_reservation_maintenance BEFORE INSERT OR UPDATE ON erp.inventory_reservations
    FOR EACH ROW EXECUTE FUNCTION erp.stock_maintenance_lock();
CREATE FUNCTION erp.rebuild_inventory_positions(p_company uuid,p_product uuid) RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
DECLARE v_count integer;
BEGIN
    PERFORM erp.assert_company_write(p_company,p_product,'inventory','inventory.reconcile');
    IF NOT pg_try_advisory_xact_lock(hashtextextended('inventory-rebuild:'||p_company,0)) THEN
        RAISE EXCEPTION 'Inventory is busy; retry reconciliation'; END IF;
    LOCK TABLE erp.stock_movements,erp.inventory_reservations IN SHARE ROW EXCLUSIVE MODE NOWAIT;
    PERFORM 1 FROM erp.inventory_positions WHERE company_id=p_company
        ORDER BY warehouse_id,item_id,lot_id FOR UPDATE NOWAIT;
    WITH stock AS (
        SELECT warehouse_id,item_id,lot_id,sum(quantity_delta) quantity,
            sum(value_delta_company) value FROM erp.stock_movements WHERE company_id=p_company
        GROUP BY warehouse_id,item_id,lot_id
    ), reserved AS (
        SELECT warehouse_id,item_id,lot_id,sum(quantity) quantity FROM erp.inventory_reservations
        WHERE company_id=p_company AND status='active' GROUP BY warehouse_id,item_id,lot_id
    ), scopes AS (
        SELECT warehouse_id,item_id,lot_id FROM stock UNION
        SELECT warehouse_id,item_id,lot_id FROM reserved UNION
        SELECT warehouse_id,item_id,lot_id FROM erp.inventory_positions WHERE company_id=p_company
    ) INSERT INTO erp.inventory_positions(company_id,warehouse_id,item_id,lot_id,
        on_hand_quantity,reserved_quantity,value_company)
    SELECT p_company,x.warehouse_id,x.item_id,x.lot_id,coalesce(s.quantity,0),
        coalesce(r.quantity,0),coalesce(s.value,0) FROM scopes x
    LEFT JOIN stock s ON s.warehouse_id=x.warehouse_id AND s.item_id=x.item_id
        AND s.lot_id IS NOT DISTINCT FROM x.lot_id
    LEFT JOIN reserved r ON r.warehouse_id=x.warehouse_id AND r.item_id=x.item_id
        AND r.lot_id IS NOT DISTINCT FROM x.lot_id
    ON CONFLICT(company_id,warehouse_id,item_id,lot_id) DO UPDATE SET
        on_hand_quantity=excluded.on_hand_quantity,reserved_quantity=excluded.reserved_quantity,
        value_company=excluded.value_company,updated_at=clock_timestamp();
    GET DIAGNOSTICS v_count=ROW_COUNT;
    INSERT INTO erp.company_audit_events(tenant_id,company_id,actor_user_id,action,
        object_type,object_id,new_data,request_id)
    VALUES(erp.current_tenant_id(),p_company,identity.current_user_id(),'inventory.rebuilt',
        'inventory_positions',p_company::text,jsonb_build_object('scope_count',v_count),
        nullif(current_setting('app.request_id',true),''));
    RETURN v_count;
END $$;
REVOKE ALL ON FUNCTION erp.rebuild_inventory_positions(uuid,uuid) FROM PUBLIC;
DO $$ BEGIN
    IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='quickaccounts_runtime') THEN
        GRANT EXECUTE ON FUNCTION erp.rebuild_inventory_positions(uuid,uuid)
            TO quickaccounts_runtime;
    END IF;
END $$;
"""

REVERSE = r"""
DROP TRIGGER trg_00_stock_maintenance ON erp.stock_movements;
DROP TRIGGER trg_00_reservation_maintenance ON erp.inventory_reservations;
DROP FUNCTION erp.stock_maintenance_lock(),erp.rebuild_inventory_positions(uuid,uuid);
"""


class Migration(migrations.Migration):
    dependencies = [("database", "0020_phase58_stock_commands")]
    operations = [migrations.RunSQL(SQL, REVERSE)]
