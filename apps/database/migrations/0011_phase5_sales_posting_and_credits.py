from django.db import migrations

FORWARD_SQL = r"""
ALTER TABLE erp.sales_invoice_lines
    ADD CONSTRAINT uq_sales_invoice_line_company_id UNIQUE (company_id, id);

ALTER TABLE erp.sales_invoice_lines
    ADD COLUMN credit_of_invoice_line_id uuid,
    ADD COLUMN revenue_account_id_snapshot uuid,
    ADD COLUMN inventory_account_id_snapshot uuid,
    ADD COLUMN cogs_account_id_snapshot uuid,
    ADD CONSTRAINT fk_sales_invoice_line_credit_source
        FOREIGN KEY (company_id, credit_of_invoice_line_id)
        REFERENCES erp.sales_invoice_lines(company_id, id),
    ADD CONSTRAINT fk_sales_line_revenue_account_snapshot
        FOREIGN KEY (company_id, revenue_account_id_snapshot)
        REFERENCES erp.accounts(company_id, id),
    ADD CONSTRAINT fk_sales_line_inventory_account_snapshot
        FOREIGN KEY (company_id, inventory_account_id_snapshot)
        REFERENCES erp.accounts(company_id, id),
    ADD CONSTRAINT fk_sales_line_cogs_account_snapshot
        FOREIGN KEY (company_id, cogs_account_id_snapshot)
        REFERENCES erp.accounts(company_id, id);

CREATE UNIQUE INDEX uq_sales_invoice_line_credited_once
    ON erp.sales_invoice_lines(company_id, credit_of_invoice_line_id)
    WHERE credit_of_invoice_line_id IS NOT NULL;

ALTER TABLE erp.sales_invoice_tax_components
    ADD COLUMN output_tax_account_id_snapshot uuid,
    ADD COLUMN account_role_snapshot text,
    ADD COLUMN polarity_snapshot text,
    ADD CONSTRAINT fk_sales_tax_output_account_snapshot
        FOREIGN KEY (company_id, output_tax_account_id_snapshot)
        REFERENCES erp.accounts(company_id, id),
    ADD CONSTRAINT ck_sales_tax_account_role_snapshot
        CHECK (account_role_snapshot IS NULL OR account_role_snapshot = 'output_tax'),
    ADD CONSTRAINT ck_sales_tax_polarity_snapshot
        CHECK (polarity_snapshot IS NULL OR polarity_snapshot IN ('credit', 'debit'));

CREATE UNIQUE INDEX uq_journal_entry_source_document
    ON erp.journal_entries(company_id, source_type, source_id)
    WHERE source_type IS NOT NULL;

CREATE FUNCTION erp.guard_sales_invoice_credit_link()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, erp
AS $$
DECLARE
    v_source_kind text;
    v_source_status text;
    v_source_partner uuid;
    v_source_currency char(3);
BEGIN
    IF NEW.document_kind = 'invoice' THEN
        IF NEW.credit_of_invoice_id IS NOT NULL THEN
            RAISE EXCEPTION 'A normal invoice cannot reference a credit source';
        END IF;
        RETURN NEW;
    END IF;

    SELECT document_kind, status, partner_id, currency_code
      INTO v_source_kind, v_source_status, v_source_partner, v_source_currency
    FROM erp.sales_invoices
    WHERE company_id = NEW.company_id AND id = NEW.credit_of_invoice_id
    FOR SHARE;

    IF v_source_kind IS DISTINCT FROM 'invoice' OR v_source_status IS DISTINCT FROM 'posted' THEN
        RAISE EXCEPTION 'A credit note must reference a posted invoice';
    END IF;
    IF NEW.partner_id IS DISTINCT FROM v_source_partner
       OR NEW.currency_code IS DISTINCT FROM v_source_currency THEN
        RAISE EXCEPTION 'A credit note must preserve source customer and currency';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_sales_invoice_credit_link
    BEFORE INSERT OR UPDATE ON erp.sales_invoices
    FOR EACH ROW EXECUTE FUNCTION erp.guard_sales_invoice_credit_link();

CREATE FUNCTION erp.guard_sales_invoice_credit_line()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, erp
AS $$
DECLARE
    v_kind text;
    v_source_invoice uuid;
    v_actual_source_invoice uuid;
BEGIN
    SELECT document_kind, credit_of_invoice_id
      INTO v_kind, v_source_invoice
    FROM erp.sales_invoices
    WHERE company_id = NEW.company_id AND id = NEW.sales_invoice_id
    FOR UPDATE;

    IF v_kind = 'invoice' AND NEW.credit_of_invoice_line_id IS NOT NULL THEN
        RAISE EXCEPTION 'A normal invoice line cannot reference a credit source line';
    END IF;
    IF v_kind = 'credit_note' THEN
        IF NEW.credit_of_invoice_line_id IS NULL THEN
            RAISE EXCEPTION 'Every credit line must reference an original invoice line';
        END IF;
        SELECT sales_invoice_id INTO v_actual_source_invoice
        FROM erp.sales_invoice_lines
        WHERE company_id = NEW.company_id AND id = NEW.credit_of_invoice_line_id;
        IF v_actual_source_invoice IS DISTINCT FROM v_source_invoice THEN
            RAISE EXCEPTION 'Credit line does not belong to the linked source invoice';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_sales_invoice_credit_line
    BEFORE INSERT OR UPDATE ON erp.sales_invoice_lines
    FOR EACH ROW EXECUTE FUNCTION erp.guard_sales_invoice_credit_line();
"""


