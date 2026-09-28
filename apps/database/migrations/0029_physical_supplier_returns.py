from django.db import migrations

SQL = r"""
CREATE FUNCTION erp.check_supplier_return_complete() RETURNS trigger LANGUAGE plpgsql
SET search_path=pg_catalog,erp AS $$
DECLARE b record;l record;m record;o record;v numeric;n integer;
BEGIN
 IF TG_TABLE_NAME='purchase_bills' THEN
 SELECT * INTO b FROM erp.purchase_bills WHERE company_id=NEW.company_id AND id=NEW.id;
 ELSE
 SELECT * INTO b FROM erp.purchase_bills WHERE company_id=NEW.company_id AND id=NEW.source_id;
 IF NOT FOUND OR b.document_kind<>'supplier_credit' OR b.status<>'posted' THEN
 RAISE EXCEPTION 'Physical supplier return requires its posted supplier credit'; END IF;
 END IF;
 IF NOT FOUND OR b.document_kind<>'supplier_credit' OR b.status<>'posted' THEN RETURN NULL; END IF;
 FOR l IN SELECT pl.*,i.track_lots,i.track_serials FROM erp.purchase_bill_lines pl
 JOIN erp.items i ON i.company_id=pl.company_id AND i.id=pl.item_id
 WHERE pl.company_id=b.company_id AND pl.purchase_bill_id=b.id
 AND (i.item_kind='stock' OR EXISTS(SELECT 1 FROM erp.stock_movements receipt
 WHERE receipt.company_id=pl.company_id AND receipt.source_type='purchase_bill'
 AND receipt.source_line_id=pl.credit_of_bill_line_id AND receipt.quantity_delta>0))
 LOOP
 SELECT count(*) INTO n FROM erp.stock_movements WHERE company_id=b.company_id
 AND source_type='purchase_bill' AND source_id=b.id AND source_line_id=l.id;
 IF n<>1 OR l.track_lots OR l.track_serials THEN
 RAISE EXCEPTION 'Supplier credit requires one untracked physical return per stock line'; END IF;
 SELECT * INTO m FROM erp.stock_movements WHERE company_id=b.company_id
 AND source_type='purchase_bill' AND source_id=b.id AND source_line_id=l.id;
 SELECT count(*) INTO n FROM erp.stock_movements WHERE company_id=b.company_id
 AND source_type='purchase_bill' AND source_line_id=l.credit_of_bill_line_id AND quantity_delta>0;
 IF n<>1 THEN RAISE EXCEPTION 'Supplier return requires original receipt provenance'; END IF;
 SELECT * INTO o FROM erp.stock_movements WHERE company_id=b.company_id
 AND source_type='purchase_bill' AND source_line_id=l.credit_of_bill_line_id AND quantity_delta>0;
 SELECT round((l.net_amount+coalesce(sum(t.tax_amount-t.recoverable_amount),0))*b.exchange_rate,
 c.minor_units) INTO v FROM erp.currencies c LEFT JOIN erp.purchase_bill_tax_components t
 ON t.company_id=l.company_id AND t.purchase_bill_line_id=l.id WHERE c.code=
 (SELECT functional_currency FROM erp.companies WHERE id=b.company_id) GROUP BY c.minor_units;
 IF m.quantity_delta IS DISTINCT FROM -l.quantity OR m.value_delta_company IS DISTINCT FROM -v
 OR m.item_id IS DISTINCT FROM l.item_id OR m.lot_id IS NOT NULL
 OR m.warehouse_id IS DISTINCT FROM o.warehouse_id OR o.item_id IS DISTINCT FROM l.item_id
 OR o.quantity_delta IS DISTINCT FROM l.quantity OR o.value_delta_company IS DISTINCT FROM v
 OR m.journal_entry_id IS DISTINCT FROM b.journal_entry_id
 OR NOT EXISTS(SELECT 1 FROM erp.inventory_cost_basis_snapshots s
 WHERE s.company_id=m.company_id AND s.movement_id=m.id) THEN
 RAISE EXCEPTION 'Supplier return stock, origin and costing must reconcile'; END IF;
 END LOOP;
 IF EXISTS(SELECT 1 FROM erp.stock_movements sm LEFT JOIN erp.purchase_bill_lines pl
 ON pl.company_id=sm.company_id AND pl.id=sm.source_line_id AND pl.purchase_bill_id=b.id
 WHERE sm.company_id=b.company_id AND sm.source_type='purchase_bill' AND sm.source_id=b.id
 AND (sm.quantity_delta>=0 OR pl.id IS NULL OR pl.inventory_account_id_snapshot IS NULL)) THEN
 RAISE EXCEPTION 'Physical supplier credit contains an unrelated stock movement'; END IF;
 IF EXISTS(SELECT 1 FROM (
 SELECT pl.inventory_account_id_snapshot account_id,-sum(ledger.value_delta_company) amount
 FROM erp.purchase_bill_lines pl JOIN erp.items i ON i.company_id=pl.company_id AND i.id=pl.item_id
 JOIN erp.stock_movements ledger ON ledger.company_id=pl.company_id AND ledger.source_line_id=pl.id
 AND ledger.source_type='purchase_bill' AND ledger.source_id=b.id
 WHERE pl.company_id=b.company_id AND pl.purchase_bill_id=b.id
 GROUP BY pl.inventory_account_id_snapshot) costs WHERE costs.amount IS DISTINCT FROM
 (SELECT coalesce(sum(j.credit_amount-j.debit_amount),0) FROM erp.journal_lines j
 WHERE j.company_id=b.company_id AND j.journal_entry_id=b.journal_entry_id
 AND j.account_id=costs.account_id)) THEN
 RAISE EXCEPTION 'Supplier return inventory journal must match physical valuation'; END IF;
 RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER trg_supplier_return_bill_complete AFTER INSERT OR UPDATE
 ON erp.purchase_bills DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
 EXECUTE FUNCTION erp.check_supplier_return_complete();
CREATE CONSTRAINT TRIGGER trg_supplier_return_movement_complete AFTER INSERT
 ON erp.stock_movements DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
 WHEN(NEW.source_type='purchase_bill' AND NEW.quantity_delta<0)
 EXECUTE FUNCTION erp.check_supplier_return_complete();
"""

REVERSE = r"""
DO $$ BEGIN
 IF EXISTS(SELECT 1 FROM erp.stock_movements m JOIN erp.purchase_bills b
 ON b.company_id=m.company_id AND b.id=m.source_id WHERE m.source_type='purchase_bill'
 AND m.quantity_delta<0 AND b.document_kind='supplier_credit') THEN
 RAISE EXCEPTION 'Cannot reverse retained physical supplier return history'; END IF;
END $$;
DROP TRIGGER trg_supplier_return_movement_complete ON erp.stock_movements;
DROP TRIGGER trg_supplier_return_bill_complete ON erp.purchase_bills;
DROP FUNCTION erp.check_supplier_return_complete();
"""


class Migration(migrations.Migration):
    dependencies = [("database", "0028_inline_return_costing")]
    operations = [migrations.RunSQL(SQL, REVERSE)]
