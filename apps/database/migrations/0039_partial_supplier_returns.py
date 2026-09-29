from django.db import migrations

SQL = r"""
DROP INDEX erp.uq_purchase_bill_line_credited_once;
CREATE INDEX ix_purchase_bill_line_credit_source
 ON erp.purchase_bill_lines(company_id,credit_of_bill_line_id)
 WHERE credit_of_bill_line_id IS NOT NULL;
ALTER TABLE erp.purchase_bill_lines ADD COLUMN credit_quantity_offset numeric(20,6)
 NOT NULL DEFAULT 0 CHECK(credit_quantity_offset>=0);

CREATE FUNCTION erp.guard_partial_supplier_credit_line() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,erp AS $$
DECLARE source record;reserved numeric;v_kind text;minor integer;v_status text;
BEGIN
 IF TG_OP='DELETE' THEN RETURN OLD; END IF;
 IF NEW.credit_of_bill_line_id IS NULL THEN
 IF NEW.credit_quantity_offset<>0 THEN
 RAISE EXCEPTION 'Only supplier credits have a quantity offset'; END IF;
 RETURN NEW;
 END IF;
 SELECT * INTO source FROM erp.purchase_bill_lines WHERE company_id=NEW.company_id
 AND id=NEW.credit_of_bill_line_id FOR UPDATE;
 SELECT document_kind INTO v_kind FROM erp.purchase_bills WHERE company_id=NEW.company_id
 AND id=NEW.purchase_bill_id;
 SELECT b.status,c.minor_units INTO v_status,minor FROM erp.purchase_bills b
 JOIN erp.currencies c ON c.code=b.currency_code WHERE b.company_id=NEW.company_id
 AND b.id=source.purchase_bill_id;
 IF v_kind<>'supplier_credit' OR source.id IS NULL OR v_status<>'posted' THEN
 RAISE EXCEPTION 'Partial credit requires an original supplier bill line'; END IF;
 IF TG_OP='UPDATE' THEN
 IF (NEW.id,NEW.company_id,NEW.purchase_bill_id,NEW.credit_of_bill_line_id,
 NEW.quantity,NEW.credit_quantity_offset,NEW.unit_cost,NEW.net_amount,
 NEW.tax_amount,NEW.gross_amount,NEW.tax_code_id,NEW.item_id,
 NEW.line_account_id,NEW.inventory_account_id_snapshot) IS DISTINCT FROM
 (OLD.id,OLD.company_id,OLD.purchase_bill_id,OLD.credit_of_bill_line_id,
 OLD.quantity,OLD.credit_quantity_offset,OLD.unit_cost,OLD.net_amount,
 OLD.tax_amount,OLD.gross_amount,OLD.tax_code_id,OLD.item_id,
 OLD.line_account_id,OLD.inventory_account_id_snapshot) THEN
 RAISE EXCEPTION 'Supplier credit source and amounts immutable'; END IF;
 RETURN NEW;
 END IF;
 SELECT coalesce(sum(l.quantity),0) INTO reserved
 FROM erp.purchase_bill_lines l JOIN erp.purchase_bills b
 ON b.company_id=l.company_id AND b.id=l.purchase_bill_id
 WHERE l.company_id=NEW.company_id AND
 l.credit_of_bill_line_id=NEW.credit_of_bill_line_id AND b.status<>'void';
 IF NEW.credit_quantity_offset<>reserved OR
 NEW.credit_quantity_offset+NEW.quantity>source.quantity OR
 EXISTS(SELECT 1 FROM erp.purchase_bill_lines l JOIN erp.purchase_bills b
 ON b.company_id=l.company_id AND b.id=l.purchase_bill_id
 WHERE l.company_id=NEW.company_id AND
 l.credit_of_bill_line_id=NEW.credit_of_bill_line_id AND b.status='draft') THEN
 RAISE EXCEPTION 'Supplier credit quantity exceeds available posted source'; END IF;
 IF NEW.item_id IS DISTINCT FROM source.item_id OR
 NEW.line_account_id IS DISTINCT FROM source.line_account_id OR
 NEW.tax_code_id IS DISTINCT FROM source.tax_code_id OR
 NEW.unit_cost IS DISTINCT FROM source.unit_cost OR
 NEW.inventory_account_id_snapshot IS DISTINCT FROM source.inventory_account_id_snapshot OR
 NEW.net_amount IS DISTINCT FROM (round(source.net_amount*
 (NEW.credit_quantity_offset+NEW.quantity)/source.quantity,minor)-
 round(source.net_amount*NEW.credit_quantity_offset/source.quantity,minor)) OR
 NEW.tax_amount IS DISTINCT FROM (round(source.tax_amount*
 (NEW.credit_quantity_offset+NEW.quantity)/source.quantity,minor)-
 round(source.tax_amount*NEW.credit_quantity_offset/source.quantity,minor)) OR
 NEW.gross_amount IS DISTINCT FROM NEW.net_amount+NEW.tax_amount THEN
 RAISE EXCEPTION 'Supplier credit economic snapshot mismatch'; END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER trg_partial_supplier_credit_line BEFORE INSERT OR UPDATE OR DELETE
 ON erp.purchase_bill_lines FOR EACH ROW
 EXECUTE FUNCTION erp.guard_partial_supplier_credit_line();

CREATE OR REPLACE FUNCTION erp.check_supplier_return_complete() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,erp AS $$
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
 AND source_type='purchase_bill' AND source_line_id=l.credit_of_bill_line_id
 AND quantity_delta>0;
 IF n<>1 THEN RAISE EXCEPTION 'Supplier return requires original receipt provenance'; END IF;
 SELECT * INTO o FROM erp.stock_movements WHERE company_id=b.company_id
 AND source_type='purchase_bill' AND source_line_id=l.credit_of_bill_line_id
 AND quantity_delta>0;
 SELECT round((l.net_amount+coalesce(sum(t.tax_amount-t.recoverable_amount),0))
 *b.exchange_rate,c.minor_units) INTO v
 FROM erp.currencies c LEFT JOIN erp.purchase_bill_tax_components t
 ON t.company_id=l.company_id AND t.purchase_bill_line_id=l.id WHERE c.code=
 (SELECT functional_currency FROM erp.companies WHERE id=b.company_id)
 GROUP BY c.minor_units;
 IF m.quantity_delta IS DISTINCT FROM -l.quantity OR
 m.value_delta_company IS DISTINCT FROM -v OR
 m.item_id IS DISTINCT FROM l.item_id OR m.lot_id IS NOT NULL OR
 m.warehouse_id IS DISTINCT FROM o.warehouse_id OR
 o.item_id IS DISTINCT FROM l.item_id OR
 o.quantity_delta<l.credit_quantity_offset+l.quantity OR
 l.credit_quantity_offset<0 OR
 v IS DISTINCT FROM
 (SELECT round(o.value_delta_company*(l.credit_quantity_offset+l.quantity)
 /o.quantity_delta,c.minor_units)-round(o.value_delta_company
 *l.credit_quantity_offset/o.quantity_delta,c.minor_units)
 FROM erp.currencies c WHERE c.code=
 (SELECT functional_currency FROM erp.companies WHERE id=b.company_id)) OR
 m.journal_entry_id IS DISTINCT FROM b.journal_entry_id OR
 NOT EXISTS(SELECT 1 FROM erp.inventory_cost_basis_snapshots s
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
 FROM erp.purchase_bill_lines pl JOIN erp.items i
 ON i.company_id=pl.company_id AND i.id=pl.item_id
 JOIN erp.stock_movements ledger ON ledger.company_id=pl.company_id
 AND ledger.source_line_id=pl.id AND ledger.source_type='purchase_bill'
 AND ledger.source_id=b.id WHERE pl.company_id=b.company_id
 AND pl.purchase_bill_id=b.id GROUP BY pl.inventory_account_id_snapshot
 ) costs WHERE costs.amount IS DISTINCT FROM
 (SELECT coalesce(sum(j.credit_amount-j.debit_amount),0)
 FROM erp.journal_lines j WHERE j.company_id=b.company_id
 AND j.journal_entry_id=b.journal_entry_id AND j.account_id=costs.account_id)) THEN
 RAISE EXCEPTION 'Supplier return inventory journal must match physical valuation'; END IF;
 RETURN NULL;
END $$;
"""


