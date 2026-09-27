from django.db import migrations

SQL = r"""
ALTER TABLE erp.sales_invoice_tax_components ADD CONSTRAINT uq_sales_tax_company_id
    UNIQUE(company_id,id);
CREATE TABLE erp.sales_returns(
    id uuid PRIMARY KEY DEFAULT uuidv7(), company_id uuid NOT NULL REFERENCES erp.companies(id),
    return_no text NOT NULL CHECK(length(btrim(return_no))>0),
    sales_invoice_id uuid NOT NULL, kind text NOT NULL CHECK(kind IN ('return','cashback')),
    return_date date NOT NULL, reason text NOT NULL CHECK(length(btrim(reason))>0),
    status text NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','inspected','posted','void')),
    row_version bigint NOT NULL DEFAULT 1, credit_total numeric(20,6) NOT NULL DEFAULT 0,
    journal_entry_id uuid, posted_at timestamptz, created_at timestamptz NOT NULL DEFAULT
        clock_timestamp(),
    UNIQUE(company_id,id), UNIQUE(company_id,return_no),
    FOREIGN KEY(company_id,sales_invoice_id) REFERENCES erp.sales_invoices(company_id,id),
    FOREIGN KEY(company_id,journal_entry_id) REFERENCES erp.journal_entries(company_id,id),
    CHECK(status<>'posted' OR (journal_entry_id IS NOT NULL AND posted_at IS NOT NULL)),
    CHECK(row_version>0 AND credit_total>=0)
);
CREATE TABLE erp.sales_return_lines(
    id uuid PRIMARY KEY DEFAULT uuidv7(), company_id uuid NOT NULL, sales_return_id uuid NOT NULL,
    sales_invoice_line_id uuid NOT NULL, quantity numeric(20,6) NOT NULL DEFAULT 0
        CHECK(quantity>=0),
    requested_credit numeric(20,6) CHECK(requested_credit>0),
    condition text CHECK(condition IN ('resellable','damaged','unusable')),
    disposition text CHECK(disposition IN
        ('restock','quarantine','damaged','supplier_return','write_off')),
    warehouse_id uuid, loss_account_id uuid, inspected_at timestamptz,
    credit_net numeric(20,6) NOT NULL DEFAULT 0 CHECK(credit_net>=0),
    credit_tax numeric(20,6) NOT NULL DEFAULT 0 CHECK(credit_tax>=0),
    credit_gross numeric(20,6) NOT NULL DEFAULT 0 CHECK(credit_gross=credit_net+credit_tax),
    historical_cost numeric(20,6) NOT NULL DEFAULT 0 CHECK(historical_cost>=0),
    UNIQUE(company_id,id), UNIQUE(company_id,sales_return_id,sales_invoice_line_id),
    FOREIGN KEY(company_id,sales_return_id) REFERENCES erp.sales_returns(company_id,id),
    FOREIGN KEY(company_id,sales_invoice_line_id) REFERENCES erp.sales_invoice_lines(company_id,id),
    FOREIGN KEY(company_id,warehouse_id) REFERENCES erp.warehouses(company_id,id),
    FOREIGN KEY(company_id,loss_account_id) REFERENCES erp.accounts(company_id,id)
);
CREATE TABLE erp.sales_return_tax_components(
    id uuid PRIMARY KEY DEFAULT uuidv7(), company_id uuid NOT NULL, sales_return_line_id uuid NOT
        NULL,
    source_component_id uuid NOT NULL, tax_amount numeric(20,6) NOT NULL CHECK(tax_amount>=0),
    taxable_base_amount numeric(20,6) NOT NULL CHECK(taxable_base_amount>=0), snapshot jsonb NOT
        NULL,
    UNIQUE(company_id,sales_return_line_id,source_component_id),
    FOREIGN KEY(company_id,sales_return_line_id) REFERENCES erp.sales_return_lines(company_id,id),
    FOREIGN KEY(company_id,source_component_id) REFERENCES
        erp.sales_invoice_tax_components(company_id,id)
);
CREATE TABLE erp.sales_return_credit_applications(
    id uuid PRIMARY KEY DEFAULT uuidv7(),company_id uuid NOT NULL,sales_return_id uuid NOT NULL,
    sales_invoice_id uuid NOT NULL,amount numeric(20,6) NOT NULL CHECK(amount>0),
    effective_date date NOT NULL,posted_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE(company_id,id),
    FOREIGN KEY(company_id,sales_return_id) REFERENCES erp.sales_returns(company_id,id),
    FOREIGN KEY(company_id,sales_invoice_id) REFERENCES erp.sales_invoices(company_id,id)
);
CREATE TABLE erp.customer_refunds(
    id uuid PRIMARY KEY DEFAULT uuidv7(),company_id uuid NOT NULL,sales_return_id uuid NOT NULL,
    receipt_payment_id uuid NOT NULL,cash_account_id uuid NOT NULL,amount numeric(20,6) NOT NULL
        CHECK(amount>0),
    refund_date date NOT NULL,refund_method text NOT NULL,external_reference text,
    journal_entry_id uuid NOT NULL,posted_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE(company_id,id),
    FOREIGN KEY(company_id,sales_return_id) REFERENCES erp.sales_returns(company_id,id),
    FOREIGN KEY(company_id,receipt_payment_id) REFERENCES erp.payments(company_id,id),
    FOREIGN KEY(company_id,cash_account_id) REFERENCES erp.accounts(company_id,id),
    FOREIGN KEY(company_id,journal_entry_id) REFERENCES erp.journal_entries(company_id,id)
);
CREATE TABLE erp.sales_return_replacements(
    id uuid PRIMARY KEY DEFAULT uuidv7(),company_id uuid NOT NULL,sales_return_id uuid NOT NULL,
    replacement_invoice_id uuid NOT NULL,posted_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE(company_id,sales_return_id), UNIQUE(company_id,replacement_invoice_id),
    FOREIGN KEY(company_id,sales_return_id) REFERENCES erp.sales_returns(company_id,id),
    FOREIGN KEY(company_id,replacement_invoice_id) REFERENCES erp.sales_invoices(company_id,id)
);

CREATE INDEX ix_sales_returns_source ON erp.sales_returns(company_id,sales_invoice_id);
CREATE INDEX ix_return_lines_original ON erp.sales_return_lines(company_id,sales_invoice_line_id);
CREATE INDEX ix_return_credit_target ON
    erp.sales_return_credit_applications(company_id,sales_invoice_id);
CREATE INDEX ix_refund_receipt ON erp.customer_refunds(company_id,receipt_payment_id);
CREATE INDEX ix_refund_return ON erp.customer_refunds(company_id,sales_return_id);

CREATE FUNCTION erp.guard_sales_return() RETURNS trigger LANGUAGE plpgsql
SET search_path=pg_catalog,erp AS $$
BEGIN
    IF TG_OP='DELETE' OR (TG_OP='UPDATE' AND OLD.status IN ('posted','void')) THEN
        RAISE EXCEPTION 'Final return documents are immutable';
    END IF;
    IF TG_OP='UPDATE' THEN
        IF (OLD.company_id,OLD.id,OLD.sales_invoice_id,OLD.kind) IS DISTINCT FROM
           (NEW.company_id,NEW.id,NEW.sales_invoice_id,NEW.kind) THEN
            RAISE EXCEPTION 'Return source identity is immutable';
        END IF;
        NEW.row_version:=OLD.row_version+1;
    END IF;
    PERFORM 1 FROM erp.sales_invoices WHERE company_id=NEW.company_id
        AND id=NEW.sales_invoice_id AND status='posted' AND document_kind='invoice' FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'Return source must be a posted original invoice'; END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER trg_sales_return_guard BEFORE INSERT OR UPDATE OR DELETE ON erp.sales_returns
    FOR EACH ROW EXECUTE FUNCTION erp.guard_sales_return();

CREATE FUNCTION erp.guard_return_child() RETURNS trigger LANGUAGE plpgsql
SET search_path=pg_catalog,erp AS $$
DECLARE v_return uuid;v_company uuid;v_status text;
BEGIN
    v_company:=CASE WHEN TG_OP='DELETE' THEN OLD.company_id ELSE NEW.company_id END;
    IF TG_TABLE_NAME='sales_return_lines' THEN
        v_return:=CASE WHEN TG_OP='DELETE' THEN OLD.sales_return_id ELSE NEW.sales_return_id END;
        IF TG_OP='UPDATE' AND (NEW.company_id,NEW.id,NEW.sales_return_id,NEW.sales_invoice_line_id)
            IS DISTINCT FROM (OLD.company_id,OLD.id,OLD.sales_return_id,OLD.sales_invoice_line_id)
                THEN
            RAISE EXCEPTION 'Return line source is immutable'; END IF;
    ELSE
        IF TG_OP='UPDATE' AND
            (NEW.company_id,NEW.id,NEW.sales_return_line_id,NEW.source_component_id)
            IS DISTINCT FROM
            (OLD.company_id,OLD.id,OLD.sales_return_line_id,OLD.source_component_id) THEN
            RAISE EXCEPTION 'Return tax source identity is immutable';
        END IF;
        SELECT sales_return_id INTO v_return FROM erp.sales_return_lines WHERE company_id=v_company
            AND id=CASE WHEN TG_OP='DELETE' THEN OLD.sales_return_line_id ELSE
                NEW.sales_return_line_id END;
    END IF;
    SELECT status INTO v_status FROM erp.sales_returns WHERE company_id=v_company AND id=v_return
        FOR UPDATE;
    IF v_status IN ('posted','void') OR v_status IS NULL THEN RAISE EXCEPTION
        'Final return children are immutable'; END IF;
    IF TG_OP='DELETE' THEN RETURN OLD; END IF;
    IF TG_TABLE_NAME='sales_return_lines' THEN
    IF NOT EXISTS(
        SELECT 1 FROM erp.sales_invoices i JOIN erp.sales_invoice_lines l
        ON l.company_id=i.company_id AND l.sales_invoice_id=i.id JOIN erp.sales_returns r
        ON r.company_id=i.company_id AND r.sales_invoice_id=i.id
        WHERE r.id=NEW.sales_return_id AND r.company_id=NEW.company_id AND
            l.id=NEW.sales_invoice_line_id
    ) THEN RAISE EXCEPTION 'Return line must belong to the original invoice'; END IF;
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER trg_return_line_guard BEFORE INSERT OR UPDATE OR DELETE ON erp.sales_return_lines
    FOR EACH ROW EXECUTE FUNCTION erp.guard_return_child();
CREATE TRIGGER trg_return_tax_guard BEFORE INSERT OR UPDATE OR DELETE ON
    erp.sales_return_tax_components
    FOR EACH ROW EXECUTE FUNCTION erp.guard_return_child();

CREATE FUNCTION erp.audit_sales_return() RETURNS trigger LANGUAGE plpgsql
SECURITY DEFINER SET search_path=pg_catalog,erp,identity AS $$
DECLARE v_tenant uuid;
BEGIN
    SELECT tenant_id INTO STRICT v_tenant FROM erp.companies WHERE id=NEW.company_id;
    INSERT INTO erp.company_audit_events(tenant_id,company_id,actor_user_id,actor_service_id,
        action,object_type,object_id,old_data,new_data,request_id)
    VALUES(v_tenant,NEW.company_id,identity.current_user_id(),identity.current_service_id(),
        TG_TABLE_NAME||CASE WHEN TG_OP='INSERT' THEN '.created' ELSE '.updated' END,
        TG_TABLE_NAME,NEW.id::text,CASE WHEN TG_OP='INSERT' THEN NULL ELSE to_jsonb(OLD) END,
        to_jsonb(NEW),nullif(current_setting('app.request_id',true),''));
    RETURN NEW;
END $$;

CREATE FUNCTION erp.check_return_capacity() RETURNS trigger LANGUAGE plpgsql
SET search_path=pg_catalog,erp AS $$
DECLARE v_line record;v_return record;v_original record;v_quantity numeric;v_credit numeric;
BEGIN
    IF NEW.status<>'posted' THEN RETURN NULL; END IF;
    SELECT * INTO v_original FROM erp.sales_invoices WHERE company_id=NEW.company_id
        AND id=NEW.sales_invoice_id FOR UPDATE;
    IF NEW.credit_total<>(SELECT coalesce(sum(credit_gross),0) FROM erp.sales_return_lines
        WHERE company_id=NEW.company_id AND sales_return_id=NEW.id) OR
       NOT EXISTS(SELECT 1 FROM erp.sales_return_lines WHERE company_id=NEW.company_id AND
           sales_return_id=NEW.id)
    THEN RAISE EXCEPTION 'Return total mismatch'; END IF;
    IF NOT EXISTS(SELECT 1 FROM erp.journal_entries WHERE company_id=NEW.company_id AND
        id=NEW.journal_entry_id
        AND status='posted' AND source_type='sales_return' AND source_id=NEW.id) THEN
        RAISE EXCEPTION 'Return must reference its posted source journal'; END IF;
    FOR v_line IN SELECT l.*,o.quantity sold_quantity,o.gross_amount original_gross FROM
        erp.sales_return_lines l
        JOIN erp.sales_invoice_lines o ON o.company_id=l.company_id AND o.id=l.sales_invoice_line_id
        WHERE l.company_id=NEW.company_id AND l.sales_return_id=NEW.id LOOP
        SELECT coalesce(sum(l.quantity),0),coalesce(sum(l.credit_gross),0) INTO v_quantity,v_credit
        FROM erp.sales_return_lines l JOIN erp.sales_returns r ON r.company_id=l.company_id AND
            r.id=l.sales_return_id
        WHERE l.company_id=NEW.company_id AND l.sales_invoice_line_id=v_line.sales_invoice_line_id
            AND r.status='posted';
        v_credit:=v_credit+coalesce((SELECT sum(l.gross_amount) FROM erp.sales_invoice_lines l
            JOIN erp.sales_invoices i ON i.company_id=l.company_id AND i.id=l.sales_invoice_id
            WHERE l.company_id=NEW.company_id AND
                l.credit_of_invoice_line_id=v_line.sales_invoice_line_id
            AND i.status='posted'),0);
        IF v_quantity>v_line.sold_quantity OR v_credit>v_line.original_gross THEN
            RAISE EXCEPTION 'Return quantity or credit exceeds original sale'; END IF;
        IF NEW.kind='return' AND (v_line.quantity<=0 OR v_line.inspected_at IS NULL OR
            v_line.warehouse_id IS NULL) THEN
            RAISE EXCEPTION 'Physical returns require completed inspection'; END IF;
        IF NEW.kind='cashback' AND (v_line.quantity<>0 OR v_line.warehouse_id IS NOT NULL) THEN
            RAISE EXCEPTION 'Cash back cannot create physical inventory'; END IF;
        IF v_line.credit_tax<>(SELECT coalesce(sum(tax_amount),0)
            FROM erp.sales_return_tax_components WHERE company_id=NEW.company_id
            AND sales_return_line_id=v_line.id) THEN RAISE EXCEPTION
                'Return taxes do not reconcile'; END IF;
        IF EXISTS(SELECT 1 FROM erp.sales_return_tax_components t
            JOIN erp.sales_invoice_tax_components original ON original.company_id=t.company_id
            AND original.id=t.source_component_id WHERE t.company_id=NEW.company_id
            AND t.sales_return_line_id=v_line.id AND (
                original.sales_invoice_line_id<>v_line.sales_invoice_line_id
                OR t.snapshot->>'polarity_snapshot'<>'debit'
                OR t.snapshot->>'tax_rate_version_id' IS DISTINCT FROM
                    original.tax_rate_version_id::text
                OR t.snapshot->>'rate_snapshot' IS DISTINCT FROM original.rate_snapshot::text
                OR t.snapshot->>'output_tax_account_id_snapshot' IS DISTINCT FROM
                    original.output_tax_account_id_snapshot::text
                OR (t.snapshot->>'tax_amount')::numeric IS DISTINCT FROM t.tax_amount
                OR (t.snapshot->>'taxable_base_amount')::numeric IS DISTINCT FROM
                    t.taxable_base_amount
            )) THEN RAISE EXCEPTION 'Return tax source snapshot mismatch'; END IF;
    END LOOP;
    RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER trg_return_posted_check AFTER INSERT OR UPDATE ON erp.sales_returns
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION erp.check_return_capacity();

CREATE FUNCTION erp.guard_return_settlement() RETURNS trigger LANGUAGE plpgsql
SET search_path=pg_catalog,erp AS $$
DECLARE r record;i record;p record;v_used numeric;v_capacity numeric;v_cash numeric;
BEGIN
    IF TG_OP<>'INSERT' THEN RAISE EXCEPTION 'Posted return settlements are immutable'; END IF;
    IF TG_TABLE_NAME='customer_refunds' THEN
        SELECT * INTO p FROM erp.payments WHERE company_id=NEW.company_id AND
            id=NEW.receipt_payment_id FOR UPDATE;
    END IF;
    SELECT * INTO r FROM erp.sales_returns WHERE company_id=NEW.company_id AND
        id=NEW.sales_return_id;
    SELECT * INTO i FROM erp.sales_invoices WHERE company_id=NEW.company_id AND
        id=r.sales_invoice_id FOR UPDATE;
    SELECT * INTO r FROM erp.sales_returns WHERE company_id=NEW.company_id AND
        id=NEW.sales_return_id FOR UPDATE;
    IF r.status IS DISTINCT FROM 'posted' THEN RAISE EXCEPTION
        'Settlement requires a posted return'; END IF;
    v_used:=coalesce((SELECT sum(amount) FROM erp.customer_refunds WHERE company_id=NEW.company_id
        AND sales_return_id=r.id),0)
        +coalesce((SELECT sum(amount) FROM erp.sales_return_credit_applications WHERE
            company_id=NEW.company_id AND sales_return_id=r.id),0);
    IF v_used+NEW.amount>r.credit_total THEN RAISE EXCEPTION 'Return credit is over-settled'; END
        IF;
    IF TG_TABLE_NAME='customer_refunds' THEN
        IF p.status IS DISTINCT FROM 'posted' OR p.direction<>'receipt' OR p.reversal_of_payment_id
            IS NOT NULL
            OR p.partner_id<>i.partner_id OR p.currency_code<>i.currency_code OR
                p.exchange_rate<>i.exchange_rate
            OR p.withholding_total<>0 OR NEW.refund_date<r.return_date OR
                NEW.refund_date<p.payment_date
            OR EXISTS(SELECT 1 FROM erp.payments WHERE company_id=p.company_id AND
                reversal_of_payment_id=p.id)
        THEN RAISE EXCEPTION 'Receipt is not eligible for refund'; END IF;
        v_cash:=coalesce((SELECT sum(applied_document_amount) FROM erp.ar_receipt_allocations
            WHERE company_id=NEW.company_id AND payment_id=p.id AND sales_invoice_id=i.id),0);
        v_used:=coalesce((SELECT sum(amount) FROM erp.customer_refunds
            WHERE company_id=NEW.company_id AND receipt_payment_id=p.id),0);
        IF v_used+NEW.amount>p.amount THEN RAISE EXCEPTION 'Receipt cash refund capacity exceeded';
            END IF;
        v_used:=coalesce((SELECT sum(f.amount) FROM erp.customer_refunds f JOIN erp.sales_returns sr
            ON sr.company_id=f.company_id AND sr.id=f.sales_return_id WHERE
                f.company_id=NEW.company_id
            AND f.receipt_payment_id=p.id AND sr.sales_invoice_id=i.id),0);
        IF v_used+NEW.amount>v_cash THEN RAISE EXCEPTION
            'Invoice receipt refund capacity exceeded'; END IF;
        IF NOT EXISTS(SELECT 1 FROM erp.journal_entries WHERE company_id=NEW.company_id AND
            id=NEW.journal_entry_id
            AND status='posted' AND source_type='customer_refund' AND source_id=NEW.id) THEN
            RAISE EXCEPTION 'Refund source journal missing'; END IF;
    ELSE
        SELECT * INTO i FROM erp.sales_invoices WHERE company_id=NEW.company_id AND
            id=NEW.sales_invoice_id FOR UPDATE;
        IF i.status IS DISTINCT FROM 'posted' OR i.document_kind<>'invoice' OR
            NEW.effective_date<i.issue_date
            OR NEW.effective_date<r.return_date OR i.partner_id<>(SELECT partner_id FROM
                erp.sales_invoices
                WHERE company_id=r.company_id AND id=r.sales_invoice_id)
        THEN RAISE EXCEPTION 'Invalid return credit application target'; END IF;
        v_capacity:=coalesce((SELECT sum(gross_amount) FROM erp.sales_invoice_lines
            WHERE company_id=i.company_id AND sales_invoice_id=i.id),0);
        v_used:=coalesce((SELECT sum(a.applied_document_amount) FROM erp.ar_receipt_allocations a
            JOIN erp.payments paid ON paid.company_id=a.company_id AND paid.id=a.payment_id
            WHERE a.company_id=i.company_id AND a.sales_invoice_id=i.id AND paid.status='posted'
            AND NOT EXISTS(SELECT 1 FROM erp.payments rv WHERE rv.company_id=paid.company_id
                AND rv.reversal_of_payment_id=paid.id AND rv.status='posted')),0)
            +coalesce((SELECT sum(amount) FROM erp.sales_return_credit_applications WHERE
                company_id=i.company_id AND sales_invoice_id=i.id),0);
        IF v_used+NEW.amount>v_capacity THEN RAISE EXCEPTION
            'Invoice credit application exceeds balance'; END IF;
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER trg_refund_guard BEFORE INSERT OR UPDATE OR DELETE ON erp.customer_refunds
    FOR EACH ROW EXECUTE FUNCTION erp.guard_return_settlement();
CREATE TRIGGER trg_return_application_guard BEFORE INSERT OR UPDATE OR DELETE ON
    erp.sales_return_credit_applications
    FOR EACH ROW EXECUTE FUNCTION erp.guard_return_settlement();

CREATE FUNCTION erp.guard_return_replacement() RETURNS trigger LANGUAGE plpgsql
SET search_path=pg_catalog,erp AS $$
BEGIN
    IF TG_OP<>'INSERT' THEN RAISE EXCEPTION 'Replacement links are immutable'; END IF;
    IF NOT EXISTS(SELECT 1 FROM erp.sales_returns r JOIN erp.sales_invoices original
        ON original.company_id=r.company_id AND original.id=r.sales_invoice_id
        JOIN erp.sales_invoices replacement ON replacement.company_id=r.company_id
        AND replacement.id=NEW.replacement_invoice_id WHERE r.company_id=NEW.company_id
        AND r.id=NEW.sales_return_id AND r.kind='return' AND r.status='posted'
        AND replacement.status='posted' AND replacement.document_kind='invoice'
        AND replacement.id<>original.id AND replacement.partner_id=original.partner_id
        AND replacement.currency_code=original.currency_code AND
            replacement.exchange_rate=original.exchange_rate)
    THEN RAISE EXCEPTION 'Invalid linked replacement'; END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER trg_return_replacement_guard BEFORE INSERT OR UPDATE OR DELETE ON
    erp.sales_return_replacements
    FOR EACH ROW EXECUTE FUNCTION erp.guard_return_replacement();

CREATE FUNCTION erp.guard_refunded_payment_reversal() RETURNS trigger LANGUAGE plpgsql
SET search_path=pg_catalog,erp AS $$
BEGIN
    IF NEW.reversal_of_payment_id IS NOT NULL AND EXISTS(SELECT 1 FROM erp.customer_refunds
        WHERE company_id=NEW.company_id AND receipt_payment_id=NEW.reversal_of_payment_id) THEN
        RAISE EXCEPTION 'A refunded receipt cannot be reversed'; END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER trg_refunded_receipt_reversal BEFORE INSERT ON erp.payments
    FOR EACH ROW EXECUTE FUNCTION erp.guard_refunded_payment_reversal();

CREATE FUNCTION erp.check_return_adjusted_receipt() RETURNS trigger LANGUAGE plpgsql
SET search_path=pg_catalog,erp AS $$
DECLARE a record;v_total numeric;v_used numeric;
BEGIN
    IF NEW.status<>'posted' OR NEW.direction<>'receipt' OR NEW.reversal_of_payment_id IS NOT NULL
        THEN RETURN NULL; END IF;
    FOR a IN SELECT sales_invoice_id FROM erp.ar_receipt_allocations
        WHERE company_id=NEW.company_id AND payment_id=NEW.id ORDER BY sales_invoice_id LOOP
        PERFORM 1 FROM erp.sales_invoices WHERE company_id=NEW.company_id AND id=a.sales_invoice_id
            FOR UPDATE;
        SELECT coalesce(sum(gross_amount),0) INTO v_total FROM erp.sales_invoice_lines
            WHERE company_id=NEW.company_id AND sales_invoice_id=a.sales_invoice_id;
        SELECT coalesce(sum(x.applied_document_amount),0) INTO v_used FROM
            erp.ar_receipt_allocations x
            JOIN erp.payments p ON p.company_id=x.company_id AND p.id=x.payment_id
            WHERE x.company_id=NEW.company_id AND x.sales_invoice_id=a.sales_invoice_id AND
                p.status='posted'
            AND NOT EXISTS(SELECT 1 FROM erp.payments rv WHERE rv.company_id=p.company_id
                AND rv.reversal_of_payment_id=p.id AND rv.status='posted');
        v_used:=v_used+coalesce((SELECT sum(amount) FROM erp.sales_return_credit_applications
            WHERE company_id=NEW.company_id AND sales_invoice_id=a.sales_invoice_id),0);
        IF v_used>v_total THEN RAISE EXCEPTION 'Receipt exceeds return-adjusted invoice balance';
            END IF;
    END LOOP;
    RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER trg_receipt_return_balance AFTER INSERT OR UPDATE ON erp.payments
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION erp.check_return_adjusted_receipt();

CREATE FUNCTION erp.check_legacy_credit_return_overlap() RETURNS trigger LANGUAGE plpgsql
SET search_path=pg_catalog,erp AS $$
DECLARE l record;v_used numeric;
BEGIN
    IF NEW.status<>'posted' OR NEW.document_kind<>'credit_note' THEN RETURN NULL; END IF;
    PERFORM 1 FROM erp.sales_invoices WHERE company_id=NEW.company_id AND
        id=NEW.credit_of_invoice_id FOR UPDATE;
    FOR l IN SELECT original.id,original.gross_amount FROM erp.sales_invoice_lines credit
        JOIN erp.sales_invoice_lines original ON original.company_id=credit.company_id
        AND original.id=credit.credit_of_invoice_line_id WHERE credit.company_id=NEW.company_id
        AND credit.sales_invoice_id=NEW.id LOOP
        SELECT coalesce(sum(rl.credit_gross),0) INTO v_used FROM erp.sales_return_lines rl
            JOIN erp.sales_returns r ON r.company_id=rl.company_id AND r.id=rl.sales_return_id
            WHERE rl.company_id=NEW.company_id AND rl.sales_invoice_line_id=l.id AND
                r.status='posted';
        v_used:=v_used+coalesce((SELECT sum(cl.gross_amount) FROM erp.sales_invoice_lines cl
            JOIN erp.sales_invoices c ON c.company_id=cl.company_id AND c.id=cl.sales_invoice_id
            WHERE cl.company_id=NEW.company_id AND cl.credit_of_invoice_line_id=l.id AND
                c.status='posted'),0);
        IF v_used>l.gross_amount THEN RAISE EXCEPTION 'Credit overlaps posted return value'; END IF;
    END LOOP;
    RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER trg_legacy_credit_return_overlap AFTER INSERT OR UPDATE ON
    erp.sales_invoices
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION
        erp.check_legacy_credit_return_overlap();

DO $$ DECLARE t text; BEGIN
    FOREACH t IN ARRAY ARRAY['sales_returns','sales_return_lines','sales_return_tax_components',
        'sales_return_credit_applications','customer_refunds','sales_return_replacements'] LOOP
        EXECUTE format('ALTER TABLE erp.%I ENABLE ROW LEVEL SECURITY',t);
        EXECUTE
            format('CREATE POLICY company_scope ON erp.%I '
            'USING(EXISTS(SELECT 1 FROM erp.companies c '
            'WHERE c.id=company_id AND c.tenant_id=erp.current_tenant_id())) WITH CHECK(EXISTS('
            'SELECT 1 FROM erp.companies c WHERE c.id=company_id '
            'AND c.tenant_id=erp.current_tenant_id()))',t);
    END LOOP;
    FOREACH t IN ARRAY
        ARRAY['sales_returns','customer_refunds','sales_return_credit_applications',
            'sales_return_replacements'] LOOP
        EXECUTE
            format('CREATE TRIGGER trg_return_audit AFTER INSERT OR UPDATE ON erp.%I FOR EACH ROW '
            'EXECUTE FUNCTION erp.audit_sales_return()',t);
    END LOOP;
END $$;
INSERT INTO identity.permissions(code,description) VALUES
    ('sales.return.view','View linked returns and sale adjustments'),
    ('sales.return.edit_draft','Manage return drafts and inspections'),
    ('sales.return.post','Post linked sale adjustments'),
    ('payments.refund','Refund eligible customer receipts') ON CONFLICT DO NOTHING;
"""

REVERSE = r"""
DROP TRIGGER trg_legacy_credit_return_overlap ON erp.sales_invoices;
DROP FUNCTION erp.check_legacy_credit_return_overlap();
DROP TRIGGER trg_receipt_return_balance ON erp.payments;
DROP FUNCTION erp.check_return_adjusted_receipt();
DROP TRIGGER trg_refunded_receipt_reversal ON erp.payments;
DROP FUNCTION erp.guard_refunded_payment_reversal();
DROP TABLE erp.sales_return_replacements,erp.customer_refunds,erp.sales_return_credit_applications,
    erp.sales_return_tax_components,erp.sales_return_lines,erp.sales_returns;
DROP FUNCTION
    erp.guard_return_replacement(),erp.guard_return_settlement(),erp.check_return_capacity(),
    erp.audit_sales_return(),erp.guard_return_child(),erp.guard_sales_return();
ALTER TABLE erp.sales_invoice_tax_components DROP CONSTRAINT uq_sales_tax_company_id;
"""


class Migration(migrations.Migration):
    dependencies = [("database", "0016_warehouse_stock_segregation")]
    operations = [migrations.RunSQL(SQL, REVERSE)]