REVERSE_SQL = r"""
DROP TRIGGER IF EXISTS trg_sales_invoice_credit_line ON erp.sales_invoice_lines;
DROP FUNCTION IF EXISTS erp.guard_sales_invoice_credit_line();
DROP TRIGGER IF EXISTS trg_sales_invoice_credit_link ON erp.sales_invoices;
DROP FUNCTION IF EXISTS erp.guard_sales_invoice_credit_link();
DROP INDEX IF EXISTS erp.uq_journal_entry_source_document;
ALTER TABLE erp.sales_invoice_tax_components
    DROP CONSTRAINT IF EXISTS ck_sales_tax_polarity_snapshot,
    DROP CONSTRAINT IF EXISTS ck_sales_tax_account_role_snapshot,
    DROP CONSTRAINT IF EXISTS fk_sales_tax_output_account_snapshot,
    DROP COLUMN IF EXISTS polarity_snapshot,
    DROP COLUMN IF EXISTS account_role_snapshot,
    DROP COLUMN IF EXISTS output_tax_account_id_snapshot;
DROP INDEX IF EXISTS erp.uq_sales_invoice_line_credited_once;
ALTER TABLE erp.sales_invoice_lines
    DROP CONSTRAINT IF EXISTS fk_sales_line_cogs_account_snapshot,
    DROP CONSTRAINT IF EXISTS fk_sales_line_inventory_account_snapshot,
    DROP CONSTRAINT IF EXISTS fk_sales_line_revenue_account_snapshot,
    DROP CONSTRAINT IF EXISTS fk_sales_invoice_line_credit_source,
    DROP COLUMN IF EXISTS cogs_account_id_snapshot,
    DROP COLUMN IF EXISTS inventory_account_id_snapshot,
    DROP COLUMN IF EXISTS revenue_account_id_snapshot,
    DROP COLUMN IF EXISTS credit_of_invoice_line_id;
ALTER TABLE erp.sales_invoice_lines
    DROP CONSTRAINT IF EXISTS uq_sales_invoice_line_company_id;
"""


class Migration(migrations.Migration):
    dependencies = [("database", "0010_phase5_sales_invoice_drafts")]
    operations = [migrations.RunSQL(FORWARD_SQL, REVERSE_SQL)]
