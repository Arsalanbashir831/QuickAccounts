from django.db import migrations

FORWARD_SQL = r"""
CREATE OR REPLACE FUNCTION erp.project_stock_insert()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path=pg_catalog
AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM erp.companies
        WHERE id=NEW.company_id AND tenant_id=erp.current_tenant_id()
    ) THEN
        RAISE EXCEPTION 'Stock projection tenant context missing/mismatched'
            USING ERRCODE='42501';
    END IF;

    -- A negative movement cannot be the proposed INSERT row of an UPSERT because
    -- PostgreSQL validates CHECK constraints before resolving its unique conflict.
    -- Establish a zero-valued position first, then apply the signed delta by UPDATE.
    INSERT INTO erp.inventory_positions(
        company_id,warehouse_id,item_id,lot_id,on_hand_quantity,value_company
    ) VALUES (NEW.company_id,NEW.warehouse_id,NEW.item_id,NEW.lot_id,0,0)
    ON CONFLICT(company_id,warehouse_id,item_id,lot_id) DO NOTHING;

    UPDATE erp.inventory_positions
       SET on_hand_quantity=on_hand_quantity+NEW.quantity_delta,
           value_company=value_company+NEW.value_delta_company,
           updated_at=clock_timestamp()
     WHERE company_id=NEW.company_id
       AND warehouse_id=NEW.warehouse_id
       AND item_id=NEW.item_id
       AND lot_id IS NOT DISTINCT FROM NEW.lot_id;
    RETURN NEW;
END
$$;
"""


REVERSE_SQL = r"""
CREATE OR REPLACE FUNCTION erp.project_stock_insert()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path=pg_catalog
AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM erp.companies
        WHERE id=NEW.company_id AND tenant_id=erp.current_tenant_id()
    ) THEN
        RAISE EXCEPTION 'Stock projection tenant context missing/mismatched'
            USING ERRCODE='42501';
    END IF;
    INSERT INTO erp.inventory_positions(
        company_id,warehouse_id,item_id,lot_id,on_hand_quantity,value_company
    ) VALUES (
        NEW.company_id,NEW.warehouse_id,NEW.item_id,NEW.lot_id,
        NEW.quantity_delta,NEW.value_delta_company
    )
    ON CONFLICT(company_id,warehouse_id,item_id,lot_id) DO UPDATE
    SET on_hand_quantity=erp.inventory_positions.on_hand_quantity+excluded.on_hand_quantity,
        value_company=erp.inventory_positions.value_company+excluded.value_company,
        updated_at=clock_timestamp();
    RETURN NEW;
END
$$;
"""


class Migration(migrations.Migration):
    dependencies = [("database", "0011_phase5_sales_posting_and_credits")]

    operations = [migrations.RunSQL(FORWARD_SQL, REVERSE_SQL)]
