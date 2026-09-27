from django.db import migrations

FORWARD_SQL = r"""
ALTER TABLE erp.purchase_bills
    ADD COLUMN calculated_at timestamptz;

ALTER TABLE erp.purchase_bill_lines
    ADD CONSTRAINT uq_purchase_bill_line_company_id UNIQUE (company_id, id),
    ADD COLUMN credit_of_bill_line_id uuid,
    ADD COLUMN purchase_account_id_snapshot uuid,
    ADD COLUMN inventory_account_id_snapshot uuid,
    ADD CONSTRAINT fk_purchase_bill_line_credit_source
        FOREIGN KEY (company_id, credit_of_bill_line_id)
        REFERENCES erp.purchase_bill_lines(company_id, id),
    ADD CONSTRAINT fk_purchase_line_account_snapshot
        FOREIGN KEY (company_id, purchase_account_id_snapshot)
        REFERENCES erp.accounts(company_id, id),
    ADD CONSTRAINT fk_purchase_line_inventory_snapshot
        FOREIGN KEY (company_id, inventory_account_id_snapshot)
        REFERENCES erp.accounts(company_id, id);

CREATE UNIQUE INDEX uq_purchase_bill_line_credited_once
    ON erp.purchase_bill_lines(company_id, credit_of_bill_line_id)
    WHERE credit_of_bill_line_id IS NOT NULL;

ALTER TABLE erp.purchase_bill_tax_components
    ADD COLUMN rate_version_code_snapshot text,
    ADD COLUMN calculation_method_snapshot text,
    ADD COLUMN calculation_base_snapshot text,
    ADD COLUMN fixed_amount_snapshot numeric(20, 6),
    ADD COLUMN recovery_percent_snapshot numeric(9, 6),
    ADD COLUMN rounding_method_snapshot text,
    ADD COLUMN rounding_precision_snapshot smallint,
    ADD COLUMN currency_code_snapshot char(3),
    ADD COLUMN input_tax_account_id_snapshot uuid,
    ADD COLUMN account_role_snapshot text,
    ADD COLUMN polarity_snapshot text;

UPDATE erp.purchase_bill_tax_components c
SET rate_version_code_snapshot=v.version_code,
    calculation_method_snapshot=v.calculation_method,
    calculation_base_snapshot=v.calculation_base,
    fixed_amount_snapshot=v.fixed_amount,
    recovery_percent_snapshot=v.recovery_percent,
    rounding_method_snapshot='half_up',
    rounding_precision_snapshot=currency.minor_units,
    currency_code_snapshot=b.currency_code
FROM erp.tax_rate_versions v, erp.purchase_bills b, erp.currencies currency
WHERE v.jurisdiction_id=c.tax_jurisdiction_id
  AND v.rate_schedule_id=c.rate_schedule_id
  AND v.id=c.tax_rate_version_id
  AND b.company_id=c.company_id AND b.id=c.purchase_bill_id
  AND currency.code=b.currency_code;

ALTER TABLE erp.purchase_bill_tax_components
    ALTER COLUMN rate_version_code_snapshot SET NOT NULL,
    ALTER COLUMN calculation_method_snapshot SET NOT NULL,
    ALTER COLUMN calculation_base_snapshot SET NOT NULL,
    ALTER COLUMN recovery_percent_snapshot SET NOT NULL,
    ALTER COLUMN rounding_method_snapshot SET NOT NULL,
    ALTER COLUMN rounding_precision_snapshot SET NOT NULL,
    ALTER COLUMN currency_code_snapshot SET NOT NULL,
    ADD CONSTRAINT ck_purchase_tax_rounding_precision
        CHECK (rounding_precision_snapshot BETWEEN 0 AND 6),
    ADD CONSTRAINT ck_purchase_tax_recovery_snapshot
        CHECK (recovery_percent_snapshot BETWEEN 0 AND 100),
    ADD CONSTRAINT fk_purchase_tax_currency_snapshot
        FOREIGN KEY (currency_code_snapshot) REFERENCES erp.currencies(code),
    ADD CONSTRAINT fk_purchase_tax_input_account_snapshot
        FOREIGN KEY (company_id, input_tax_account_id_snapshot)
        REFERENCES erp.accounts(company_id, id),
    ADD CONSTRAINT ck_purchase_tax_account_role_snapshot
        CHECK (account_role_snapshot IS NULL OR account_role_snapshot='input_tax'),
    ADD CONSTRAINT ck_purchase_tax_polarity_snapshot
        CHECK (polarity_snapshot IS NULL OR polarity_snapshot IN ('debit','credit'));

CREATE INDEX ix_purchase_bills_draft_list
    ON erp.purchase_bills(company_id, created_at DESC, id DESC)
    WHERE status='draft';

CREATE FUNCTION erp.enforce_purchase_bill_line_limit()
RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog,erp AS $$
DECLARE v_status text; v_count bigint;
BEGIN
    SELECT status INTO v_status FROM erp.purchase_bills
    WHERE company_id=NEW.company_id AND id=NEW.purchase_bill_id FOR UPDATE;
    IF v_status IS NULL THEN RAISE EXCEPTION 'Parent purchase bill not found'; END IF;
    IF TG_OP='INSERT' THEN
        SELECT count(*) INTO v_count FROM erp.purchase_bill_lines
        WHERE company_id=NEW.company_id AND purchase_bill_id=NEW.purchase_bill_id;
        IF v_count>=50 THEN RAISE EXCEPTION 'A purchase bill cannot contain more than 50 lines';
        END IF;
    END IF;
    RETURN NEW;
END $$;

CREATE TRIGGER trg_purchase_bill_line_limit
    BEFORE INSERT OR UPDATE ON erp.purchase_bill_lines
    FOR EACH ROW EXECUTE FUNCTION erp.enforce_purchase_bill_line_limit();

CREATE FUNCTION erp.audit_purchase_bill_change()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,erp,identity AS $$
DECLARE v_tenant_id uuid; v_action text;
BEGIN
    SELECT tenant_id INTO STRICT v_tenant_id FROM erp.companies WHERE id=NEW.company_id;
    v_action:=nullif(current_setting('app.purchase_bill_action',true),'');
    IF v_action IS NULL THEN
        v_action:=CASE TG_OP WHEN 'INSERT' THEN 'purchase_bill.created'
                            ELSE 'purchase_bill.updated' END;
    END IF;
    INSERT INTO erp.company_audit_events(
        tenant_id,company_id,actor_user_id,actor_service_id,action,
        object_type,object_id,old_data,new_data,request_id
    ) VALUES (
        v_tenant_id,NEW.company_id,identity.current_user_id(),identity.current_service_id(),
        v_action,'purchase_bill',NEW.id::text,
        CASE WHEN TG_OP='INSERT' THEN NULL ELSE to_jsonb(OLD) END,to_jsonb(NEW),
        nullif(current_setting('app.request_id',true),'')
    );
    RETURN NEW;
END $$;

CREATE TRIGGER trg_purchase_bill_audit
    AFTER INSERT OR UPDATE ON erp.purchase_bills
    FOR EACH ROW EXECUTE FUNCTION erp.audit_purchase_bill_change();

CREATE FUNCTION erp.guard_purchase_bill_credit_link()
RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog,erp AS $$
DECLARE v_kind text; v_status text; v_supplier uuid; v_currency char(3);
BEGIN
    IF NEW.document_kind='bill' THEN
        IF NEW.credit_of_bill_id IS NOT NULL THEN
            RAISE EXCEPTION 'A normal bill cannot reference a credit source';
        END IF;
        RETURN NEW;
    END IF;
    SELECT document_kind,status,supplier_id,currency_code
      INTO v_kind,v_status,v_supplier,v_currency
    FROM erp.purchase_bills
    WHERE company_id=NEW.company_id AND id=NEW.credit_of_bill_id FOR SHARE;
    IF v_kind IS DISTINCT FROM 'bill' OR v_status IS DISTINCT FROM 'posted' THEN
        RAISE EXCEPTION 'A supplier credit must reference a posted bill';
    END IF;
    IF NEW.supplier_id IS DISTINCT FROM v_supplier
       OR NEW.currency_code IS DISTINCT FROM v_currency THEN
        RAISE EXCEPTION 'A supplier credit must preserve source supplier and currency';
    END IF;
    RETURN NEW;
END $$;

CREATE TRIGGER trg_purchase_bill_credit_link
    BEFORE INSERT OR UPDATE ON erp.purchase_bills
    FOR EACH ROW EXECUTE FUNCTION erp.guard_purchase_bill_credit_link();

CREATE FUNCTION erp.guard_purchase_bill_credit_line()
RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog,erp AS $$
DECLARE v_kind text; v_source_bill uuid; v_actual_source_bill uuid;
BEGIN
    SELECT document_kind,credit_of_bill_id INTO v_kind,v_source_bill
    FROM erp.purchase_bills
    WHERE company_id=NEW.company_id AND id=NEW.purchase_bill_id FOR UPDATE;
    IF v_kind='bill' AND NEW.credit_of_bill_line_id IS NOT NULL THEN
        RAISE EXCEPTION 'A normal bill line cannot reference a credit source line';
    END IF;
    IF v_kind='supplier_credit' THEN
        IF NEW.credit_of_bill_line_id IS NULL THEN
            RAISE EXCEPTION 'Every supplier credit line must reference an original bill line';
        END IF;
        SELECT purchase_bill_id INTO v_actual_source_bill FROM erp.purchase_bill_lines
        WHERE company_id=NEW.company_id AND id=NEW.credit_of_bill_line_id;
        IF v_actual_source_bill IS DISTINCT FROM v_source_bill THEN
            RAISE EXCEPTION 'Supplier credit line does not belong to the linked source bill';
        END IF;
    END IF;
    RETURN NEW;
END $$;

CREATE TRIGGER trg_purchase_bill_credit_line
    BEFORE INSERT OR UPDATE ON erp.purchase_bill_lines
    FOR EACH ROW EXECUTE FUNCTION erp.guard_purchase_bill_credit_line();

INSERT INTO identity.permissions(code, description) VALUES
    ('purchasing.bill.view', 'Read purchase bills'),
    ('purchasing.bill.edit_draft', 'Edit purchase bill drafts')
ON CONFLICT (code) DO NOTHING;
"""

