"""Extend the existing supplier-credit reconciliation to exact serial movements."""

from importlib import import_module

from django.db import migrations

previous = str(import_module("apps.database.migrations.0040_supplier_transfer_provenance").SQL)
start = previous.index("CREATE OR REPLACE FUNCTION erp.check_supplier_return_complete()")
end = previous.index("END $$;", start) + len("END $$;")
original_function = previous[start:end]

old_guard = """ IF n<>1 OR l.track_lots OR l.track_serials THEN
 RAISE EXCEPTION 'Supplier credit requires one untracked physical return per stock line'; END IF;"""
serial_guard = """ IF l.track_serials THEN
 SELECT count(*),coalesce(sum(-sm.value_delta_company),0) INTO n,v
 FROM erp.stock_movements sm WHERE sm.company_id=b.company_id
 AND sm.source_type='purchase_bill' AND sm.source_id=b.id AND sm.source_line_id=l.id;
 SELECT round((l.net_amount+coalesce(sum(t.tax_amount-t.recoverable_amount),0))
 *b.exchange_rate,c.minor_units) INTO v_expected
 FROM erp.currencies c LEFT JOIN erp.purchase_bill_tax_components t
 ON t.company_id=l.company_id AND t.purchase_bill_line_id=l.id WHERE c.code=
 (SELECT functional_currency FROM erp.companies WHERE id=b.company_id)
 GROUP BY c.minor_units;
 IF n<>l.quantity OR v IS DISTINCT FROM v_expected OR EXISTS(
 SELECT 1 FROM erp.stock_movements sm
 LEFT JOIN erp.inventory_serials s ON s.company_id=sm.company_id AND s.lot_id=sm.lot_id
 LEFT JOIN erp.stock_movements origin ON origin.company_id=s.company_id
 AND origin.id=s.first_receipt_movement_id
 WHERE sm.company_id=b.company_id AND sm.source_type='purchase_bill'
 AND sm.source_id=b.id AND sm.source_line_id=l.id
 AND (sm.quantity_delta<>-1 OR sm.item_id<>l.item_id OR sm.lot_id IS NULL
 OR s.id IS NULL OR origin.source_type IS DISTINCT FROM 'purchase_bill'
 OR origin.source_line_id IS DISTINCT FROM l.credit_of_bill_line_id
 OR sm.journal_entry_id IS DISTINCT FROM b.journal_entry_id
 OR NOT EXISTS(SELECT 1 FROM erp.inventory_cost_basis_snapshots cb
 WHERE cb.company_id=sm.company_id AND cb.movement_id=sm.id)))
 THEN RAISE EXCEPTION 'Supplier return serial, origin and valuation must reconcile';
 END IF;
 CONTINUE;
 END IF;
 IF n<>1 OR l.track_lots THEN
 RAISE EXCEPTION 'Supplier credit requires one untracked physical return per stock line'; END IF;"""
if old_guard not in original_function:
    raise RuntimeError("Supplier return guard changed; review serial migration.")
