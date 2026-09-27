from django.db import migrations

SQL = r"""
ALTER TABLE erp.payments
    ADD COLUMN reversal_of_payment_id uuid,
    ADD COLUMN reversal_reason text,
    ADD CONSTRAINT fk_payment_reversal FOREIGN KEY(company_id,reversal_of_payment_id)
        REFERENCES erp.payments(company_id,id),
    ADD CONSTRAINT ck_payment_reversal_reason
        CHECK ((reversal_of_payment_id IS NULL)=(reversal_reason IS NULL)),
    ADD CONSTRAINT ck_payment_not_self_reversal CHECK(reversal_of_payment_id IS DISTINCT FROM id);
CREATE UNIQUE INDEX uq_payment_reversed_once ON erp.payments(company_id,reversal_of_payment_id)
    WHERE reversal_of_payment_id IS NOT NULL;
ALTER TABLE erp.payment_tax_components
    ADD COLUMN rate_version_code_snapshot text,
    ADD COLUMN calculation_method_snapshot text,
    ADD COLUMN calculation_base_snapshot text,
    ADD COLUMN fixed_amount_snapshot numeric(20,6),
    ADD COLUMN rounding_method_snapshot text,
    ADD COLUMN account_role_snapshot text,
    ADD COLUMN polarity_snapshot text,
    ADD COLUMN rounding_precision_snapshot smallint,
    ADD COLUMN currency_code_snapshot char(3) REFERENCES erp.currencies(code);
ALTER TABLE erp.ar_receipt_allocations
    ADD CONSTRAINT uq_ar_allocation_company_id UNIQUE(company_id,id);
ALTER TABLE erp.ap_disbursement_allocations
    ADD CONSTRAINT uq_ap_allocation_company_id UNIQUE(company_id,id);
-- A correction retains original tax versions even if they expire before reversal.
CREATE FUNCTION erp.check_posted_payment_source() RETURNS trigger LANGUAGE plpgsql
SET search_path=pg_catalog,erp AS $$
DECLARE v_total numeric;
BEGIN
    IF NEW.status<>'posted' THEN RETURN NULL; END IF;
    IF NOT EXISTS(SELECT 1 FROM erp.journal_entries e WHERE e.company_id=NEW.company_id
        AND e.id=NEW.journal_entry_id AND e.status='posted'
        AND e.source_type='payment' AND e.source_id=NEW.id) THEN
        RAISE EXCEPTION 'Payment source must reference its posted journal';
    END IF;
    SELECT coalesce(sum(tax_amount),0) INTO v_total FROM erp.payment_tax_components
    WHERE company_id=NEW.company_id AND payment_id=NEW.id;
    IF v_total<>NEW.withholding_total THEN RAISE EXCEPTION 'Withholding total mismatch'; END IF;
    IF NEW.reversal_of_payment_id IS NULL AND EXISTS(
        SELECT 1 FROM erp.payment_tax_components c
        JOIN erp.tax_codes tc ON tc.company_id=c.company_id AND tc.id=c.tax_code_id
        JOIN erp.tax_rate_versions v ON v.id=c.tax_rate_version_id
        JOIN erp.tax_rate_schedules s ON s.id=c.rate_schedule_id
        JOIN erp.tax_types tt ON tt.id=s.tax_type_id
        WHERE c.company_id=NEW.company_id AND c.payment_id=NEW.id
          AND (tc.tax_scope<>'withholding' OR tt.calculation_stage<>'payment'
            OR tt.tax_direction NOT IN ('withheld','bidirectional')
            OR NEW.payment_date<v.valid_from
            OR (v.valid_to IS NOT NULL AND NEW.payment_date>=v.valid_to))) THEN
        RAISE EXCEPTION 'Invalid or expired withholding configuration';
    END IF;
    RETURN NULL;
END $$;
DROP TRIGGER trg_payment_posted_link ON erp.payments;
CREATE CONSTRAINT TRIGGER trg_payment_posted_link AFTER INSERT OR UPDATE ON erp.payments
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION erp.check_posted_payment_source();
CREATE TABLE erp.payment_allocation_reversals(
    id uuid PRIMARY KEY DEFAULT uuidv7(),
    company_id uuid NOT NULL REFERENCES erp.companies(id),
    reversal_payment_id uuid NOT NULL,
    ar_allocation_id uuid,
    ap_allocation_id uuid,
    document_amount numeric(20,6) NOT NULL CHECK(document_amount>0),
    payment_amount numeric(20,6) NOT NULL CHECK(payment_amount>0),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK ((ar_allocation_id IS NULL)<>(ap_allocation_id IS NULL)),
    FOREIGN KEY(company_id,reversal_payment_id) REFERENCES erp.payments(company_id,id),
    FOREIGN KEY(company_id,ar_allocation_id) REFERENCES erp.ar_receipt_allocations(company_id,id),
    FOREIGN KEY(company_id,ap_allocation_id)
        REFERENCES erp.ap_disbursement_allocations(company_id,id),
    UNIQUE(company_id,ar_allocation_id),
    UNIQUE(company_id,ap_allocation_id)
);
ALTER TABLE erp.payment_allocation_reversals ENABLE ROW LEVEL SECURITY;
CREATE POLICY company_scope ON erp.payment_allocation_reversals
    USING(EXISTS(SELECT 1 FROM erp.companies c WHERE c.id=company_id
        AND c.tenant_id=erp.current_tenant_id()))
    WITH CHECK(EXISTS(SELECT 1 FROM erp.companies c WHERE c.id=company_id
        AND c.tenant_id=erp.current_tenant_id()));

CREATE FUNCTION erp.guard_payment_allocation() RETURNS trigger LANGUAGE plpgsql
SET search_path=pg_catalog,erp AS $$
DECLARE v_status text;
BEGIN
    IF TG_OP<>'INSERT' THEN
        SELECT status INTO v_status FROM erp.payments
        WHERE company_id=OLD.company_id AND id=OLD.payment_id FOR UPDATE;
        IF v_status IS DISTINCT FROM 'draft' THEN
            RAISE EXCEPTION 'Posted payment allocations are immutable';
        END IF;
        IF TG_OP='UPDATE' AND (NEW.company_id<>OLD.company_id OR NEW.payment_id<>OLD.payment_id)
        THEN RAISE EXCEPTION 'Allocation ownership is immutable'; END IF;
    END IF;
    IF TG_OP<>'DELETE' THEN
        SELECT status INTO v_status FROM erp.payments
        WHERE company_id=NEW.company_id AND id=NEW.payment_id FOR UPDATE;
        IF v_status IS DISTINCT FROM 'draft' THEN
            RAISE EXCEPTION 'Allocations require a draft payment';
        END IF;
        RETURN NEW;
    END IF;
    RETURN OLD;
END $$;
CREATE TRIGGER trg_ar_payment_immutable BEFORE INSERT OR UPDATE OR DELETE
    ON erp.ar_receipt_allocations FOR EACH ROW EXECUTE FUNCTION erp.guard_payment_allocation();
CREATE TRIGGER trg_ap_payment_immutable BEFORE INSERT OR UPDATE OR DELETE
    ON erp.ap_disbursement_allocations FOR EACH ROW EXECUTE FUNCTION erp.guard_payment_allocation();

CREATE FUNCTION erp.guard_payment_compensation() RETURNS trigger LANGUAGE plpgsql
SET search_path=pg_catalog,erp AS $$
DECLARE p erp.payments; a record;
BEGIN
    IF TG_OP<>'INSERT' THEN RAISE EXCEPTION 'Payment compensations are immutable'; END IF;
    SELECT * INTO p FROM erp.payments
    WHERE company_id=NEW.company_id AND id=NEW.reversal_payment_id FOR UPDATE;
    IF p.status<>'draft' OR p.reversal_of_payment_id IS NULL THEN
        RAISE EXCEPTION 'Compensation requires a draft reversal';
    END IF;
    IF NEW.ar_allocation_id IS NOT NULL THEN
        SELECT * INTO a FROM erp.ar_receipt_allocations
        WHERE company_id=NEW.company_id AND id=NEW.ar_allocation_id;
    ELSE
        SELECT * INTO a FROM erp.ap_disbursement_allocations
        WHERE company_id=NEW.company_id AND id=NEW.ap_allocation_id;
    END IF;
    IF a.payment_id IS DISTINCT FROM p.reversal_of_payment_id
       OR a.applied_document_amount<>NEW.document_amount
       OR a.applied_payment_amount<>NEW.payment_amount THEN
       RAISE EXCEPTION 'Compensation must match the original allocation';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER trg_payment_compensation_guard BEFORE INSERT OR UPDATE OR DELETE
    ON erp.payment_allocation_reversals
    FOR EACH ROW EXECUTE FUNCTION erp.guard_payment_compensation();

CREATE FUNCTION erp.check_payment_settlement() RETURNS trigger LANGUAGE plpgsql
SET search_path=pg_catalog,erp AS $$
DECLARE a record; d record; v_gross numeric; v_applied numeric; v_total numeric;
        v_count bigint; v_expected bigint; v_original erp.payments; v_control uuid;
        v_cash_net numeric; v_control_net numeric; v_sign integer;
BEGIN
    IF NEW.status<>'posted' THEN RETURN NULL; END IF;
    IF NEW.reversal_of_payment_id IS NOT NULL THEN
        SELECT * INTO v_original FROM erp.payments
        WHERE company_id=NEW.company_id AND id=NEW.reversal_of_payment_id FOR UPDATE;
        IF v_original.status IS DISTINCT FROM 'posted'
           OR v_original.reversal_of_payment_id IS NOT NULL
           OR NEW.payment_date<v_original.payment_date
           OR NEW.amount<>v_original.amount
           OR NEW.currency_code<>v_original.currency_code
           OR NEW.withholding_total<>v_original.withholding_total
           OR NEW.direction=v_original.direction THEN
            RAISE EXCEPTION 'Invalid payment reversal';
        END IF;
        IF NOT EXISTS(SELECT 1 FROM erp.journal_entries e
            WHERE e.company_id=NEW.company_id AND e.id=NEW.journal_entry_id
              AND e.reversal_of_entry_id=v_original.journal_entry_id) THEN
            RAISE EXCEPTION 'Reversal journal must reference original journal';
        END IF;
        IF EXISTS(
            (SELECT line_no,account_id,transaction_currency,exchange_rate,
                    transaction_debit,transaction_credit,debit_amount,credit_amount
             FROM erp.journal_lines WHERE company_id=NEW.company_id
               AND journal_entry_id=NEW.journal_entry_id
             EXCEPT
             SELECT line_no,account_id,transaction_currency,exchange_rate,
                    transaction_credit,transaction_debit,credit_amount,debit_amount
             FROM erp.journal_lines WHERE company_id=NEW.company_id
               AND journal_entry_id=v_original.journal_entry_id)
            UNION ALL
            (SELECT line_no,account_id,transaction_currency,exchange_rate,
                    transaction_credit,transaction_debit,credit_amount,debit_amount
             FROM erp.journal_lines WHERE company_id=NEW.company_id
               AND journal_entry_id=v_original.journal_entry_id
             EXCEPT
             SELECT line_no,account_id,transaction_currency,exchange_rate,
                    transaction_debit,transaction_credit,debit_amount,credit_amount
             FROM erp.journal_lines WHERE company_id=NEW.company_id
               AND journal_entry_id=NEW.journal_entry_id)
        ) THEN RAISE EXCEPTION 'Reversal journal must exactly compensate the original'; END IF;
        SELECT count(*) INTO v_count FROM erp.payment_allocation_reversals
        WHERE company_id=NEW.company_id AND reversal_payment_id=NEW.id;
        SELECT (SELECT count(*) FROM erp.ar_receipt_allocations WHERE company_id=NEW.company_id
                AND payment_id=v_original.id)
             + (SELECT count(*) FROM erp.ap_disbursement_allocations WHERE company_id=NEW.company_id
                AND payment_id=v_original.id) INTO v_expected;
        IF v_count<>v_expected THEN RAISE EXCEPTION 'Every allocation must be compensated'; END IF;
        RETURN NULL;
    END IF;
    IF NEW.cash_account_id IS NULL THEN RAISE EXCEPTION 'Cash account is required'; END IF;
    v_sign:=CASE NEW.direction WHEN 'receipt' THEN 1 ELSE -1 END;
    SELECT account_id INTO v_control FROM erp.accounting_posting_rules
    WHERE company_id=NEW.company_id
      AND event_code=CASE NEW.direction WHEN 'receipt' THEN 'sales_invoice' ELSE 'purchase_bill' END
      AND role_code=CASE NEW.direction
          WHEN 'receipt' THEN 'accounts_receivable' ELSE 'accounts_payable' END;
    SELECT coalesce(sum(transaction_debit-transaction_credit),0)*v_sign
    INTO v_cash_net FROM erp.journal_lines
    WHERE company_id=NEW.company_id AND journal_entry_id=NEW.journal_entry_id
      AND account_id=NEW.cash_account_id AND transaction_currency=NEW.currency_code;
    SELECT coalesce(sum(transaction_credit-transaction_debit),0)*v_sign
    INTO v_control_net FROM erp.journal_lines
    WHERE company_id=NEW.company_id AND journal_entry_id=NEW.journal_entry_id
      AND account_id=v_control AND transaction_currency=NEW.currency_code;
    IF v_control IS NULL OR v_cash_net<>NEW.amount
       OR v_control_net<>NEW.amount+NEW.withholding_total THEN
        RAISE EXCEPTION 'Payment journal must reconcile cash and settlement';
    END IF;
    IF EXISTS(SELECT 1 FROM erp.payment_tax_components c
        WHERE c.company_id=NEW.company_id AND c.payment_id=NEW.id AND (
            c.withholding_account_id_snapshot IS NULL
            OR c.rate_version_code_snapshot IS NULL
            OR c.calculation_method_snapshot IS NULL
            OR c.calculation_base_snapshot IS NULL
            OR c.rounding_method_snapshot IS NULL
            OR c.rounding_precision_snapshot IS NULL
            OR c.currency_code_snapshot IS DISTINCT FROM NEW.currency_code
            OR c.account_role_snapshot IS DISTINCT FROM 'withholding'
            OR c.polarity_snapshot IS DISTINCT FROM
                CASE NEW.direction WHEN 'receipt' THEN 'debit' ELSE 'credit' END)) THEN
        RAISE EXCEPTION 'Withholding snapshots are incomplete';
    END IF;
    SELECT coalesce(sum(amount),0) INTO v_total FROM (
        SELECT applied_payment_amount amount FROM erp.ar_receipt_allocations
        WHERE company_id=NEW.company_id AND payment_id=NEW.id
        UNION ALL SELECT applied_payment_amount FROM erp.ap_disbursement_allocations
        WHERE company_id=NEW.company_id AND payment_id=NEW.id
    ) amounts;
    IF v_total>NEW.amount+NEW.withholding_total THEN
        RAISE EXCEPTION 'Allocations exceed payment settlement capacity';
    END IF;
    FOR a IN
        SELECT id,sales_invoice_id document_id,applied_document_amount document_amount,
               applied_payment_amount payment_amount,allocation_exchange_rate rate,
               'receipt' direction
        FROM erp.ar_receipt_allocations WHERE company_id=NEW.company_id AND payment_id=NEW.id
        UNION ALL
        SELECT id,purchase_bill_id,applied_document_amount,applied_payment_amount,
               allocation_exchange_rate,'disbursement' FROM erp.ap_disbursement_allocations
        WHERE company_id=NEW.company_id AND payment_id=NEW.id
        ORDER BY document_id
    LOOP
        IF a.direction<>NEW.direction OR a.rate<>1 OR a.document_amount<>a.payment_amount THEN
            RAISE EXCEPTION 'Allocation direction or conversion is invalid';
        END IF;
        IF a.direction='receipt' THEN
            SELECT status,currency_code,partner_id partner,document_kind INTO d
            FROM erp.sales_invoices WHERE company_id=NEW.company_id AND id=a.document_id FOR UPDATE;
            SELECT coalesce(sum(gross_amount),0) INTO v_gross FROM erp.sales_invoice_lines
            WHERE company_id=NEW.company_id AND sales_invoice_id=a.document_id;
            SELECT coalesce(sum(x.applied_document_amount),0) INTO v_applied
            FROM erp.ar_receipt_allocations x JOIN erp.payments p
              ON p.company_id=x.company_id AND p.id=x.payment_id
            WHERE x.company_id=NEW.company_id AND x.sales_invoice_id=a.document_id
              AND p.status='posted' AND NOT EXISTS(SELECT 1 FROM erp.payments r
                WHERE r.company_id=p.company_id AND r.reversal_of_payment_id=p.id
                  AND r.status='posted' AND r.payment_date<=NEW.payment_date);
        ELSE
            SELECT status,currency_code,supplier_id partner,document_kind INTO d
            FROM erp.purchase_bills WHERE company_id=NEW.company_id AND id=a.document_id FOR UPDATE;
            SELECT coalesce(sum(gross_amount),0) INTO v_gross FROM erp.purchase_bill_lines
            WHERE company_id=NEW.company_id AND purchase_bill_id=a.document_id;
            SELECT coalesce(sum(x.applied_document_amount),0) INTO v_applied
            FROM erp.ap_disbursement_allocations x JOIN erp.payments p
              ON p.company_id=x.company_id AND p.id=x.payment_id
            WHERE x.company_id=NEW.company_id AND x.purchase_bill_id=a.document_id
              AND p.status='posted' AND NOT EXISTS(SELECT 1 FROM erp.payments r
                WHERE r.company_id=p.company_id AND r.reversal_of_payment_id=p.id
                  AND r.status='posted' AND r.payment_date<=NEW.payment_date);
        END IF;
        IF d.status IS DISTINCT FROM 'posted' OR d.currency_code<>NEW.currency_code
           OR d.partner IS DISTINCT FROM NEW.partner_id
           OR d.document_kind NOT IN ('invoice','bill') OR v_applied>v_gross THEN
            RAISE EXCEPTION 'Invalid target or document over-allocation';
        END IF;
    END LOOP;
    RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER trg_payment_settlement_check AFTER INSERT OR UPDATE ON erp.payments
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION erp.check_payment_settlement();

CREATE FUNCTION erp.audit_payment_change() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,erp,identity AS $$
DECLARE v_tenant uuid;
BEGIN
    SELECT tenant_id INTO v_tenant FROM erp.companies WHERE id=NEW.company_id;
    INSERT INTO erp.company_audit_events(tenant_id,company_id,actor_user_id,action,
        object_type,object_id,old_data,new_data,request_id)
    VALUES(v_tenant,NEW.company_id,identity.current_user_id(),
        CASE WHEN NEW.status='posted' THEN
            CASE WHEN NEW.reversal_of_payment_id IS NULL THEN 'payment.posted'
                ELSE 'payment.reversed' END
        WHEN TG_OP='INSERT' THEN 'payment.created' ELSE 'payment.updated' END,
        'payment',NEW.id::text,CASE WHEN TG_OP='INSERT' THEN NULL ELSE to_jsonb(OLD) END,
        to_jsonb(NEW),nullif(current_setting('app.request_id',true),''));
    RETURN NULL;
END $$;
CREATE TRIGGER trg_payment_audit AFTER INSERT OR UPDATE ON erp.payments
    FOR EACH ROW EXECUTE FUNCTION erp.audit_payment_change();
INSERT INTO identity.permissions(code,description) VALUES
    ('payments.view','Read payment documents'),
    ('payments.edit_draft','Edit payment drafts'),
    ('payments.reverse','Reverse posted payments')
ON CONFLICT DO NOTHING;

"""