REVERSE_SQL = r"""
DELETE FROM identity.role_permissions
 WHERE permission_code IN ('purchasing.bill.view', 'purchasing.bill.edit_draft');
DELETE FROM identity.service_account_permissions
 WHERE permission_code IN ('purchasing.bill.view', 'purchasing.bill.edit_draft');
DELETE FROM identity.permissions
 WHERE code IN ('purchasing.bill.view', 'purchasing.bill.edit_draft');
DROP TRIGGER IF EXISTS trg_purchase_bill_credit_line ON erp.purchase_bill_lines;
DROP FUNCTION IF EXISTS erp.guard_purchase_bill_credit_line();
DROP TRIGGER IF EXISTS trg_purchase_bill_credit_link ON erp.purchase_bills;
DROP FUNCTION IF EXISTS erp.guard_purchase_bill_credit_link();
DROP TRIGGER IF EXISTS trg_purchase_bill_audit ON erp.purchase_bills;
DROP FUNCTION IF EXISTS erp.audit_purchase_bill_change();
DROP TRIGGER IF EXISTS trg_purchase_bill_line_limit ON erp.purchase_bill_lines;
DROP FUNCTION IF EXISTS erp.enforce_purchase_bill_line_limit();
DROP INDEX IF EXISTS erp.ix_purchase_bills_draft_list;
ALTER TABLE erp.purchase_bill_tax_components
    DROP CONSTRAINT IF EXISTS ck_purchase_tax_polarity_snapshot,
    DROP CONSTRAINT IF EXISTS ck_purchase_tax_account_role_snapshot,
    DROP CONSTRAINT IF EXISTS fk_purchase_tax_input_account_snapshot,
    DROP CONSTRAINT IF EXISTS fk_purchase_tax_currency_snapshot,
    DROP CONSTRAINT IF EXISTS ck_purchase_tax_recovery_snapshot,
    DROP CONSTRAINT IF EXISTS ck_purchase_tax_rounding_precision,
    DROP COLUMN IF EXISTS polarity_snapshot,
    DROP COLUMN IF EXISTS account_role_snapshot,
    DROP COLUMN IF EXISTS input_tax_account_id_snapshot,
    DROP COLUMN IF EXISTS currency_code_snapshot,
    DROP COLUMN IF EXISTS rounding_precision_snapshot,
    DROP COLUMN IF EXISTS rounding_method_snapshot,
    DROP COLUMN IF EXISTS recovery_percent_snapshot,
    DROP COLUMN IF EXISTS fixed_amount_snapshot,
    DROP COLUMN IF EXISTS calculation_base_snapshot,
    DROP COLUMN IF EXISTS calculation_method_snapshot,
    DROP COLUMN IF EXISTS rate_version_code_snapshot;
DROP INDEX IF EXISTS erp.uq_purchase_bill_line_credited_once;
ALTER TABLE erp.purchase_bill_lines
    DROP CONSTRAINT IF EXISTS fk_purchase_line_inventory_snapshot,
    DROP CONSTRAINT IF EXISTS fk_purchase_line_account_snapshot,
    DROP CONSTRAINT IF EXISTS fk_purchase_bill_line_credit_source,
    DROP COLUMN IF EXISTS inventory_account_id_snapshot,
    DROP COLUMN IF EXISTS purchase_account_id_snapshot,
    DROP COLUMN IF EXISTS credit_of_bill_line_id,
    DROP CONSTRAINT IF EXISTS uq_purchase_bill_line_company_id;
ALTER TABLE erp.purchase_bills DROP COLUMN IF EXISTS calculated_at;
"""


class Migration(migrations.Migration):
    dependencies = [("database", "0012_fix_signed_stock_projection")]
    operations = [migrations.RunSQL(FORWARD_SQL, REVERSE_SQL)]
