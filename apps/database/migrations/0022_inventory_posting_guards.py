from django.db import migrations

SQL = r"""
ALTER TABLE erp.inventory_document_lines ADD COLUMN value_company numeric(20,6)
    CHECK(value_company>=0);
ALTER TABLE erp.inventory_document_lines ADD COLUMN inventory_account_id_snapshot uuid;
ALTER TABLE erp.inventory_document_lines ADD COLUMN offset_account_id_snapshot uuid;
ALTER TABLE erp.inventory_document_lines ADD CONSTRAINT fk_stock_inventory_account
    FOREIGN KEY(company_id,inventory_account_id_snapshot) REFERENCES erp.accounts(company_id,id);
ALTER TABLE erp.inventory_document_lines ADD CONSTRAINT fk_stock_offset_account
    FOREIGN KEY(company_id,offset_account_id_snapshot) REFERENCES erp.accounts(company_id,id);
CREATE FUNCTION erp.check_typed_stock_posting() RETURNS trigger LANGUAGE plpgsql
SET search_path=pg_catalog,erp AS $$
DECLARE l record;i record;a record;v_shipped numeric;v_value numeric;v_actual numeric;
BEGIN
    IF NEW.status<>'posted' THEN RETURN NULL; END IF;
    FOR l IN SELECT * FROM erp.inventory_document_lines WHERE company_id=NEW.company_id
        AND inventory_document_id=NEW.id ORDER BY id LOOP
        IF l.value_company IS NULL OR l.inventory_account_id_snapshot IS NULL THEN
            RAISE EXCEPTION 'Posted stock requires accounting/cost snapshots'; END IF;
        IF EXISTS(SELECT 1 FROM erp.stock_movements m WHERE m.company_id=l.company_id
            AND m.inventory_document_line_id=l.id AND (
                abs(m.value_delta_company)<>l.value_company OR
                m.journal_entry_id IS DISTINCT FROM NEW.journal_entry_id)) THEN
            RAISE EXCEPTION 'Stock value/journal does not reconcile'; END IF;
        IF NEW.document_kind='shipment' THEN
            SELECT s.*,d.status,d.document_kind,d.stock_fulfillment,d.issue_date
                INTO i FROM erp.sales_invoice_lines s JOIN erp.sales_invoices d
                ON d.company_id=s.company_id AND d.id=s.sales_invoice_id
                WHERE s.company_id=l.company_id AND s.id=l.sales_invoice_line_id;
            PERFORM 1 FROM erp.sales_invoices WHERE company_id=l.company_id
                AND id=i.sales_invoice_id FOR UPDATE;
            IF i.status IS DISTINCT FROM 'posted' OR i.document_kind<>'invoice'
                OR i.stock_fulfillment<>'deferred' OR i.item_id<>l.item_id
                OR NEW.document_date<i.issue_date THEN
                RAISE EXCEPTION 'Invalid shipment source'; END IF;
            SELECT coalesce(sum(-quantity_delta),0) INTO v_shipped FROM erp.stock_movements
                WHERE company_id=l.company_id AND source_type='sales_invoice'
                AND source_line_id=i.id;
            IF v_shipped>i.quantity THEN RAISE EXCEPTION 'Invoice over-fulfilled'; END IF;
            IF EXISTS(SELECT 1 FROM erp.stock_movements WHERE company_id=l.company_id
                AND inventory_document_line_id=l.id AND (source_type<>'sales_invoice'
                OR source_id<>i.sales_invoice_id OR source_line_id<>i.id)) THEN
                RAISE EXCEPTION 'Shipment movement source mismatch'; END IF;
        ELSIF l.sales_invoice_line_id IS NOT NULL THEN
            RAISE EXCEPTION 'Only shipments link invoice lines';
        END IF;
    END LOOP;
    SELECT coalesce(sum(value_company),0) INTO v_value FROM erp.inventory_document_lines
        WHERE company_id=NEW.company_id AND inventory_document_id=NEW.id;
    IF NEW.document_kind='transfer' THEN
        IF NEW.journal_entry_id IS NOT NULL THEN
            RAISE EXCEPTION 'Transfer cannot create GL'; END IF;
    ELSIF v_value>0 THEN
        IF NOT EXISTS(SELECT 1 FROM erp.journal_entries WHERE company_id=NEW.company_id
            AND id=NEW.journal_entry_id AND status='posted' AND source_type='inventory_document'
            AND source_id=NEW.id) OR v_value<>(SELECT coalesce(sum(debit_amount),0)
                FROM erp.journal_lines WHERE company_id=NEW.company_id
                AND journal_entry_id=NEW.journal_entry_id) THEN
            RAISE EXCEPTION 'Inventory ledger and GL value differ'; END IF;
        FOR a IN WITH expected AS (
            SELECT inventory_account_id_snapshot account_id,
                CASE WHEN NEW.document_kind IN ('receipt','adjustment_in') THEN value_company
                    ELSE -value_company END amount FROM erp.inventory_document_lines
                WHERE company_id=NEW.company_id AND inventory_document_id=NEW.id
            UNION ALL SELECT offset_account_id_snapshot,
                CASE WHEN NEW.document_kind IN ('receipt','adjustment_in') THEN -value_company
                    ELSE value_company END FROM erp.inventory_document_lines
                WHERE company_id=NEW.company_id AND inventory_document_id=NEW.id)
            SELECT account_id,sum(amount) amount FROM expected GROUP BY account_id LOOP
            SELECT coalesce(sum(debit_amount-credit_amount),0) INTO v_actual FROM erp.journal_lines
                WHERE company_id=NEW.company_id AND journal_entry_id=NEW.journal_entry_id
                AND account_id=a.account_id;
            IF v_actual<>a.amount THEN RAISE EXCEPTION 'Stock GL account mapping mismatch'; END IF;
        END LOOP;
    END IF;
    RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER trg_typed_stock_posted AFTER INSERT OR UPDATE ON erp.inventory_documents
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION erp.check_typed_stock_posting();
CREATE FUNCTION erp.guard_inventory_identity() RETURNS trigger LANGUAGE plpgsql
SET search_path=pg_catalog,erp AS $$
BEGIN
    IF TG_OP='DELETE' THEN RAISE EXCEPTION 'Void stock drafts; do not delete history'; END IF;
    IF (NEW.id,NEW.company_id,NEW.document_kind,NEW.reversal_of_document_id) IS DISTINCT FROM
        (OLD.id,OLD.company_id,OLD.document_kind,OLD.reversal_of_document_id)
        OR OLD.status='void' THEN RAISE EXCEPTION 'Stock identity/final state is immutable'; END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER trg_stock_identity BEFORE UPDATE OR DELETE ON erp.inventory_documents
    FOR EACH ROW EXECUTE FUNCTION erp.guard_inventory_identity();
CREATE FUNCTION erp.guard_inventory_lot_history() RETURNS trigger LANGUAGE plpgsql
SET search_path=pg_catalog,erp AS $$
BEGIN
    RAISE EXCEPTION 'Inventory lot identities are immutable';
END $$;
CREATE TRIGGER trg_lot_history BEFORE UPDATE OR DELETE ON erp.inventory_lots
    FOR EACH ROW EXECUTE FUNCTION erp.guard_inventory_lot_history();
CREATE FUNCTION erp.check_serial_capacity() RETURNS trigger LANGUAGE plpgsql
SET search_path=pg_catalog,erp AS $$
DECLARE v_quantity numeric;
BEGIN
    IF EXISTS(SELECT 1 FROM erp.items WHERE company_id=NEW.company_id AND id=NEW.item_id
        AND track_serials) THEN
        PERFORM 1 FROM erp.inventory_lots WHERE company_id=NEW.company_id AND id=NEW.lot_id
            FOR UPDATE;
        SELECT sum(quantity_delta) INTO v_quantity FROM erp.stock_movements
            WHERE company_id=NEW.company_id AND item_id=NEW.item_id AND lot_id=NEW.lot_id;
        IF v_quantity NOT IN (0,1) THEN RAISE EXCEPTION 'Serial unit duplicated'; END IF;
    END IF;
    RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER trg_serial_capacity AFTER INSERT ON erp.stock_movements
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION erp.check_serial_capacity();
CREATE FUNCTION erp.check_reservation_consumed() RETURNS trigger LANGUAGE plpgsql
SET search_path=pg_catalog,erp AS $$
BEGIN
    IF NEW.status='consumed' AND NOT EXISTS(SELECT 1 FROM erp.inventory_document_lines l
        JOIN erp.inventory_documents d ON d.company_id=l.company_id AND d.id=l.inventory_document_id
        WHERE l.company_id=NEW.company_id AND l.id=NEW.source_id AND d.status='posted') THEN
        RAISE EXCEPTION 'Consumed reservation requires posted stock document'; END IF;
    RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER trg_reservation_consumed AFTER UPDATE ON erp.inventory_reservations
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION erp.check_reservation_consumed();
CREATE FUNCTION erp.guard_reserved_line_history() RETURNS trigger LANGUAGE plpgsql
SET search_path=pg_catalog,erp AS $$
BEGIN
    IF EXISTS(SELECT 1 FROM erp.inventory_reservations WHERE company_id=OLD.company_id
        AND source_type='inventory_document_line' AND source_id=OLD.id) THEN
        IF TG_OP='DELETE' THEN RAISE EXCEPTION 'Reserved line history cannot be deleted'; END IF;
        IF (NEW.id,NEW.company_id,NEW.item_id,NEW.lot_id,NEW.from_warehouse_id,
            NEW.to_warehouse_id,NEW.quantity,NEW.sales_invoice_line_id) IS DISTINCT FROM
            (OLD.id,OLD.company_id,OLD.item_id,OLD.lot_id,OLD.from_warehouse_id,
            OLD.to_warehouse_id,OLD.quantity,OLD.sales_invoice_line_id) THEN
            RAISE EXCEPTION 'Reserved line scope/quantity cannot change'; END IF;
    END IF;
    IF TG_OP='DELETE' THEN RETURN OLD; END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER trg_reserved_line_history BEFORE UPDATE OR DELETE ON erp.inventory_document_lines
    FOR EACH ROW EXECUTE FUNCTION erp.guard_reserved_line_history();
"""

REVERSE = r"""
DROP TRIGGER trg_reserved_line_history ON erp.inventory_document_lines;
DROP FUNCTION erp.guard_reserved_line_history();
DROP TRIGGER trg_reservation_consumed ON erp.inventory_reservations;
DROP FUNCTION erp.check_reservation_consumed();
DROP TRIGGER trg_serial_capacity ON erp.stock_movements;
DROP FUNCTION erp.check_serial_capacity();
DROP TRIGGER trg_lot_history ON erp.inventory_lots;
DROP FUNCTION erp.guard_inventory_lot_history();
DROP TRIGGER trg_stock_identity ON erp.inventory_documents;
DROP FUNCTION erp.guard_inventory_identity();
DROP TRIGGER trg_typed_stock_posted ON erp.inventory_documents;
DROP FUNCTION erp.check_typed_stock_posting();
ALTER TABLE erp.inventory_document_lines DROP COLUMN value_company,
    DROP COLUMN inventory_account_id_snapshot,DROP COLUMN offset_account_id_snapshot;
"""


class Migration(migrations.Migration):
    dependencies = [("database", "0021_inventory_rebuild")]
    operations = [migrations.RunSQL(SQL, REVERSE)]