REVERSE_SQL = r"""
DROP TRIGGER trg_payment_posted_link ON erp.payments;
DROP FUNCTION erp.check_posted_payment_source();
CREATE CONSTRAINT TRIGGER trg_payment_posted_link AFTER INSERT OR UPDATE ON erp.payments
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION erp.check_posted_source_document();
DROP TRIGGER trg_payment_audit ON erp.payments;
DROP FUNCTION erp.audit_payment_change();
DROP TRIGGER trg_payment_settlement_check ON erp.payments;
DROP FUNCTION erp.check_payment_settlement();
DROP TABLE erp.payment_allocation_reversals;
DROP FUNCTION erp.guard_payment_compensation();
DROP TRIGGER trg_ar_payment_immutable ON erp.ar_receipt_allocations;
DROP TRIGGER trg_ap_payment_immutable ON erp.ap_disbursement_allocations;
DROP FUNCTION erp.guard_payment_allocation();
ALTER TABLE erp.ar_receipt_allocations DROP CONSTRAINT uq_ar_allocation_company_id;
ALTER TABLE erp.ap_disbursement_allocations DROP CONSTRAINT uq_ap_allocation_company_id;
ALTER TABLE erp.payment_tax_components
    DROP COLUMN rate_version_code_snapshot,
    DROP COLUMN calculation_method_snapshot,
    DROP COLUMN calculation_base_snapshot,
    DROP COLUMN fixed_amount_snapshot,
    DROP COLUMN rounding_method_snapshot,
    DROP COLUMN account_role_snapshot,
    DROP COLUMN polarity_snapshot,
    DROP COLUMN rounding_precision_snapshot,
    DROP COLUMN currency_code_snapshot;
ALTER TABLE erp.payments DROP COLUMN reversal_of_payment_id, DROP COLUMN reversal_reason;
"""


