from django.db import migrations

FORWARD_SQL = r"""
ALTER TABLE erp.sales_invoices
    ADD COLUMN calculated_at timestamptz;

ALTER TABLE erp.sales_invoice_tax_components
    ADD COLUMN rate_version_code_snapshot text,
    ADD COLUMN calculation_method_snapshot text,
    ADD COLUMN calculation_base_snapshot text,
    ADD COLUMN fixed_amount_snapshot numeric(20, 6),
    ADD COLUMN recovery_percent_snapshot numeric(9, 6),
    ADD COLUMN rounding_method_snapshot text,
    ADD COLUMN rounding_precision_snapshot smallint,
    ADD COLUMN currency_code_snapshot char(3);

UPDATE erp.sales_invoice_tax_components c
SET rate_version_code_snapshot = v.version_code,
    calculation_method_snapshot = v.calculation_method,
    calculation_base_snapshot = v.calculation_base,
    fixed_amount_snapshot = v.fixed_amount,
    recovery_percent_snapshot = v.recovery_percent,
    rounding_method_snapshot = 'half_up',
    rounding_precision_snapshot = currency.minor_units,
    currency_code_snapshot = i.currency_code
FROM erp.tax_rate_versions v,
     erp.sales_invoices i,
     erp.currencies currency
WHERE v.jurisdiction_id = c.tax_jurisdiction_id
  AND v.rate_schedule_id = c.rate_schedule_id
  AND v.id = c.tax_rate_version_id
  AND i.company_id = c.company_id
  AND i.id = c.sales_invoice_id
  AND currency.code = i.currency_code;

ALTER TABLE erp.sales_invoice_tax_components
    ALTER COLUMN rate_version_code_snapshot SET NOT NULL,
    ALTER COLUMN calculation_method_snapshot SET NOT NULL,
    ALTER COLUMN calculation_base_snapshot SET NOT NULL,
    ALTER COLUMN recovery_percent_snapshot SET NOT NULL,
    ALTER COLUMN rounding_method_snapshot SET NOT NULL,
    ALTER COLUMN rounding_precision_snapshot SET NOT NULL,
    ALTER COLUMN currency_code_snapshot SET NOT NULL,
    ADD CONSTRAINT ck_sales_tax_rounding_precision
        CHECK (rounding_precision_snapshot BETWEEN 0 AND 6),
    ADD CONSTRAINT ck_sales_tax_recovery_snapshot
        CHECK (recovery_percent_snapshot BETWEEN 0 AND 100),
    ADD CONSTRAINT fk_sales_tax_currency_snapshot
        FOREIGN KEY (currency_code_snapshot) REFERENCES erp.currencies(code);

CREATE INDEX ix_sales_invoices_draft_list
    ON erp.sales_invoices(company_id, created_at DESC, id DESC)
    WHERE status = 'draft';

CREATE FUNCTION erp.enforce_sales_invoice_line_limit()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, erp
AS $$
DECLARE
    v_status text;
    v_count bigint;
BEGIN
    SELECT status INTO v_status
    FROM erp.sales_invoices
    WHERE company_id = NEW.company_id AND id = NEW.sales_invoice_id
    FOR UPDATE;

    IF v_status IS NULL THEN
        RAISE EXCEPTION 'Parent sales invoice not found';
    END IF;
    IF TG_OP = 'INSERT' THEN
        SELECT count(*) INTO v_count
        FROM erp.sales_invoice_lines
        WHERE company_id = NEW.company_id
          AND sales_invoice_id = NEW.sales_invoice_id;
        IF v_count >= 50 THEN
            RAISE EXCEPTION 'A sales invoice cannot contain more than 50 lines';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_sales_invoice_line_limit
    BEFORE INSERT OR UPDATE ON erp.sales_invoice_lines
    FOR EACH ROW EXECUTE FUNCTION erp.enforce_sales_invoice_line_limit();

CREATE FUNCTION erp.audit_sales_invoice_draft_change()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, erp, identity
AS $$
DECLARE
    v_tenant_id uuid;
    v_action text;
BEGIN
    SELECT tenant_id INTO STRICT v_tenant_id
    FROM erp.companies
    WHERE id = NEW.company_id;

    v_action := nullif(current_setting('app.sales_invoice_action', true), '');
    IF v_action IS NULL THEN
        v_action := CASE TG_OP
            WHEN 'INSERT' THEN 'sales_invoice.created'
            ELSE 'sales_invoice.updated'
        END;
    END IF;

    INSERT INTO erp.company_audit_events(
        tenant_id, company_id, actor_user_id, actor_service_id, action,
        object_type, object_id, old_data, new_data, request_id
    ) VALUES (
        v_tenant_id,
        NEW.company_id,
        identity.current_user_id(),
        identity.current_service_id(),
        v_action,
        'sales_invoice',
        NEW.id::text,
        CASE WHEN TG_OP = 'INSERT' THEN NULL ELSE to_jsonb(OLD) END,
        to_jsonb(NEW),
        nullif(current_setting('app.request_id', true), '')
    );
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_sales_invoice_draft_audit
    AFTER INSERT OR UPDATE ON erp.sales_invoices
    FOR EACH ROW EXECUTE FUNCTION erp.audit_sales_invoice_draft_change();
"""


REVERSE_SQL = r"""
DROP TRIGGER IF EXISTS trg_sales_invoice_draft_audit ON erp.sales_invoices;
DROP FUNCTION IF EXISTS erp.audit_sales_invoice_draft_change();
DROP TRIGGER IF EXISTS trg_sales_invoice_line_limit ON erp.sales_invoice_lines;
DROP FUNCTION IF EXISTS erp.enforce_sales_invoice_line_limit();
DROP INDEX IF EXISTS erp.ix_sales_invoices_draft_list;
ALTER TABLE erp.sales_invoice_tax_components
    DROP CONSTRAINT IF EXISTS fk_sales_tax_currency_snapshot,
    DROP CONSTRAINT IF EXISTS ck_sales_tax_recovery_snapshot,
    DROP CONSTRAINT IF EXISTS ck_sales_tax_rounding_precision,
    DROP COLUMN IF EXISTS currency_code_snapshot,
    DROP COLUMN IF EXISTS rounding_precision_snapshot,
    DROP COLUMN IF EXISTS rounding_method_snapshot,
    DROP COLUMN IF EXISTS recovery_percent_snapshot,
    DROP COLUMN IF EXISTS fixed_amount_snapshot,
    DROP COLUMN IF EXISTS calculation_base_snapshot,
    DROP COLUMN IF EXISTS calculation_method_snapshot,
    DROP COLUMN IF EXISTS rate_version_code_snapshot;
ALTER TABLE erp.sales_invoices DROP COLUMN IF EXISTS calculated_at;
"""


class Migration(migrations.Migration):
    dependencies = [("database", "0009_phase5_item_catalog")]
    operations = [migrations.RunSQL(FORWARD_SQL, REVERSE_SQL)]