SQL = original_function.replace(old_guard, serial_guard).replace(
    "v numeric;n integer;", "v numeric;v_expected numeric;n integer;"
)
SQL += r"""
DROP INDEX erp.ix_serial_movement_history;
CREATE INDEX ix_serial_movement_cursor ON erp.serial_movements(company_id,serial_id,id);
CREATE FUNCTION erp.check_serial_purchase_receipt() RETURNS trigger LANGUAGE plpgsql
SET search_path=pg_catalog,erp AS $$
DECLARE l record;v_quantity numeric;v_cost numeric;v_expected numeric;
BEGIN
    IF NEW.status<>'posted' OR NEW.document_kind<>'bill' THEN RETURN NULL; END IF;
    FOR l IN SELECT pl.*,i.track_serials FROM erp.purchase_bill_lines pl
      JOIN erp.items i ON i.company_id=pl.company_id AND i.id=pl.item_id
      WHERE pl.company_id=NEW.company_id AND pl.purchase_bill_id=NEW.id
        AND i.item_kind='stock' AND i.track_serials LOOP
        SELECT count(*),coalesce(sum(m.value_delta_company),0)
          INTO v_quantity,v_cost FROM erp.stock_movements m
          WHERE m.company_id=NEW.company_id AND m.source_type='purchase_bill'
            AND m.source_id=NEW.id AND m.source_line_id=l.id;
        SELECT round((l.net_amount+coalesce(sum(t.tax_amount-t.recoverable_amount),0))
          *NEW.exchange_rate,c.minor_units) INTO v_expected
          FROM erp.currencies c LEFT JOIN erp.purchase_bill_tax_components t
            ON t.company_id=l.company_id AND t.purchase_bill_line_id=l.id
          WHERE c.code=(SELECT functional_currency FROM erp.companies
            WHERE id=NEW.company_id) GROUP BY c.minor_units;
        IF v_quantity<>l.quantity OR v_cost IS DISTINCT FROM v_expected OR EXISTS(
          SELECT 1 FROM erp.stock_movements m
          LEFT JOIN erp.inventory_serials s ON s.company_id=m.company_id
            AND s.lot_id=m.lot_id
          WHERE m.company_id=NEW.company_id AND m.source_type='purchase_bill'
            AND m.source_id=NEW.id AND m.source_line_id=l.id
            AND (m.quantity_delta<>1 OR m.movement_kind<>'receipt'
              OR m.item_id<>l.item_id OR m.lot_id IS NULL
              OR s.first_receipt_movement_id IS DISTINCT FROM m.id
              OR m.journal_entry_id IS DISTINCT FROM NEW.journal_entry_id)
        ) THEN
          RAISE EXCEPTION 'Serialized bill receipt quantity, origin or value mismatch';
        END IF;
    END LOOP;
    RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER trg_serial_purchase_receipt AFTER INSERT OR UPDATE
    ON erp.purchase_bills DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
    EXECUTE FUNCTION erp.check_serial_purchase_receipt();

-- The document update already validates every line once. The older movement
-- trigger repeated that full-document scan for every unit (quadratic for
-- serialized receipts). Keep the movement trigger, but scope it to its line.
CREATE OR REPLACE FUNCTION erp.check_inventory_document_event() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,erp AS $$
DECLARE l record;d record;v_quantity numeric;
BEGIN
    IF TG_TABLE_NAME='inventory_documents' THEN
        PERFORM erp.assert_inventory_document(NEW.company_id,NEW.id);
        RETURN NULL;
    END IF;
    IF NEW.inventory_document_line_id IS NULL THEN RETURN NULL; END IF;
    SELECT * INTO l FROM erp.inventory_document_lines
      WHERE company_id=NEW.company_id AND id=NEW.inventory_document_line_id;
    SELECT * INTO d FROM erp.inventory_documents
      WHERE company_id=NEW.company_id AND id=l.inventory_document_id;
    IF d.status IS DISTINCT FROM 'posted'
       OR NEW.item_id IS DISTINCT FROM l.item_id
       OR NEW.lot_id IS DISTINCT FROM l.lot_id
       OR NEW.journal_entry_id IS DISTINCT FROM d.journal_entry_id
       OR (NEW.warehouse_id IS DISTINCT FROM l.from_warehouse_id
           AND NEW.warehouse_id IS DISTINCT FROM l.to_warehouse_id)
       OR (d.document_kind='shipment' AND
           (NEW.source_type<>'sales_invoice'
             OR NEW.source_line_id IS DISTINCT FROM l.sales_invoice_line_id))
       OR (d.document_kind<>'shipment' AND
           (NEW.source_type<>'inventory_document'
             OR NEW.source_id IS DISTINCT FROM d.id
             OR NEW.source_line_id IS DISTINCT FROM l.id)) THEN
        RAISE EXCEPTION 'Inventory movement and posted document disagree';
    END IF;
    IF l.from_warehouse_id IS NOT NULL THEN
        SELECT coalesce(sum(quantity_delta),0) INTO v_quantity
          FROM erp.stock_movements WHERE company_id=l.company_id
          AND inventory_document_line_id=l.id AND warehouse_id=l.from_warehouse_id;
        IF v_quantity<>-l.quantity THEN
            RAISE EXCEPTION 'Inventory source movement quantity disagrees';
        END IF;
    END IF;
    IF l.to_warehouse_id IS NOT NULL THEN
        SELECT coalesce(sum(quantity_delta),0) INTO v_quantity
          FROM erp.stock_movements WHERE company_id=l.company_id
          AND inventory_document_line_id=l.id AND warehouse_id=l.to_warehouse_id;
        IF v_quantity<>l.quantity THEN
            RAISE EXCEPTION 'Inventory destination movement quantity disagrees';
        END IF;
    END IF;
    RETURN NULL;
END $$;
"""

serial_source = str(import_module("apps.database.migrations.0042_serial_identity").SQL)
serial_start = serial_source.index("CREATE FUNCTION erp.sync_serial_movement()")
serial_end = serial_source.index("END $$;", serial_start) + len("END $$;")
serial_function = serial_source[serial_start:serial_end].replace(
    "CREATE FUNCTION erp.sync_serial_movement()",
    "CREATE OR REPLACE FUNCTION erp.sync_serial_movement()",
    1,
)
status_needle = "WHEN NEW.source_type='purchase_bill' THEN 'outside'"
if status_needle not in serial_function:
    raise RuntimeError("Serial stock transition changed; review production migration.")
serial_function = serial_function.replace(
    status_needle,
    status_needle + "\n                WHEN NEW.source_type='production_execution' "
    "THEN 'in_production'",
)
reentry_needle = """        IF v_serial.lifecycle_status='disposed' THEN
            RAISE EXCEPTION 'Disposed serial cannot re-enter inventory';
        END IF;"""
if reentry_needle not in serial_function:
    raise RuntimeError("Serial re-entry guard changed; review production migration.")
serial_function = serial_function.replace(
    reentry_needle,
    reentry_needle + """
        IF v_serial.lifecycle_status='in_production' AND NOT
            (NEW.source_type='production_execution' AND NEW.movement_kind='receipt'
             AND EXISTS(SELECT 1 FROM erp.stock_movements prior
               WHERE prior.company_id=NEW.company_id AND prior.lot_id=NEW.lot_id
                 AND prior.source_type='production_execution'
                 AND prior.quantity_delta=-1)) THEN
            RAISE EXCEPTION 'Production material can return only through execution reversal';
        END IF;""",
)
SQL += r"""
DO $$ DECLARE v_name text;
BEGIN
    SELECT conname INTO v_name FROM pg_constraint
      WHERE conrelid='erp.inventory_serials'::regclass
        AND contype='c' AND conname='inventory_serials_lifecycle_status_check';
    IF v_name IS NULL THEN RAISE EXCEPTION 'Serial status constraint missing'; END IF;
    EXECUTE format('ALTER TABLE erp.inventory_serials DROP CONSTRAINT %I',v_name);
END $$;
ALTER TABLE erp.inventory_serials ADD CONSTRAINT ck_serial_lifecycle_status
    CHECK(lifecycle_status IN
      ('in_stock','with_customer','in_transit','disposed','outside','in_production'));
""" + serial_function


class Migration(migrations.Migration):
    dependencies = [("database", "0042_serial_identity")]
    operations = [migrations.RunSQL(SQL)]
