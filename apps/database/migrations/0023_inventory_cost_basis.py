from django.db import migrations

SQL = r"""
CREATE TABLE erp.inventory_cost_policies(
    id uuid PRIMARY KEY DEFAULT uuidv7(),company_id uuid NOT NULL REFERENCES erp.companies(id),
    version integer NOT NULL DEFAULT 1 CHECK(version=1),
    method text NOT NULL DEFAULT 'scope_average' CHECK(method='scope_average'),
    ledger_precision integer NOT NULL DEFAULT 6 CHECK(ledger_precision=6),
    rounding_policy text NOT NULL DEFAULT 'reject_fractional_gl'
        CHECK(rounding_policy='reject_fractional_gl'),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE(company_id,id),UNIQUE(company_id,version)
);
CREATE TABLE erp.inventory_cost_basis_snapshots(
    id uuid PRIMARY KEY DEFAULT uuidv7(),company_id uuid NOT NULL,
    movement_id uuid NOT NULL,policy_id uuid NOT NULL,
    currency_code char(3) NOT NULL REFERENCES erp.currencies(code),
    currency_precision integer NOT NULL CHECK(currency_precision BETWEEN 0 AND 6),
    basis_quantity numeric(20,6) NOT NULL CHECK(basis_quantity>0),
    basis_value_company numeric(20,6) NOT NULL CHECK(basis_value_company>=0),
    reserved_quantity numeric(20,6) NOT NULL CHECK(reserved_quantity>=0),
    issue_quantity numeric(20,6) NOT NULL CHECK(issue_quantity>0),
    issue_value_company numeric(20,6) NOT NULL CHECK(issue_value_company>=0),
    unit_cost_company numeric(20,6) NOT NULL CHECK(unit_cost_company>=0),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK(issue_quantity<=basis_quantity-reserved_quantity),
    UNIQUE(company_id,id),UNIQUE(company_id,movement_id),
    FOREIGN KEY(company_id,movement_id) REFERENCES erp.stock_movements(company_id,id),
    FOREIGN KEY(company_id,policy_id) REFERENCES erp.inventory_cost_policies(company_id,id)
);
ALTER TABLE erp.inventory_cost_policies ENABLE ROW LEVEL SECURITY;
ALTER TABLE erp.inventory_cost_basis_snapshots ENABLE ROW LEVEL SECURITY;
CREATE POLICY company_scope ON erp.inventory_cost_policies
    USING(EXISTS(SELECT 1 FROM erp.companies c WHERE c.id=company_id
        AND c.tenant_id=erp.current_tenant_id()))
    WITH CHECK(EXISTS(SELECT 1 FROM erp.companies c WHERE c.id=company_id
        AND c.tenant_id=erp.current_tenant_id()));
CREATE POLICY company_scope ON erp.inventory_cost_basis_snapshots
    USING(EXISTS(SELECT 1 FROM erp.companies c WHERE c.id=company_id
        AND c.tenant_id=erp.current_tenant_id()))
    WITH CHECK(EXISTS(SELECT 1 FROM erp.companies c WHERE c.id=company_id
        AND c.tenant_id=erp.current_tenant_id()));
CREATE FUNCTION erp.guard_cost_basis_history() RETURNS trigger LANGUAGE plpgsql
SET search_path=pg_catalog,erp AS $$
DECLARE m record;v_value numeric;
BEGIN
    IF TG_OP<>'INSERT' THEN RAISE EXCEPTION 'Costing history is immutable'; END IF;
    IF TG_TABLE_NAME='inventory_cost_policies' THEN RETURN NEW; END IF;
    IF NEW.currency_code IS DISTINCT FROM (SELECT functional_currency FROM erp.companies
        WHERE id=NEW.company_id) OR NEW.currency_precision IS DISTINCT FROM
        (SELECT minor_units FROM erp.currencies WHERE code=NEW.currency_code) THEN
        RAISE EXCEPTION 'Cost snapshot functional currency mismatch'; END IF;
    SELECT * INTO m FROM erp.stock_movements WHERE company_id=NEW.company_id AND id=NEW.movement_id;
    v_value:=CASE WHEN NEW.issue_quantity=NEW.basis_quantity THEN NEW.basis_value_company
        ELSE round(NEW.basis_value_company*NEW.issue_quantity/NEW.basis_quantity,6) END;
    IF m.quantity_delta IS DISTINCT FROM -NEW.issue_quantity
        OR m.value_delta_company IS DISTINCT FROM -NEW.issue_value_company
        OR m.unit_cost_company IS DISTINCT FROM NEW.unit_cost_company
        OR NEW.issue_value_company<>v_value
        OR NEW.unit_cost_company<>round(v_value/NEW.issue_quantity,6)
        OR round(v_value,NEW.currency_precision)<>v_value THEN
        RAISE EXCEPTION 'Cost basis and issue movement do not reconcile'; END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER trg_cost_policy_history BEFORE INSERT OR UPDATE OR DELETE
    ON erp.inventory_cost_policies FOR EACH ROW EXECUTE FUNCTION erp.guard_cost_basis_history();
CREATE TRIGGER trg_cost_basis_history BEFORE INSERT OR UPDATE OR DELETE
    ON erp.inventory_cost_basis_snapshots
    FOR EACH ROW EXECUTE FUNCTION erp.guard_cost_basis_history();
CREATE TRIGGER trg_cost_policy_audit AFTER INSERT ON erp.inventory_cost_policies
    FOR EACH ROW EXECUTE FUNCTION erp.audit_inventory_command();
CREATE TRIGGER trg_cost_basis_audit AFTER INSERT ON erp.inventory_cost_basis_snapshots
    FOR EACH ROW EXECUTE FUNCTION erp.audit_inventory_command();
CREATE FUNCTION erp.check_stock_cost_basis_complete() RETURNS trigger LANGUAGE plpgsql
SET search_path=pg_catalog,erp AS $$
BEGIN
    IF NEW.status='posted' AND EXISTS(SELECT 1 FROM erp.inventory_document_lines l
        JOIN erp.stock_movements m ON m.company_id=l.company_id
            AND m.inventory_document_line_id=l.id
        WHERE l.company_id=NEW.company_id AND l.inventory_document_id=NEW.id AND m.quantity_delta<0
        AND NOT EXISTS(SELECT 1 FROM erp.inventory_cost_basis_snapshots b
            WHERE b.company_id=m.company_id AND b.movement_id=m.id)) THEN
        RAISE EXCEPTION 'Posted outgoing stock requires a cost-basis snapshot'; END IF;
    RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER trg_stock_cost_basis_complete AFTER INSERT OR UPDATE
    ON erp.inventory_documents DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION erp.check_stock_cost_basis_complete();
"""

REVERSE = r"""
DROP TRIGGER trg_stock_cost_basis_complete ON erp.inventory_documents;
DROP FUNCTION erp.check_stock_cost_basis_complete();
DROP TABLE erp.inventory_cost_basis_snapshots,erp.inventory_cost_policies;
DROP FUNCTION erp.guard_cost_basis_history();
"""


class Migration(migrations.Migration):
    dependencies = [("database", "0022_inventory_posting_guards")]
    operations = [migrations.RunSQL(SQL, REVERSE)]