def open_item_views(include_reversals: bool) -> str:
    statements = []
    for prefix, documents, lines, allocations, column, number, partner, date, kind in (
        (
            "ar",
            "sales_invoices",
            "sales_invoice_lines",
            "ar_receipt_allocations",
            "sales_invoice_id",
            "invoice_no",
            "partner_id",
            "issue_date",
            "invoice",
        ),
        (
            "ap",
            "purchase_bills",
            "purchase_bill_lines",
            "ap_disbursement_allocations",
            "purchase_bill_id",
            "bill_no",
            "supplier_id",
            "bill_date",
            "bill",
        ),
    ):
        extra = (
            """
            AND p.payment_date<=CURRENT_DATE
            AND NOT EXISTS(SELECT 1 FROM erp.payments r WHERE r.company_id=p.company_id
                AND r.reversal_of_payment_id=p.id AND r.status='posted'
                AND r.payment_date<=CURRENT_DATE)
        """
            if include_reversals
            else ""
        )
        sign = f"(CASE WHEN d.document_kind='{kind}' THEN 1 ELSE -1 END)"
        for v2 in (False, True):
            view = (
                f"v_open_{prefix}_items_v2"
                if v2
                else ("v_open_ar_invoices" if prefix == "ar" else "v_open_ap_bills")
            )
            total = f"{sign}*coalesce(t.total,0)" if v2 else "coalesce(t.total,0)"
            open_amount = (
                f"{total}-coalesce(a.applied,0)"
                if v2
                else (f"{sign}*greatest(coalesce(t.total,0)-coalesce(a.applied,0),0)")
            )
            total_name = "signed_document_total" if v2 else "document_total"
            open_name = "signed_open_amount" if v2 else "open_amount"
            statements.append(f"""
                CREATE OR REPLACE VIEW erp.{view} WITH(security_invoker=true) AS
                WITH totals AS (SELECT company_id,{column},sum(gross_amount) total
                    FROM erp.{lines} GROUP BY company_id,{column}),
                applied AS (SELECT a.company_id,a.{column},sum(applied_document_amount) applied
                    FROM erp.{allocations} a JOIN erp.payments p
                      ON p.company_id=a.company_id AND p.id=a.payment_id
                    WHERE p.status='posted' {extra} GROUP BY a.company_id,a.{column})
                SELECT d.company_id,d.id AS {column},d.{number},d.document_kind,
                    d.{partner},d.{date},d.due_date,d.currency_code,
                    {total} AS {total_name},coalesce(a.applied,0) AS applied_amount,
                    {open_amount} AS {open_name}
                FROM erp.{documents} d LEFT JOIN totals t
                  ON t.company_id=d.company_id AND t.{column}=d.id
                LEFT JOIN applied a ON a.company_id=d.company_id AND a.{column}=d.id
                WHERE d.status='posted';
            """)
    return "\n".join(statements)


class Migration(migrations.Migration):
    dependencies = [("database", "0014_phase5_payment_contract")]
    operations = [
        migrations.RunSQL(SQL, REVERSE_SQL),
        migrations.RunSQL(open_item_views(True), open_item_views(False)),
    ]
