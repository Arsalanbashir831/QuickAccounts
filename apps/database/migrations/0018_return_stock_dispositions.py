from django.db import migrations

SQL = r"""
CREATE TABLE erp.sales_return_stock_actions(
    id uuid PRIMARY KEY DEFAULT uuidv7(),company_id uuid NOT NULL,
    sales_return_line_id uuid NOT NULL,from_warehouse_id uuid NOT NULL,to_warehouse_id uuid,
    quantity numeric(20,6) NOT NULL CHECK(quantity>0),historical_cost numeric(20,6) NOT NULL
        CHECK(historical_cost>=0),
    action_date date NOT NULL,reason text NOT NULL CHECK(length(btrim(reason))>0),
    loss_account_id uuid,journal_entry_id uuid,posted_at timestamptz NOT NULL DEFAULT
        clock_timestamp(),
    UNIQUE(company_id,id),
    FOREIGN KEY(company_id,sales_return_line_id) REFERENCES erp.sales_return_lines(company_id,id),
    FOREIGN KEY(company_id,from_warehouse_id) REFERENCES erp.warehouses(company_id,id),
    FOREIGN KEY(company_id,to_warehouse_id) REFERENCES erp.warehouses(company_id,id),
    FOREIGN KEY(company_id,loss_account_id) REFERENCES erp.accounts(company_id,id),
    FOREIGN KEY(company_id,journal_entry_id) REFERENCES erp.journal_entries(company_id,id),
    CHECK(to_warehouse_id IS DISTINCT FROM from_warehouse_id),
    CHECK((to_warehouse_id IS NULL)=(loss_account_id IS NOT NULL)),
    CHECK(to_warehouse_id IS NOT NULL OR historical_cost=0 OR journal_entry_id IS NOT NULL)
);
CREATE INDEX ix_return_stock_line ON
    erp.sales_return_stock_actions(company_id,sales_return_line_id);
ALTER TABLE erp.sales_return_stock_actions ENABLE ROW LEVEL SECURITY;
CREATE POLICY company_scope ON erp.sales_return_stock_actions
    USING(EXISTS(SELECT 1 FROM erp.companies c WHERE c.id=company_id AND
        c.tenant_id=erp.current_tenant_id()))
    WITH CHECK(EXISTS(SELECT 1 FROM erp.companies c WHERE c.id=company_id AND
        c.tenant_id=erp.current_tenant_id()));

CREATE FUNCTION erp.guard_return_stock_action() RETURNS trigger LANGUAGE plpgsql
SET search_path=pg_catalog,erp AS $$
DECLARE l record;r record;v_quantity numeric;v_value numeric;v_available numeric;v_date date;
BEGIN
    IF TG_OP<>'INSERT' THEN RAISE EXCEPTION 'Posted return stock actions are immutable'; END IF;
    SELECT * INTO l FROM erp.sales_return_lines WHERE company_id=NEW.company_id
        AND id=NEW.sales_return_line_id FOR UPDATE;
    SELECT * INTO r FROM erp.sales_returns WHERE company_id=NEW.company_id AND id=l.sales_return_id;
    IF r.status IS DISTINCT FROM 'posted' OR r.kind<>'return' OR l.disposition='write_off' THEN
        RAISE EXCEPTION 'Stock disposition requires an unconsumed posted physical return'; END IF;
    PERFORM 1 FROM erp.warehouses WHERE company_id=NEW.company_id AND id=NEW.from_warehouse_id
        AND is_active AND stock_category<>'sellable' FOR SHARE;
    IF NOT FOUND THEN RAISE EXCEPTION 'Source must be an active non-sellable warehouse'; END IF;
    v_quantity:=CASE WHEN l.warehouse_id=NEW.from_warehouse_id THEN l.quantity ELSE 0 END;
    v_value:=CASE WHEN l.warehouse_id=NEW.from_warehouse_id THEN l.historical_cost ELSE 0 END;
    SELECT v_quantity+coalesce(sum(CASE WHEN a.to_warehouse_id=NEW.from_warehouse_id THEN a.quantity
        WHEN a.from_warehouse_id=NEW.from_warehouse_id THEN -a.quantity ELSE 0 END),0),
        v_value+coalesce(sum(CASE WHEN a.to_warehouse_id=NEW.from_warehouse_id THEN
            a.historical_cost
        WHEN a.from_warehouse_id=NEW.from_warehouse_id THEN -a.historical_cost ELSE 0 END),0),
        greatest(r.return_date,coalesce(max(a.action_date),r.return_date))
        INTO v_quantity,v_value,v_date FROM erp.sales_return_stock_actions a
        WHERE a.company_id=NEW.company_id AND a.sales_return_line_id=l.id;
    IF NEW.action_date<v_date OR NEW.quantity>v_quantity OR NEW.historical_cost>v_value
        OR (NEW.quantity=v_quantity AND NEW.historical_cost<>v_value) THEN
        RAISE EXCEPTION 'Return stock quantity, cost or chronology does not reconcile'; END IF;
    SELECT on_hand_quantity-reserved_quantity INTO v_available FROM erp.inventory_positions
        WHERE company_id=NEW.company_id AND warehouse_id=NEW.from_warehouse_id
        AND item_id=(SELECT item_id FROM erp.sales_invoice_lines WHERE company_id=l.company_id
            AND id=l.sales_invoice_line_id) AND lot_id IS NULL FOR UPDATE;
    IF v_available IS NULL OR NEW.quantity>v_available THEN RAISE EXCEPTION
        'Return stock unavailable'; END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER trg_return_stock_action BEFORE INSERT OR UPDATE OR DELETE ON
    erp.sales_return_stock_actions
    FOR EACH ROW EXECUTE FUNCTION erp.guard_return_stock_action();
CREATE TRIGGER trg_return_stock_audit AFTER INSERT ON erp.sales_return_stock_actions
    FOR EACH ROW EXECUTE FUNCTION erp.audit_sales_return();

CREATE FUNCTION erp.check_return_stock_action() RETURNS trigger LANGUAGE plpgsql
SET search_path=pg_catalog,erp AS $$
DECLARE v_quantity numeric;v_value numeric;
BEGIN
    SELECT coalesce(sum(quantity_delta),0),coalesce(sum(value_delta_company),0)
        INTO v_quantity,v_value FROM erp.stock_movements WHERE company_id=NEW.company_id
        AND source_type='return_stock_action' AND source_id=NEW.id AND
            warehouse_id=NEW.from_warehouse_id;
    IF v_quantity<>-NEW.quantity OR v_value<>-NEW.historical_cost THEN
        RAISE EXCEPTION 'Disposition issue movement missing'; END IF;
    IF NEW.to_warehouse_id IS NOT NULL THEN
        SELECT coalesce(sum(quantity_delta),0),coalesce(sum(value_delta_company),0)
            INTO v_quantity,v_value FROM erp.stock_movements WHERE company_id=NEW.company_id
            AND source_type='return_stock_action' AND source_id=NEW.id AND
                warehouse_id=NEW.to_warehouse_id;
        IF v_quantity<>NEW.quantity OR v_value<>NEW.historical_cost THEN
            RAISE EXCEPTION 'Disposition receipt movement missing'; END IF;
    ELSIF NEW.historical_cost>0 AND NOT EXISTS(SELECT 1 FROM erp.journal_entries WHERE
        company_id=NEW.company_id
        AND id=NEW.journal_entry_id AND source_type='return_stock_action' AND source_id=NEW.id AND
            status='posted')
    THEN RAISE EXCEPTION 'Write-off journal missing'; END IF;
    RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER trg_return_stock_posted AFTER INSERT ON erp.sales_return_stock_actions
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION erp.check_return_stock_action();
INSERT INTO identity.permissions(code,description) VALUES
    ('inventory.write_off','Approve inventory write-offs') ON CONFLICT DO NOTHING;
"""

REVERSE = r"""
DROP TABLE erp.sales_return_stock_actions;
DROP FUNCTION erp.check_return_stock_action(),erp.guard_return_stock_action();
"""


class Migration(migrations.Migration):
    dependencies = [("database", "0017_sales_returns_refunds")]
    operations = [migrations.RunSQL(SQL, REVERSE)]