def reverse_sql() -> str:
    from importlib import import_module

    previous = str(import_module("apps.database.migrations.0029_physical_supplier_returns").SQL)
    start = previous.index("CREATE FUNCTION erp.check_supplier_return_complete()")
    end = previous.index("CREATE CONSTRAINT TRIGGER trg_supplier_return_bill_complete", start)
    restore = previous[start:end].replace("CREATE FUNCTION", "CREATE OR REPLACE FUNCTION", 1)
    return (
        "DO $$ BEGIN IF EXISTS(SELECT 1 FROM erp.purchase_bill_lines l "
        "JOIN erp.purchase_bill_lines source ON source.company_id=l.company_id "
        "AND source.id=l.credit_of_bill_line_id WHERE l.credit_quantity_offset>0 "
        "OR l.quantity<>source.quantity) OR EXISTS(SELECT 1 FROM erp.purchase_bill_lines "
        "WHERE credit_of_bill_line_id IS NOT NULL "
        "GROUP BY company_id,credit_of_bill_line_id HAVING count(*)>1) THEN "
        "RAISE EXCEPTION 'Cannot reverse retained partial supplier credit history'; "
        "END IF; END $$; "
        "DROP TRIGGER trg_partial_supplier_credit_line ON erp.purchase_bill_lines; "
        "DROP FUNCTION erp.guard_partial_supplier_credit_line(); "
        "DROP INDEX erp.ix_purchase_bill_line_credit_source; "
        "CREATE UNIQUE INDEX uq_purchase_bill_line_credited_once "
        "ON erp.purchase_bill_lines(company_id,credit_of_bill_line_id) "
        "WHERE credit_of_bill_line_id IS NOT NULL; "
        "ALTER TABLE erp.purchase_bill_lines DROP COLUMN credit_quantity_offset; " + restore
    )


class Migration(migrations.Migration):
    dependencies = [("database", "0038_supplier_claims")]
    operations = [migrations.RunSQL(SQL, reverse_sql())]
