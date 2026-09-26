-- ERP Accounting + scalable Django backend schema
-- PostgreSQL 18; run on an empty database inside a controlled migration job.
BEGIN;

-- ERP Accounting + Inventory reference schema
-- Target: PostgreSQL 18. Apply through versioned migrations, not by rerunning blindly.
-- UUID primary keys use native UUIDv7 for locality and external uniqueness.
-- Amounts/quantities use NUMERIC; no accounting values use float/double precision.

BEGIN;

CREATE SCHEMA IF NOT EXISTS erp;
CREATE SCHEMA IF NOT EXISTS licensing;

-- Global reference data. Seed currencies and units from controlled ISO/business data.
CREATE TABLE erp.currencies (
    code                char(3) PRIMARY KEY,
    name                text NOT NULL,
    minor_units         smallint NOT NULL CHECK (minor_units BETWEEN 0 AND 6),
    is_active           boolean NOT NULL DEFAULT true
);

CREATE TABLE erp.uom_categories (
    id                  uuid PRIMARY KEY DEFAULT uuidv7(),
    code                text NOT NULL UNIQUE,
    name                text NOT NULL
);

CREATE TABLE erp.uoms (
    id                  uuid PRIMARY KEY DEFAULT uuidv7(),
    category_id         uuid NOT NULL REFERENCES erp.uom_categories(id),
    code                text NOT NULL UNIQUE,
    name                text NOT NULL,
    to_base_factor      numeric(20, 10) NOT NULL CHECK (to_base_factor > 0)
);

-- Tenants own one or more legally separate companies.
CREATE TABLE erp.tenants (
    id                  uuid PRIMARY KEY DEFAULT uuidv7(),
    name                text NOT NULL,
    created_at          timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE erp.companies (
    id                      uuid PRIMARY KEY DEFAULT uuidv7(),
    tenant_id               uuid NOT NULL REFERENCES erp.tenants(id),
    code                    text NOT NULL,
    legal_name              text NOT NULL,
    functional_currency     char(3) NOT NULL REFERENCES erp.currencies(code),
    timezone_name           text NOT NULL DEFAULT 'UTC',
    is_active               boolean NOT NULL DEFAULT true,
    created_at              timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at              timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (tenant_id, code),
    UNIQUE (tenant_id, id),
    UNIQUE (id, functional_currency)
);

CREATE TABLE erp.fiscal_periods (
    id                  uuid PRIMARY KEY DEFAULT uuidv7(),
    company_id          uuid NOT NULL REFERENCES erp.companies(id),
    code                text NOT NULL,
    starts_on           date NOT NULL,
    ends_on             date NOT NULL,
    state               text NOT NULL DEFAULT 'open'
                        CHECK (state IN ('open', 'closed', 'locked')),
    created_at          timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK (starts_on <= ends_on),
    UNIQUE (company_id, code),
    UNIQUE (company_id, id),
    UNIQUE (company_id, starts_on)
);

CREATE TABLE erp.document_sequences (
    company_id          uuid NOT NULL REFERENCES erp.companies(id),
    fiscal_period_id    uuid NOT NULL,
    sequence_code       text NOT NULL,
    prefix              text NOT NULL DEFAULT '',
    next_value          bigint NOT NULL DEFAULT 1 CHECK (next_value > 0),
    padding_length      smallint NOT NULL DEFAULT 6 CHECK (padding_length BETWEEN 1 AND 18),
    PRIMARY KEY (company_id, fiscal_period_id, sequence_code),
    FOREIGN KEY (company_id, fiscal_period_id)
        REFERENCES erp.fiscal_periods(company_id, id)
);

-- Chart of accounts and posting configuration.
CREATE TABLE erp.accounts (
    id                  uuid PRIMARY KEY DEFAULT uuidv7(),
    company_id          uuid NOT NULL REFERENCES erp.companies(id),
    parent_account_id   uuid,
    code                text NOT NULL,
    name                text NOT NULL,
    account_type        text NOT NULL CHECK (
                            account_type IN (
                                'asset', 'liability', 'equity',
                                'revenue', 'expense', 'cost_of_sales',
                                'receivable', 'payable'
                            )
                        ),
    normal_balance      text NOT NULL CHECK (normal_balance IN ('debit', 'credit')),
    is_control_account  boolean NOT NULL DEFAULT false,
    allow_posting       boolean NOT NULL DEFAULT true,
    is_active           boolean NOT NULL DEFAULT true,
    created_at          timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at          timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK (parent_account_id IS NULL OR parent_account_id <> id),
    CHECK (allow_posting OR NOT is_control_account),
    UNIQUE (company_id, code),
    UNIQUE (company_id, id),
    FOREIGN KEY (company_id, parent_account_id)
        REFERENCES erp.accounts(company_id, id)
);

CREATE TABLE erp.journals (
    id                  uuid PRIMARY KEY DEFAULT uuidv7(),
    company_id          uuid NOT NULL REFERENCES erp.companies(id),
    code                text NOT NULL,
    name                text NOT NULL,
    journal_type        text NOT NULL CHECK (
                            journal_type IN (
                                'general', 'sales', 'purchase',
                                'cash', 'bank', 'inventory', 'manufacturing'
                            )
                        ),
    is_active           boolean NOT NULL DEFAULT true,
    created_at          timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at          timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (company_id, code),
    UNIQUE (company_id, id)
);

CREATE TABLE erp.accounting_posting_rules (
    id                  uuid PRIMARY KEY DEFAULT uuidv7(),
    company_id          uuid NOT NULL REFERENCES erp.companies(id),
    event_code          text NOT NULL,
    role_code           text NOT NULL,
    account_id          uuid NOT NULL,
    created_at          timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at          timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (company_id, event_code, role_code),
    FOREIGN KEY (company_id, account_id)
        REFERENCES erp.accounts(company_id, id)
);

-- Optional reporting dimensions (department, project, cost center, etc.).
CREATE TABLE erp.dimension_types (
    id                  uuid PRIMARY KEY DEFAULT uuidv7(),
    company_id          uuid NOT NULL REFERENCES erp.companies(id),
    code                text NOT NULL,
    name                text NOT NULL,
    is_required         boolean NOT NULL DEFAULT false,
    is_active           boolean NOT NULL DEFAULT true,
    UNIQUE (company_id, code),
    UNIQUE (company_id, id)
);

CREATE TABLE erp.dimension_values (
    id                  uuid PRIMARY KEY DEFAULT uuidv7(),
    company_id          uuid NOT NULL REFERENCES erp.companies(id),
    dimension_type_id   uuid NOT NULL,
    code                text NOT NULL,
    name                text NOT NULL,
    is_active           boolean NOT NULL DEFAULT true,
    UNIQUE (company_id, dimension_type_id, code),
    UNIQUE (company_id, dimension_type_id, id),
    FOREIGN KEY (company_id, dimension_type_id)
        REFERENCES erp.dimension_types(company_id, id)
);

-- A party can act as customer, supplier, or both for the same company.
CREATE TABLE erp.business_partners (
    id                  uuid PRIMARY KEY DEFAULT uuidv7(),
    company_id          uuid NOT NULL REFERENCES erp.companies(id),
    partner_code        text NOT NULL,
    display_name        text NOT NULL,
    legal_name          text,
    partner_kind        text NOT NULL DEFAULT 'other'
                        CHECK (partner_kind IN ('customer', 'supplier', 'both', 'other')),
    email               text,
    phone               text,
    is_active           boolean NOT NULL DEFAULT true,
    created_at          timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at          timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (company_id, partner_code),
    UNIQUE (company_id, id)
);

CREATE TABLE erp.partner_addresses (
    id                  uuid PRIMARY KEY DEFAULT uuidv7(),
    company_id          uuid NOT NULL REFERENCES erp.companies(id),
    partner_id          uuid NOT NULL,
    address_kind        text NOT NULL CHECK (address_kind IN ('billing', 'shipping', 'registered', 'other')),
    line_1              text NOT NULL,
    line_2              text,
    city                text,
    region              text,
    postal_code         text,
    country_code        char(2),
    is_default          boolean NOT NULL DEFAULT false,
    created_at          timestamptz NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY (company_id, partner_id)
        REFERENCES erp.business_partners(company_id, id)
);

-- Jurisdiction tax catalog. Seed this from versioned, reviewed country packs;
-- never hard-code rates or assume a single national tax per transaction.
CREATE TABLE erp.tax_jurisdictions (
    id                  uuid PRIMARY KEY DEFAULT uuidv7(),
    country_code        char(2) NOT NULL,
    jurisdiction_code  text NOT NULL,
    name                text NOT NULL,
    jurisdiction_level text NOT NULL CHECK (
                            jurisdiction_level IN ('country', 'state', 'province', 'region', 'city', 'district', 'other')
                        ),
    parent_jurisdiction_id uuid REFERENCES erp.tax_jurisdictions(id),
    is_active           boolean NOT NULL DEFAULT true,
    UNIQUE (country_code, jurisdiction_code),
    UNIQUE (country_code, id),
    UNIQUE (id, country_code)
);

CREATE TABLE erp.tax_types (
    id                  uuid PRIMARY KEY DEFAULT uuidv7(),
    jurisdiction_id     uuid NOT NULL REFERENCES erp.tax_jurisdictions(id),
    code                text NOT NULL,
    name                text NOT NULL,
    tax_family          text NOT NULL CHECK (
                            tax_family IN ('sales', 'vat', 'gst', 'excise', 'withholding', 'income', 'payroll', 'property', 'other')
                        ),
    calculation_stage   text NOT NULL CHECK (
                            calculation_stage IN ('line', 'document', 'payment', 'tax_period')
                        ),
    tax_direction       text NOT NULL CHECK (
                            tax_direction IN ('output', 'input', 'withheld', 'assessed', 'bidirectional')
                        ),
    is_active           boolean NOT NULL DEFAULT true,
    UNIQUE (jurisdiction_id, code),
    UNIQUE (jurisdiction_id, id)
);

CREATE TABLE erp.partner_tax_registrations (
    id                  uuid PRIMARY KEY DEFAULT uuidv7(),
    company_id          uuid NOT NULL,
    partner_id          uuid NOT NULL,
    jurisdiction_id     uuid NOT NULL,
    tax_type_id         uuid NOT NULL,
    registration_number text NOT NULL,
    valid_from          date NOT NULL,
    valid_to            date,
    is_verified         boolean NOT NULL DEFAULT false,
    is_active           boolean NOT NULL DEFAULT true,
    created_at          timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK (valid_to IS NULL OR valid_to > valid_from),
    UNIQUE (company_id, partner_id, jurisdiction_id, tax_type_id, registration_number),
    UNIQUE (company_id, id),
    FOREIGN KEY (company_id, partner_id)
        REFERENCES erp.business_partners(company_id, id),
    FOREIGN KEY (jurisdiction_id, tax_type_id)
        REFERENCES erp.tax_types(jurisdiction_id, id)
);

CREATE TABLE erp.tax_rate_schedules (
    id                  uuid PRIMARY KEY DEFAULT uuidv7(),
    jurisdiction_id     uuid NOT NULL,
    tax_type_id         uuid NOT NULL,
    schedule_code       text NOT NULL,
    name                text NOT NULL,
    is_active           boolean NOT NULL DEFAULT true,
    UNIQUE (jurisdiction_id, schedule_code),
    UNIQUE (jurisdiction_id, id),
    UNIQUE (jurisdiction_id, id, tax_type_id),
    FOREIGN KEY (jurisdiction_id, tax_type_id)
        REFERENCES erp.tax_types(jurisdiction_id, id)
);

-- Validity is [valid_from, valid_to); valid_to NULL means no scheduled end.
CREATE TABLE erp.tax_rate_versions (
    id                  uuid PRIMARY KEY DEFAULT uuidv7(),
    jurisdiction_id     uuid NOT NULL,
    rate_schedule_id    uuid NOT NULL,
    version_code        text NOT NULL,
    valid_from          date NOT NULL,
    valid_to            date,
    calculation_method  text NOT NULL CHECK (
                            calculation_method IN ('percentage', 'fixed', 'compound', 'external_rule')
                        ),
    calculation_base    text NOT NULL DEFAULT 'net_amount',
    rule_adapter_code   text,
    rate                numeric(12, 8) NOT NULL DEFAULT 0 CHECK (rate >= 0),
    fixed_amount        numeric(20, 6) CHECK (fixed_amount IS NULL OR fixed_amount >= 0),
    recovery_percent    numeric(9, 6) NOT NULL DEFAULT 100 CHECK (recovery_percent BETWEEN 0 AND 100),
    source_reference    text,
    created_at          timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK (valid_to IS NULL OR valid_to > valid_from),
    CHECK (calculation_method <> 'external_rule' OR rule_adapter_code IS NOT NULL),
    CHECK (calculation_method <> 'fixed' OR fixed_amount IS NOT NULL),
    UNIQUE (rate_schedule_id, version_code),
    UNIQUE (jurisdiction_id, rate_schedule_id, id),
    FOREIGN KEY (jurisdiction_id, rate_schedule_id)
        REFERENCES erp.tax_rate_schedules(jurisdiction_id, id)
);

CREATE TABLE erp.company_tax_registrations (
    id                  uuid PRIMARY KEY DEFAULT uuidv7(),
    company_id          uuid NOT NULL REFERENCES erp.companies(id),
    jurisdiction_id     uuid NOT NULL,
    tax_type_id         uuid NOT NULL,
    registration_number text NOT NULL,
    valid_from          date NOT NULL,
    valid_to            date,
    filing_frequency    text CHECK (filing_frequency IN ('monthly', 'quarterly', 'annual', 'other')),
    is_active           boolean NOT NULL DEFAULT true,
    created_at          timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK (valid_to IS NULL OR valid_to > valid_from),
    UNIQUE (company_id, jurisdiction_id, tax_type_id, registration_number),
    UNIQUE (company_id, id),
    FOREIGN KEY (jurisdiction_id, tax_type_id)
        REFERENCES erp.tax_types(jurisdiction_id, id)
);

CREATE TABLE erp.tax_codes (
    id                  uuid PRIMARY KEY DEFAULT uuidv7(),
    company_id          uuid NOT NULL REFERENCES erp.companies(id),
    tax_jurisdiction_id uuid NOT NULL REFERENCES erp.tax_jurisdictions(id),
    code                text NOT NULL,
    name                text NOT NULL,
    tax_scope           text NOT NULL CHECK (
                            tax_scope IN ('sales', 'purchase', 'both', 'withholding', 'income')
                        ),
    is_tax_inclusive    boolean NOT NULL DEFAULT false,
    is_active           boolean NOT NULL DEFAULT true,
    created_at          timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at          timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (company_id, code),
    UNIQUE (company_id, id),
    UNIQUE (company_id, tax_jurisdiction_id, id)
);

-- A tax code is a company-local group of one or more jurisdiction rules,
-- e.g. national plus provincial/city components, or a withholding component.
CREATE TABLE erp.tax_code_components (
    id                  uuid PRIMARY KEY DEFAULT uuidv7(),
    company_id          uuid NOT NULL,
    tax_jurisdiction_id uuid NOT NULL,
    tax_code_id         uuid NOT NULL,
    rate_schedule_id    uuid NOT NULL,
    sequence_no         smallint NOT NULL CHECK (sequence_no > 0),
    output_tax_account_id uuid,
    input_tax_account_id  uuid,
    withholding_account_id uuid,
    created_at          timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (company_id, tax_code_id, sequence_no),
    UNIQUE (company_id, tax_code_id, id),
    UNIQUE (company_id, tax_code_id, id, rate_schedule_id),
    UNIQUE (company_id, tax_code_id, id, tax_jurisdiction_id, rate_schedule_id),
    FOREIGN KEY (company_id, tax_code_id)
        REFERENCES erp.tax_codes(company_id, id),
    FOREIGN KEY (tax_jurisdiction_id, rate_schedule_id)
        REFERENCES erp.tax_rate_schedules(jurisdiction_id, id),
    FOREIGN KEY (company_id, output_tax_account_id)
        REFERENCES erp.accounts(company_id, id),
    FOREIGN KEY (company_id, input_tax_account_id)
        REFERENCES erp.accounts(company_id, id),
    FOREIGN KEY (company_id, withholding_account_id)
        REFERENCES erp.accounts(company_id, id)
);

COMMENT ON TABLE erp.tax_rate_versions IS
    'Effective-dated rules/rates. A jurisdiction tax adapter handles legal formulas, exemptions and filing formats beyond these generic fields.';

-- Product and location master data.
CREATE TABLE erp.items (
    id                  uuid PRIMARY KEY DEFAULT uuidv7(),
    company_id          uuid NOT NULL REFERENCES erp.companies(id),
    sku                 text NOT NULL,
    name                text NOT NULL,
    item_kind           text NOT NULL CHECK (item_kind IN ('stock', 'service', 'non_stock')),
    base_uom_id         uuid NOT NULL REFERENCES erp.uoms(id),
    track_lots          boolean NOT NULL DEFAULT false,
    track_serials       boolean NOT NULL DEFAULT false,
    is_active           boolean NOT NULL DEFAULT true,
    created_at          timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at          timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK (NOT (track_lots AND track_serials)),
    UNIQUE (company_id, sku),
    UNIQUE (company_id, id)
);

CREATE TABLE erp.item_accounting_profiles (
    id                      uuid PRIMARY KEY DEFAULT uuidv7(),
    company_id              uuid NOT NULL REFERENCES erp.companies(id),
    item_id                 uuid NOT NULL,
    inventory_account_id    uuid,
    revenue_account_id      uuid,
    cogs_account_id         uuid,
    purchase_account_id     uuid,
    created_at              timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at              timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (company_id, item_id),
    FOREIGN KEY (company_id, item_id)
        REFERENCES erp.items(company_id, id),
    FOREIGN KEY (company_id, inventory_account_id)
        REFERENCES erp.accounts(company_id, id),
    FOREIGN KEY (company_id, revenue_account_id)
        REFERENCES erp.accounts(company_id, id),
    FOREIGN KEY (company_id, cogs_account_id)
        REFERENCES erp.accounts(company_id, id),
    FOREIGN KEY (company_id, purchase_account_id)
        REFERENCES erp.accounts(company_id, id)
);

CREATE TABLE erp.sales_channels (
    id                  uuid PRIMARY KEY DEFAULT uuidv7(),
    company_id          uuid NOT NULL REFERENCES erp.companies(id),
    code                text NOT NULL,
    name                text NOT NULL,
    channel_kind        text NOT NULL CHECK (
                            channel_kind IN ('wholesale', 'retail', 'webstore', 'marketplace', 'other')
                        ),
    is_active           boolean NOT NULL DEFAULT true,
    created_at          timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at          timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (company_id, code),
    UNIQUE (company_id, id)
);

CREATE TABLE erp.warehouses (
    id                  uuid PRIMARY KEY DEFAULT uuidv7(),
    company_id          uuid NOT NULL REFERENCES erp.companies(id),
    code                text NOT NULL,
    name                text NOT NULL,
    address_text        text,
    is_active           boolean NOT NULL DEFAULT true,
    created_at          timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at          timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (company_id, code),
    UNIQUE (company_id, id)
);

-- Draft business documents. Header totals are derived from their immutable lines.
CREATE TABLE erp.sales_orders (
    id                  uuid PRIMARY KEY DEFAULT uuidv7(),
    company_id          uuid NOT NULL REFERENCES erp.companies(id),
    order_no            text NOT NULL,
    partner_id          uuid,
    channel_id          uuid NOT NULL,
    order_date          date NOT NULL,
    currency_code       char(3) NOT NULL REFERENCES erp.currencies(code),
    external_ref        text,
    status              text NOT NULL DEFAULT 'draft'
                        CHECK (status IN ('draft', 'confirmed', 'fulfilled', 'cancelled')),
    created_at          timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at          timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (company_id, order_no),
    UNIQUE (company_id, id),
    UNIQUE (company_id, channel_id, external_ref),
    FOREIGN KEY (company_id, partner_id)
        REFERENCES erp.business_partners(company_id, id),
    FOREIGN KEY (company_id, channel_id)
        REFERENCES erp.sales_channels(company_id, id)
);

CREATE TABLE erp.sales_order_lines (
    id                  uuid PRIMARY KEY DEFAULT uuidv7(),
    company_id          uuid NOT NULL REFERENCES erp.companies(id),
    sales_order_id      uuid NOT NULL,
    line_no             integer NOT NULL CHECK (line_no > 0),
    item_id             uuid NOT NULL,
    description         text NOT NULL,
    ordered_quantity    numeric(20, 6) NOT NULL CHECK (ordered_quantity > 0),
    unit_price          numeric(20, 6) NOT NULL CHECK (unit_price >= 0),
    discount_percent    numeric(9, 6) NOT NULL DEFAULT 0
                        CHECK (discount_percent BETWEEN 0 AND 100),
    tax_code_id         uuid,
    created_at          timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (company_id, sales_order_id, line_no),
    FOREIGN KEY (company_id, sales_order_id)
        REFERENCES erp.sales_orders(company_id, id),
    FOREIGN KEY (company_id, item_id)
        REFERENCES erp.items(company_id, id),
    FOREIGN KEY (company_id, tax_code_id)
        REFERENCES erp.tax_codes(company_id, id)
);

CREATE TABLE erp.purchase_orders (
    id                  uuid PRIMARY KEY DEFAULT uuidv7(),
    company_id          uuid NOT NULL REFERENCES erp.companies(id),
    order_no            text NOT NULL,
    supplier_id         uuid NOT NULL,
    warehouse_id        uuid NOT NULL,
    order_date          date NOT NULL,
    currency_code       char(3) NOT NULL REFERENCES erp.currencies(code),
    external_ref        text,
    status              text NOT NULL DEFAULT 'draft'
                        CHECK (status IN ('draft', 'confirmed', 'received', 'cancelled')),
    created_at          timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at          timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (company_id, order_no),
    UNIQUE (company_id, id),
    FOREIGN KEY (company_id, supplier_id)
        REFERENCES erp.business_partners(company_id, id),
    FOREIGN KEY (company_id, warehouse_id)
        REFERENCES erp.warehouses(company_id, id)
);

CREATE TABLE erp.purchase_order_lines (
    id                  uuid PRIMARY KEY DEFAULT uuidv7(),
    company_id          uuid NOT NULL REFERENCES erp.companies(id),
    purchase_order_id   uuid NOT NULL,
    line_no             integer NOT NULL CHECK (line_no > 0),
    item_id             uuid NOT NULL,
    description         text NOT NULL,
    ordered_quantity    numeric(20, 6) NOT NULL CHECK (ordered_quantity > 0),
    unit_cost           numeric(20, 6) NOT NULL CHECK (unit_cost >= 0),
    tax_code_id         uuid,
    created_at          timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (company_id, purchase_order_id, line_no),
    FOREIGN KEY (company_id, purchase_order_id)
        REFERENCES erp.purchase_orders(company_id, id),
    FOREIGN KEY (company_id, item_id)
        REFERENCES erp.items(company_id, id),
    FOREIGN KEY (company_id, tax_code_id)
        REFERENCES erp.tax_codes(company_id, id)
);

-- Ledger headers are created as draft, populated with lines, then posted by the function below.
CREATE TABLE erp.journal_entries (
    id                  uuid PRIMARY KEY DEFAULT uuidv7(),
    company_id          uuid NOT NULL REFERENCES erp.companies(id),
    journal_id          uuid NOT NULL,
    fiscal_period_id    uuid NOT NULL,
    entry_number        text NOT NULL,
    entry_date          date NOT NULL,
    status              text NOT NULL DEFAULT 'draft'
                        CHECK (status IN ('draft', 'posted', 'void')),
    description         text,
    source_type         text,
    source_id           uuid,
    idempotency_key     text,
    reversal_of_entry_id uuid,
    created_by          uuid,
    created_at          timestamptz NOT NULL DEFAULT clock_timestamp(),
    posted_at           timestamptz,
    CHECK ((source_type IS NULL) = (source_id IS NULL)),
    CHECK (status <> 'posted' OR posted_at IS NOT NULL),
    UNIQUE (company_id, id),
    UNIQUE (company_id, journal_id, entry_number),
    UNIQUE (company_id, idempotency_key),
    FOREIGN KEY (company_id, journal_id)
        REFERENCES erp.journals(company_id, id),
    FOREIGN KEY (company_id, fiscal_period_id)
        REFERENCES erp.fiscal_periods(company_id, id),
    FOREIGN KEY (company_id, reversal_of_entry_id)
        REFERENCES erp.journal_entries(company_id, id)
);

CREATE UNIQUE INDEX uq_journal_entry_single_reversal
    ON erp.journal_entries(company_id, reversal_of_entry_id)
    WHERE reversal_of_entry_id IS NOT NULL;

CREATE TABLE erp.journal_lines (
    id                  uuid PRIMARY KEY DEFAULT uuidv7(),
    company_id          uuid NOT NULL REFERENCES erp.companies(id),
    journal_entry_id    uuid NOT NULL,
    line_no             integer NOT NULL CHECK (line_no > 0),
    account_id          uuid NOT NULL,
    business_partner_id uuid,
    sales_channel_id    uuid,
    description         text,
    transaction_currency char(3) NOT NULL REFERENCES erp.currencies(code),
    exchange_rate       numeric(20, 10) NOT NULL CHECK (exchange_rate > 0),
    transaction_debit   numeric(20, 6) NOT NULL DEFAULT 0 CHECK (transaction_debit >= 0),
    transaction_credit  numeric(20, 6) NOT NULL DEFAULT 0 CHECK (transaction_credit >= 0),
    debit_amount        numeric(20, 6) NOT NULL DEFAULT 0 CHECK (debit_amount >= 0),
    credit_amount       numeric(20, 6) NOT NULL DEFAULT 0 CHECK (credit_amount >= 0),
    created_at          timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK (
        (transaction_debit > 0 AND transaction_credit = 0)
        OR (transaction_credit > 0 AND transaction_debit = 0)
    ),
    CHECK (
        (debit_amount > 0 AND credit_amount = 0)
        OR (credit_amount > 0 AND debit_amount = 0)
    ),
    UNIQUE (company_id, journal_entry_id, line_no),
    UNIQUE (company_id, id),
    FOREIGN KEY (company_id, journal_entry_id)
        REFERENCES erp.journal_entries(company_id, id),
    FOREIGN KEY (company_id, account_id)
        REFERENCES erp.accounts(company_id, id),
    FOREIGN KEY (company_id, business_partner_id)
        REFERENCES erp.business_partners(company_id, id),
    FOREIGN KEY (company_id, sales_channel_id)
        REFERENCES erp.sales_channels(company_id, id)
);

CREATE TABLE erp.journal_line_dimensions (
    company_id          uuid NOT NULL REFERENCES erp.companies(id),
    journal_line_id     uuid NOT NULL,
    dimension_type_id   uuid NOT NULL,
    dimension_value_id  uuid NOT NULL,
    PRIMARY KEY (company_id, journal_line_id, dimension_type_id),
    FOREIGN KEY (company_id, journal_line_id)
        REFERENCES erp.journal_lines(company_id, id),
    FOREIGN KEY (company_id, dimension_type_id, dimension_value_id)
        REFERENCES erp.dimension_values(company_id, dimension_type_id, id)
);

-- Issued sales documents preserve amounts and tax snapshots as historical facts.
CREATE TABLE erp.sales_invoices (
    id                  uuid PRIMARY KEY DEFAULT uuidv7(),
    company_id          uuid NOT NULL REFERENCES erp.companies(id),
    invoice_no          text NOT NULL,
    document_kind       text NOT NULL DEFAULT 'invoice'
                        CHECK (document_kind IN ('invoice', 'credit_note')),
    partner_id          uuid,
    sales_order_id      uuid,
    journal_entry_id    uuid,
    issue_date          date NOT NULL,
    tax_point_date      date NOT NULL,
    tax_jurisdiction_id uuid REFERENCES erp.tax_jurisdictions(id),
    due_date            date,
    currency_code       char(3) NOT NULL REFERENCES erp.currencies(code),
    exchange_rate       numeric(20, 10) NOT NULL DEFAULT 1 CHECK (exchange_rate > 0),
    status              text NOT NULL DEFAULT 'draft'
                        CHECK (status IN ('draft', 'posted', 'void')),
    created_at          timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at          timestamptz NOT NULL DEFAULT clock_timestamp(),
    posted_at           timestamptz,
    CHECK (due_date IS NULL OR due_date >= issue_date),
    CHECK (status <> 'posted' OR posted_at IS NOT NULL),
    UNIQUE (company_id, invoice_no),
    UNIQUE (company_id, id),
    UNIQUE (company_id, journal_entry_id),
    FOREIGN KEY (company_id, partner_id)
        REFERENCES erp.business_partners(company_id, id),
    FOREIGN KEY (company_id, sales_order_id)
        REFERENCES erp.sales_orders(company_id, id),
    FOREIGN KEY (company_id, journal_entry_id)
        REFERENCES erp.journal_entries(company_id, id)
);

CREATE TABLE erp.sales_invoice_address_snapshots (
    id                  uuid PRIMARY KEY DEFAULT uuidv7(),
    company_id          uuid NOT NULL REFERENCES erp.companies(id),
    sales_invoice_id    uuid NOT NULL,
    address_kind        text NOT NULL CHECK (address_kind IN ('billing', 'shipping')),
    recipient_name     text NOT NULL,
    tax_registration_no text,
    line_1              text NOT NULL,
    line_2              text,
    city                text,
    region              text,
    postal_code         text,
    country_code        char(2),
    UNIQUE (company_id, sales_invoice_id, address_kind),
    FOREIGN KEY (company_id, sales_invoice_id)
        REFERENCES erp.sales_invoices(company_id, id)
);

CREATE TABLE erp.sales_invoice_lines (
    id                  uuid PRIMARY KEY DEFAULT uuidv7(),
    company_id          uuid NOT NULL REFERENCES erp.companies(id),
    sales_invoice_id    uuid NOT NULL,
    line_no             integer NOT NULL CHECK (line_no > 0),
    item_id             uuid,
    line_account_id     uuid,
    description         text NOT NULL,
    quantity            numeric(20, 6) NOT NULL CHECK (quantity > 0),
    unit_price          numeric(20, 6) NOT NULL CHECK (unit_price >= 0),
    discount_amount     numeric(20, 6) NOT NULL DEFAULT 0 CHECK (discount_amount >= 0),
    net_amount          numeric(20, 6) NOT NULL CHECK (net_amount >= 0),
    tax_code_id         uuid,
    tax_amount          numeric(20, 6) NOT NULL DEFAULT 0 CHECK (tax_amount >= 0),
    gross_amount        numeric(20, 6) NOT NULL CHECK (gross_amount >= 0),
    created_at          timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK (gross_amount = net_amount + tax_amount),
    CHECK (item_id IS NOT NULL OR line_account_id IS NOT NULL),
    UNIQUE (company_id, sales_invoice_id, line_no),
    UNIQUE (company_id, sales_invoice_id, id, tax_code_id),
    FOREIGN KEY (company_id, sales_invoice_id)
        REFERENCES erp.sales_invoices(company_id, id),
    FOREIGN KEY (company_id, item_id)
        REFERENCES erp.items(company_id, id),
    FOREIGN KEY (company_id, line_account_id)
        REFERENCES erp.accounts(company_id, id),
    FOREIGN KEY (company_id, tax_code_id)
        REFERENCES erp.tax_codes(company_id, id)
);

CREATE TABLE erp.sales_invoice_tax_components (
    id                  uuid PRIMARY KEY DEFAULT uuidv7(),
    company_id          uuid NOT NULL,
    sales_invoice_id    uuid NOT NULL,
    sales_invoice_line_id uuid NOT NULL,
    tax_code_id         uuid NOT NULL,
    tax_code_component_id uuid NOT NULL,
    tax_jurisdiction_id uuid NOT NULL,
    rate_schedule_id    uuid NOT NULL,
    tax_rate_version_id uuid NOT NULL,
    taxable_base_amount numeric(20, 6) NOT NULL CHECK (taxable_base_amount >= 0),
    rate_snapshot       numeric(12, 8) NOT NULL CHECK (rate_snapshot >= 0),
    tax_inclusive_snapshot boolean NOT NULL,
    tax_amount          numeric(20, 6) NOT NULL CHECK (tax_amount >= 0),
    created_at          timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (company_id, sales_invoice_line_id, tax_code_component_id),
    FOREIGN KEY (company_id, sales_invoice_id, sales_invoice_line_id, tax_code_id)
        REFERENCES erp.sales_invoice_lines(company_id, sales_invoice_id, id, tax_code_id),
    FOREIGN KEY (company_id, tax_code_id, tax_code_component_id, tax_jurisdiction_id, rate_schedule_id)
        REFERENCES erp.tax_code_components(company_id, tax_code_id, id, tax_jurisdiction_id, rate_schedule_id),
    FOREIGN KEY (tax_jurisdiction_id, rate_schedule_id, tax_rate_version_id)
        REFERENCES erp.tax_rate_versions(jurisdiction_id, rate_schedule_id, id)
);

-- Supplier bills use the same snapshot pattern as sales invoices.
CREATE TABLE erp.purchase_bills (
    id                  uuid PRIMARY KEY DEFAULT uuidv7(),
    company_id          uuid NOT NULL REFERENCES erp.companies(id),
    bill_no             text NOT NULL,
    document_kind       text NOT NULL DEFAULT 'bill'
                        CHECK (document_kind IN ('bill', 'supplier_credit')),
    supplier_id         uuid NOT NULL,
    purchase_order_id   uuid,
    journal_entry_id    uuid,
    bill_date            date NOT NULL,
    tax_point_date      date NOT NULL,
    tax_jurisdiction_id uuid REFERENCES erp.tax_jurisdictions(id),
    due_date             date,
    currency_code        char(3) NOT NULL REFERENCES erp.currencies(code),
    exchange_rate        numeric(20, 10) NOT NULL DEFAULT 1 CHECK (exchange_rate > 0),
    status               text NOT NULL DEFAULT 'draft'
                         CHECK (status IN ('draft', 'posted', 'void')),
    created_at           timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at           timestamptz NOT NULL DEFAULT clock_timestamp(),
    posted_at            timestamptz,
    CHECK (due_date IS NULL OR due_date >= bill_date),
    CHECK (status <> 'posted' OR posted_at IS NOT NULL),
    UNIQUE (company_id, bill_no),
    UNIQUE (company_id, id),
    UNIQUE (company_id, journal_entry_id),
    FOREIGN KEY (company_id, supplier_id)
        REFERENCES erp.business_partners(company_id, id),
    FOREIGN KEY (company_id, purchase_order_id)
        REFERENCES erp.purchase_orders(company_id, id),
    FOREIGN KEY (company_id, journal_entry_id)
        REFERENCES erp.journal_entries(company_id, id)
);

CREATE TABLE erp.purchase_bill_address_snapshots (
    id                  uuid PRIMARY KEY DEFAULT uuidv7(),
    company_id          uuid NOT NULL REFERENCES erp.companies(id),
    purchase_bill_id    uuid NOT NULL,
    address_kind        text NOT NULL CHECK (address_kind IN ('supplier', 'ship_from', 'bill_to')),
    recipient_name     text NOT NULL,
    tax_registration_no text,
    line_1              text NOT NULL,
    line_2              text,
    city                text,
    region              text,
    postal_code         text,
    country_code        char(2),
    UNIQUE (company_id, purchase_bill_id, address_kind),
    FOREIGN KEY (company_id, purchase_bill_id)
        REFERENCES erp.purchase_bills(company_id, id)
);

CREATE TABLE erp.purchase_bill_lines (
    id                  uuid PRIMARY KEY DEFAULT uuidv7(),
    company_id          uuid NOT NULL REFERENCES erp.companies(id),
    purchase_bill_id    uuid NOT NULL,
    line_no             integer NOT NULL CHECK (line_no > 0),
    item_id             uuid,
    line_account_id     uuid,
    description         text NOT NULL,
    quantity            numeric(20, 6) NOT NULL CHECK (quantity > 0),
    unit_cost           numeric(20, 6) NOT NULL CHECK (unit_cost >= 0),
    net_amount          numeric(20, 6) NOT NULL CHECK (net_amount >= 0),
    tax_code_id         uuid,
    tax_amount          numeric(20, 6) NOT NULL DEFAULT 0 CHECK (tax_amount >= 0),
    gross_amount        numeric(20, 6) NOT NULL CHECK (gross_amount >= 0),
    created_at          timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK (gross_amount = net_amount + tax_amount),
    CHECK (item_id IS NOT NULL OR line_account_id IS NOT NULL),
    UNIQUE (company_id, purchase_bill_id, line_no),
    UNIQUE (company_id, purchase_bill_id, id, tax_code_id),
    FOREIGN KEY (company_id, purchase_bill_id)
        REFERENCES erp.purchase_bills(company_id, id),
    FOREIGN KEY (company_id, item_id)
        REFERENCES erp.items(company_id, id),
    FOREIGN KEY (company_id, line_account_id)
        REFERENCES erp.accounts(company_id, id),
    FOREIGN KEY (company_id, tax_code_id)
        REFERENCES erp.tax_codes(company_id, id)
);

CREATE TABLE erp.purchase_bill_tax_components (
    id                  uuid PRIMARY KEY DEFAULT uuidv7(),
    company_id          uuid NOT NULL,
    purchase_bill_id    uuid NOT NULL,
    purchase_bill_line_id uuid NOT NULL,
    tax_code_id         uuid NOT NULL,
    tax_code_component_id uuid NOT NULL,
    tax_jurisdiction_id uuid NOT NULL,
    rate_schedule_id    uuid NOT NULL,
    tax_rate_version_id uuid NOT NULL,
    taxable_base_amount numeric(20, 6) NOT NULL CHECK (taxable_base_amount >= 0),
    rate_snapshot       numeric(12, 8) NOT NULL CHECK (rate_snapshot >= 0),
    tax_inclusive_snapshot boolean NOT NULL,
    tax_amount          numeric(20, 6) NOT NULL CHECK (tax_amount >= 0),
    recoverable_amount  numeric(20, 6) NOT NULL DEFAULT 0 CHECK (recoverable_amount >= 0),
    created_at          timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK (recoverable_amount <= tax_amount),
    UNIQUE (company_id, purchase_bill_line_id, tax_code_component_id),
    FOREIGN KEY (company_id, purchase_bill_id, purchase_bill_line_id, tax_code_id)
        REFERENCES erp.purchase_bill_lines(company_id, purchase_bill_id, id, tax_code_id),
    FOREIGN KEY (company_id, tax_code_id, tax_code_component_id, tax_jurisdiction_id, rate_schedule_id)
        REFERENCES erp.tax_code_components(company_id, tax_code_id, id, tax_jurisdiction_id, rate_schedule_id),
    FOREIGN KEY (tax_jurisdiction_id, rate_schedule_id, tax_rate_version_id)
        REFERENCES erp.tax_rate_versions(jurisdiction_id, rate_schedule_id, id)
);

-- Incoming receipts and outgoing payments; allocation rows settle open documents.
CREATE TABLE erp.payments (
    id                  uuid PRIMARY KEY DEFAULT uuidv7(),
    company_id          uuid NOT NULL REFERENCES erp.companies(id),
    payment_no          text NOT NULL,
    direction           text NOT NULL CHECK (direction IN ('receipt', 'disbursement')),
    partner_id          uuid,
    journal_entry_id    uuid,
    payment_date        date NOT NULL,
    currency_code       char(3) NOT NULL REFERENCES erp.currencies(code),
    exchange_rate       numeric(20, 10) NOT NULL DEFAULT 1 CHECK (exchange_rate > 0),
    amount              numeric(20, 6) NOT NULL CHECK (amount > 0),
    withholding_total   numeric(20, 6) NOT NULL DEFAULT 0 CHECK (withholding_total >= 0),
    payment_method      text NOT NULL,
    external_reference  text,
    status              text NOT NULL DEFAULT 'draft'
                        CHECK (status IN ('draft', 'posted', 'void')),
    created_at          timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at          timestamptz NOT NULL DEFAULT clock_timestamp(),
    posted_at           timestamptz,
    CHECK (status <> 'posted' OR posted_at IS NOT NULL),
    UNIQUE (company_id, payment_no),
    UNIQUE (company_id, id),
    UNIQUE (company_id, journal_entry_id),
    FOREIGN KEY (company_id, partner_id)
        REFERENCES erp.business_partners(company_id, id),
    FOREIGN KEY (company_id, journal_entry_id)
        REFERENCES erp.journal_entries(company_id, id)
);

-- Payment-level withholding (including advance/final/adjustable treatments).
-- The source payment amount is the cash paid/received; the accounting service
-- books the withholding component to its configured tax payable/receivable account.
CREATE TABLE erp.payment_tax_components (
    id                  uuid PRIMARY KEY DEFAULT uuidv7(),
    company_id          uuid NOT NULL,
    payment_id          uuid NOT NULL,
    tax_code_id         uuid NOT NULL,
    tax_code_component_id uuid NOT NULL,
    tax_jurisdiction_id uuid NOT NULL,
    rate_schedule_id    uuid NOT NULL,
    tax_rate_version_id uuid NOT NULL,
    taxable_base_amount numeric(20, 6) NOT NULL CHECK (taxable_base_amount >= 0),
    rate_snapshot       numeric(12, 8) NOT NULL CHECK (rate_snapshot >= 0),
    tax_amount          numeric(20, 6) NOT NULL CHECK (tax_amount >= 0),
    tax_treatment       text NOT NULL CHECK (
                            tax_treatment IN ('advance', 'adjustable', 'final', 'minimum', 'other')
                        ),
    created_at          timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (company_id, payment_id, tax_code_component_id),
    FOREIGN KEY (company_id, payment_id)
        REFERENCES erp.payments(company_id, id),
    FOREIGN KEY (company_id, tax_code_id, tax_code_component_id, tax_jurisdiction_id, rate_schedule_id)
        REFERENCES erp.tax_code_components(company_id, tax_code_id, id, tax_jurisdiction_id, rate_schedule_id),
    FOREIGN KEY (tax_jurisdiction_id, rate_schedule_id, tax_rate_version_id)
        REFERENCES erp.tax_rate_versions(jurisdiction_id, rate_schedule_id, id)
);
COMMENT ON COLUMN erp.payments.amount IS
    'Cash amount actually received or disbursed in payment currency; withholding is stored separately and included in settlement accounting.';
COMMENT ON COLUMN erp.payments.withholding_total IS
    'Sum of payment-time withholding components; gross settlement is cash plus withholding for disbursements and applicable receipt offsets.';

-- A tax period is independent of the accounting fiscal period and follows the
-- registration's statutory reporting calendar (monthly, quarterly, annual, etc.).
CREATE TABLE erp.tax_filing_periods (
    id                  uuid PRIMARY KEY DEFAULT uuidv7(),
    company_id          uuid NOT NULL,
    tax_registration_id uuid NOT NULL,
    period_code         text NOT NULL,
    starts_on           date NOT NULL,
    ends_on             date NOT NULL,
    due_on              date,
    status              text NOT NULL DEFAULT 'open'
                        CHECK (status IN ('open', 'ready', 'filed', 'closed')),
    created_at          timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK (starts_on <= ends_on),
    UNIQUE (company_id, tax_registration_id, period_code),
    UNIQUE (company_id, id),
    FOREIGN KEY (company_id, tax_registration_id)
        REFERENCES erp.company_tax_registrations(company_id, id)
);

CREATE TABLE erp.tax_returns (
    id                  uuid PRIMARY KEY DEFAULT uuidv7(),
    company_id          uuid NOT NULL,
    tax_filing_period_id uuid NOT NULL,
    return_kind         text NOT NULL,
    status              text NOT NULL DEFAULT 'draft'
                        CHECK (status IN ('draft', 'calculated', 'submitted', 'accepted', 'rejected', 'amended')),
    currency_code       char(3) NOT NULL REFERENCES erp.currencies(code),
    taxable_base_total  numeric(20, 6) NOT NULL DEFAULT 0,
    tax_due_total       numeric(20, 6) NOT NULL DEFAULT 0,
    submitted_at        timestamptz,
    authority_receipt_ref text,
    filing_payload      jsonb,
    created_at          timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at          timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (company_id, id),
    UNIQUE (company_id, tax_filing_period_id, return_kind),
    FOREIGN KEY (company_id, tax_filing_period_id)
        REFERENCES erp.tax_filing_periods(company_id, id)
);

CREATE TABLE erp.tax_return_lines (
    id                  uuid PRIMARY KEY DEFAULT uuidv7(),
    company_id          uuid NOT NULL,
    tax_return_id       uuid NOT NULL,
    box_code            text NOT NULL,
    description         text NOT NULL,
    taxable_base_amount numeric(20, 6) NOT NULL DEFAULT 0,
    output_tax_amount   numeric(20, 6) NOT NULL DEFAULT 0,
    recoverable_tax_amount numeric(20, 6) NOT NULL DEFAULT 0,
    withholding_amount  numeric(20, 6) NOT NULL DEFAULT 0,
    adjustment_amount   numeric(20, 6) NOT NULL DEFAULT 0,
    created_at          timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (company_id, tax_return_id, box_code),
    FOREIGN KEY (company_id, tax_return_id)
        REFERENCES erp.tax_returns(company_id, id)
);

-- Income/corporate tax is assessed for a period from workpapers and then
-- posted through the GL. It is not modeled as an ordinary sales invoice line.
CREATE TABLE erp.tax_assessments (
    id                  uuid PRIMARY KEY DEFAULT uuidv7(),
    company_id          uuid NOT NULL,
    tax_filing_period_id uuid NOT NULL,
    journal_entry_id    uuid,
    assessment_kind     text NOT NULL CHECK (
                            assessment_kind IN ('current', 'estimated', 'deferred', 'adjustment', 'final')
                        ),
    status              text NOT NULL DEFAULT 'draft'
                        CHECK (status IN ('draft', 'calculated', 'posted')),
    currency_code       char(3) NOT NULL REFERENCES erp.currencies(code),
    tax_base_amount     numeric(20, 6) NOT NULL DEFAULT 0,
    tax_amount          numeric(20, 6) NOT NULL DEFAULT 0,
    calculated_at       timestamptz,
    created_at          timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (company_id, id),
    FOREIGN KEY (company_id, tax_filing_period_id)
        REFERENCES erp.tax_filing_periods(company_id, id),
    FOREIGN KEY (company_id, journal_entry_id)
        REFERENCES erp.journal_entries(company_id, id)
);

CREATE TABLE erp.tax_assessment_lines (
    id                  uuid PRIMARY KEY DEFAULT uuidv7(),
    company_id          uuid NOT NULL,
    tax_assessment_id   uuid NOT NULL,
    line_code           text NOT NULL,
    description         text NOT NULL,
    tax_base_amount     numeric(20, 6) NOT NULL DEFAULT 0,
    rate_snapshot       numeric(12, 8) CHECK (rate_snapshot IS NULL OR rate_snapshot >= 0),
    tax_effect_amount   numeric(20, 6) NOT NULL DEFAULT 0,
    source_reference    text,
    created_at          timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (company_id, tax_assessment_id, line_code),
    FOREIGN KEY (company_id, tax_assessment_id)
        REFERENCES erp.tax_assessments(company_id, id)
);

CREATE TABLE erp.ar_receipt_allocations (
    id                      uuid PRIMARY KEY DEFAULT uuidv7(),
    company_id              uuid NOT NULL REFERENCES erp.companies(id),
    payment_id              uuid NOT NULL,
    sales_invoice_id        uuid NOT NULL,
    applied_document_amount numeric(20, 6) NOT NULL CHECK (applied_document_amount > 0),
    applied_payment_amount  numeric(20, 6) NOT NULL CHECK (applied_payment_amount > 0),
    allocation_exchange_rate numeric(20, 10) NOT NULL CHECK (allocation_exchange_rate > 0),
    created_at              timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (company_id, payment_id, sales_invoice_id),
    FOREIGN KEY (company_id, payment_id)
        REFERENCES erp.payments(company_id, id),
    FOREIGN KEY (company_id, sales_invoice_id)
        REFERENCES erp.sales_invoices(company_id, id)
);
COMMENT ON COLUMN erp.ar_receipt_allocations.allocation_exchange_rate IS
    'Invoice-currency units per one payment-currency unit at allocation time.';

CREATE TABLE erp.ap_disbursement_allocations (
    id                      uuid PRIMARY KEY DEFAULT uuidv7(),
    company_id              uuid NOT NULL REFERENCES erp.companies(id),
    payment_id              uuid NOT NULL,
    purchase_bill_id        uuid NOT NULL,
    applied_document_amount numeric(20, 6) NOT NULL CHECK (applied_document_amount > 0),
    applied_payment_amount  numeric(20, 6) NOT NULL CHECK (applied_payment_amount > 0),
    allocation_exchange_rate numeric(20, 10) NOT NULL CHECK (allocation_exchange_rate > 0),
    created_at              timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (company_id, payment_id, purchase_bill_id),
    FOREIGN KEY (company_id, payment_id)
        REFERENCES erp.payments(company_id, id),
    FOREIGN KEY (company_id, purchase_bill_id)
        REFERENCES erp.purchase_bills(company_id, id)
);
COMMENT ON COLUMN erp.ap_disbursement_allocations.allocation_exchange_rate IS
    'Bill-currency units per one payment-currency unit at allocation time.';

-- Location/lot inventory ledger. Transfers are two opposite signed movement rows.
CREATE TABLE erp.inventory_lots (
    id                  uuid PRIMARY KEY DEFAULT uuidv7(),
    company_id          uuid NOT NULL REFERENCES erp.companies(id),
    item_id             uuid NOT NULL,
    lot_code            text NOT NULL,
    serial_code         text,
    received_on         date,
    expires_on          date,
    created_at          timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (company_id, item_id, lot_code),
    UNIQUE (company_id, item_id, id),
    FOREIGN KEY (company_id, item_id)
        REFERENCES erp.items(company_id, id)
);

CREATE TABLE erp.stock_movements (
    id                      uuid PRIMARY KEY DEFAULT uuidv7(),
    company_id              uuid NOT NULL REFERENCES erp.companies(id),
    event_key               text NOT NULL,
    occurred_at             timestamptz NOT NULL,
    warehouse_id            uuid NOT NULL,
    item_id                 uuid NOT NULL,
    lot_id                  uuid,
    movement_kind           text NOT NULL CHECK (
                                movement_kind IN (
                                    'receipt', 'issue', 'transfer',
                                    'adjustment', 'production'
                                )
                            ),
    quantity_delta          numeric(20, 6) NOT NULL CHECK (quantity_delta <> 0),
    unit_cost_company       numeric(20, 6) NOT NULL DEFAULT 0 CHECK (unit_cost_company >= 0),
    value_delta_company     numeric(20, 6) NOT NULL,
    source_type             text,
    source_id               uuid,
    source_line_id          uuid,
    journal_entry_id        uuid,
    created_at              timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK ((source_type IS NULL) = (source_id IS NULL)),
    CHECK (
        (quantity_delta > 0 AND value_delta_company >= 0)
        OR (quantity_delta < 0 AND value_delta_company <= 0)
    ),
    UNIQUE (company_id, event_key),
    UNIQUE (company_id, id),
    FOREIGN KEY (company_id, warehouse_id)
        REFERENCES erp.warehouses(company_id, id),
    FOREIGN KEY (company_id, item_id)
        REFERENCES erp.items(company_id, id),
    FOREIGN KEY (company_id, item_id, lot_id)
        REFERENCES erp.inventory_lots(company_id, item_id, id),
    FOREIGN KEY (company_id, journal_entry_id)
        REFERENCES erp.journal_entries(company_id, id)
);

-- Cost allocations connect each stock issue to the receipt layers it consumes.
-- The costing service locks layers and writes these rows in FIFO/AVCO order.
CREATE TABLE erp.inventory_cost_allocations (
    id                      uuid PRIMARY KEY DEFAULT uuidv7(),
    company_id              uuid NOT NULL REFERENCES erp.companies(id),
    issue_movement_id       uuid NOT NULL,
    receipt_movement_id     uuid NOT NULL,
    quantity                numeric(20, 6) NOT NULL CHECK (quantity > 0),
    unit_cost_company       numeric(20, 6) NOT NULL CHECK (unit_cost_company >= 0),
    value_company           numeric(20, 6) NOT NULL CHECK (value_company >= 0),
    created_at              timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (company_id, issue_movement_id, receipt_movement_id),
    FOREIGN KEY (company_id, issue_movement_id)
        REFERENCES erp.stock_movements(company_id, id),
    FOREIGN KEY (company_id, receipt_movement_id)
        REFERENCES erp.stock_movements(company_id, id)
);

CREATE TABLE erp.inventory_reservations (
    id                  uuid PRIMARY KEY DEFAULT uuidv7(),
    company_id          uuid NOT NULL REFERENCES erp.companies(id),
    warehouse_id        uuid NOT NULL,
    item_id             uuid NOT NULL,
    lot_id              uuid,
    reservation_key     text NOT NULL,
    source_type         text NOT NULL,
    source_id           uuid NOT NULL,
    quantity            numeric(20, 6) NOT NULL CHECK (quantity > 0),
    status              text NOT NULL DEFAULT 'active'
                        CHECK (status IN ('active', 'released', 'consumed')),
    created_at          timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at          timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (company_id, reservation_key),
    FOREIGN KEY (company_id, warehouse_id)
        REFERENCES erp.warehouses(company_id, id),
    FOREIGN KEY (company_id, item_id)
        REFERENCES erp.items(company_id, id),
    FOREIGN KEY (company_id, item_id, lot_id)
        REFERENCES erp.inventory_lots(company_id, item_id, id)
);

-- BOM revisions and production execution.
CREATE TABLE erp.boms (
    id                  uuid PRIMARY KEY DEFAULT uuidv7(),
    company_id          uuid NOT NULL REFERENCES erp.companies(id),
    output_item_id      uuid NOT NULL,
    revision            text NOT NULL,
    output_quantity     numeric(20, 6) NOT NULL CHECK (output_quantity > 0),
    status              text NOT NULL DEFAULT 'draft'
                        CHECK (status IN ('draft', 'active', 'retired')),
    effective_from      date,
    effective_to        date,
    created_at          timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at          timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK (effective_to IS NULL OR effective_from IS NULL OR effective_to >= effective_from),
    UNIQUE (company_id, output_item_id, revision),
    UNIQUE (company_id, id),
    FOREIGN KEY (company_id, output_item_id)
        REFERENCES erp.items(company_id, id)
);

CREATE TABLE erp.bom_lines (
    id                  uuid PRIMARY KEY DEFAULT uuidv7(),
    company_id          uuid NOT NULL REFERENCES erp.companies(id),
    bom_id              uuid NOT NULL,
    line_no             integer NOT NULL CHECK (line_no > 0),
    component_item_id   uuid NOT NULL,
    quantity_per_output numeric(20, 6) NOT NULL CHECK (quantity_per_output > 0),
    scrap_percent       numeric(9, 6) NOT NULL DEFAULT 0
                        CHECK (scrap_percent BETWEEN 0 AND 100),
    UNIQUE (company_id, bom_id, line_no),
    FOREIGN KEY (company_id, bom_id)
        REFERENCES erp.boms(company_id, id),
    FOREIGN KEY (company_id, component_item_id)
        REFERENCES erp.items(company_id, id)
);

CREATE TABLE erp.production_orders (
    id                  uuid PRIMARY KEY DEFAULT uuidv7(),
    company_id          uuid NOT NULL REFERENCES erp.companies(id),
    production_no       text NOT NULL,
    bom_id              uuid NOT NULL,
    output_item_id      uuid NOT NULL,
    warehouse_id        uuid NOT NULL,
    planned_quantity    numeric(20, 6) NOT NULL CHECK (planned_quantity > 0),
    completed_quantity  numeric(20, 6) NOT NULL DEFAULT 0 CHECK (completed_quantity >= 0),
    status              text NOT NULL DEFAULT 'planned'
                        CHECK (status IN ('planned', 'released', 'in_progress', 'completed', 'cancelled')),
    planned_start       date,
    planned_end         date,
    created_at          timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at          timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK (planned_end IS NULL OR planned_start IS NULL OR planned_end >= planned_start),
    UNIQUE (company_id, production_no),
    UNIQUE (company_id, id),
    FOREIGN KEY (company_id, bom_id)
        REFERENCES erp.boms(company_id, id),
    FOREIGN KEY (company_id, output_item_id)
        REFERENCES erp.items(company_id, id),
    FOREIGN KEY (company_id, warehouse_id)
        REFERENCES erp.warehouses(company_id, id)
);

CREATE TABLE erp.production_material_issues (
    id                  uuid PRIMARY KEY DEFAULT uuidv7(),
    company_id          uuid NOT NULL REFERENCES erp.companies(id),
    production_order_id uuid NOT NULL,
    stock_movement_id   uuid NOT NULL,
    component_item_id   uuid NOT NULL,
    quantity_issued     numeric(20, 6) NOT NULL CHECK (quantity_issued > 0),
    cost_company        numeric(20, 6) NOT NULL CHECK (cost_company >= 0),
    created_at          timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (company_id, stock_movement_id),
    FOREIGN KEY (company_id, production_order_id)
        REFERENCES erp.production_orders(company_id, id),
    FOREIGN KEY (company_id, stock_movement_id)
        REFERENCES erp.stock_movements(company_id, id),
    FOREIGN KEY (company_id, component_item_id)
        REFERENCES erp.items(company_id, id)
);

CREATE TABLE erp.production_outputs (
    id                  uuid PRIMARY KEY DEFAULT uuidv7(),
    company_id          uuid NOT NULL REFERENCES erp.companies(id),
    production_order_id uuid NOT NULL,
    stock_movement_id   uuid NOT NULL,
    output_item_id      uuid NOT NULL,
    quantity_completed  numeric(20, 6) NOT NULL CHECK (quantity_completed > 0),
    cost_company        numeric(20, 6) NOT NULL CHECK (cost_company >= 0),
    created_at          timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (company_id, stock_movement_id),
    FOREIGN KEY (company_id, production_order_id)
        REFERENCES erp.production_orders(company_id, id),
    FOREIGN KEY (company_id, stock_movement_id)
        REFERENCES erp.stock_movements(company_id, id),
    FOREIGN KEY (company_id, output_item_id)
        REFERENCES erp.items(company_id, id)
);

-- Webhook/integration inbox: retain source event for idempotency, replay and diagnosis.
CREATE TABLE erp.integration_event_inbox (
    id                  uuid PRIMARY KEY DEFAULT uuidv7(),
    company_id          uuid NOT NULL REFERENCES erp.companies(id),
    sales_channel_id    uuid NOT NULL,
    external_event_id   text NOT NULL,
    event_type          text NOT NULL,
    payload             jsonb NOT NULL,
    status              text NOT NULL DEFAULT 'received'
                        CHECK (status IN ('received', 'processing', 'processed', 'failed')),
    received_at         timestamptz NOT NULL DEFAULT clock_timestamp(),
    processed_at        timestamptz,
    last_error           text,
    UNIQUE (company_id, sales_channel_id, external_event_id),
    FOREIGN KEY (company_id, sales_channel_id)
        REFERENCES erp.sales_channels(company_id, id)
);

CREATE TABLE erp.outbox_events (
    id                  uuid PRIMARY KEY DEFAULT uuidv7(),
    company_id          uuid NOT NULL REFERENCES erp.companies(id),
    event_key           text NOT NULL,
    aggregate_type      text NOT NULL,
    aggregate_id        uuid NOT NULL,
    event_type          text NOT NULL,
    payload             jsonb NOT NULL,
    occurred_at         timestamptz NOT NULL DEFAULT clock_timestamp(),
    claim_token         uuid,
    claim_expires_at    timestamptz,
    delivered_at        timestamptz,
    attempt_count       integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    last_error          text,
    UNIQUE (company_id, event_key)
);

-- License catalog and customer entitlement records. In production these may
-- live in a separately operated control-plane database/service.
CREATE TABLE licensing.products (
    id                  uuid PRIMARY KEY DEFAULT uuidv7(),
    product_code        text NOT NULL UNIQUE,
    name                text NOT NULL,
    is_active           boolean NOT NULL DEFAULT true,
    created_at          timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE licensing.plans (
    id                  uuid PRIMARY KEY DEFAULT uuidv7(),
    product_id          uuid NOT NULL REFERENCES licensing.products(id),
    plan_code           text NOT NULL,
    name                text NOT NULL,
    is_active           boolean NOT NULL DEFAULT true,
    created_at          timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (product_id, plan_code),
    UNIQUE (product_id, id)
);

CREATE TABLE licensing.plan_versions (
    id                  uuid PRIMARY KEY DEFAULT uuidv7(),
    product_id          uuid NOT NULL,
    plan_id             uuid NOT NULL,
    version_number      integer NOT NULL CHECK (version_number > 0),
    term_unit           text NOT NULL CHECK (term_unit IN ('day', 'month', 'year', 'lifetime')),
    term_count          integer,
    max_activations     integer NOT NULL DEFAULT 1 CHECK (max_activations > 0),
    permits_offline_use boolean NOT NULL DEFAULT false,
    price_currency      char(3) REFERENCES erp.currencies(code),
    price_amount        numeric(20, 6) CHECK (price_amount IS NULL OR price_amount >= 0),
    published_at        timestamptz,
    created_at          timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK ((term_unit = 'lifetime' AND term_count IS NULL) OR
           (term_unit <> 'lifetime' AND term_count > 0)),
    UNIQUE (plan_id, version_number),
    UNIQUE (plan_id, id),
    FOREIGN KEY (product_id, plan_id)
        REFERENCES licensing.plans(product_id, id)
);

CREATE TABLE licensing.plan_features (
    plan_version_id     uuid NOT NULL REFERENCES licensing.plan_versions(id),
    feature_code        text NOT NULL,
    is_enabled          boolean NOT NULL DEFAULT true,
    limit_value         numeric(20, 6),
    PRIMARY KEY (plan_version_id, feature_code),
    CHECK (limit_value IS NULL OR limit_value >= 0)
);

CREATE TABLE licensing.licenses (
    id                  uuid PRIMARY KEY DEFAULT uuidv7(),
    tenant_id           uuid NOT NULL REFERENCES erp.tenants(id),
    product_id          uuid NOT NULL REFERENCES licensing.products(id),
    license_number_hash bytea NOT NULL UNIQUE,
    license_number_last4 char(4) NOT NULL,
    status              text NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending', 'active', 'suspended', 'revoked')),
    issued_at           timestamptz NOT NULL DEFAULT clock_timestamp(),
    created_at          timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at          timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (tenant_id, id),
    CHECK (octet_length(license_number_hash) >= 32)
);

-- Each purchase/renewal adds a term. Expiry is exclusive: now() < expires_at.
-- Lifetime access has NULL expires_at. Support/update dates are independent.
CREATE TABLE licensing.license_terms (
    id                  uuid PRIMARY KEY DEFAULT uuidv7(),
    tenant_id           uuid NOT NULL,
    license_id          uuid NOT NULL,
    plan_version_id     uuid NOT NULL REFERENCES licensing.plan_versions(id),
    term_unit_snapshot  text NOT NULL CHECK (term_unit_snapshot IN ('day', 'month', 'year', 'lifetime')),
    term_count_snapshot integer,
    starts_at           timestamptz NOT NULL,
    expires_at          timestamptz,
    support_valid_until timestamptz,
    updates_valid_until timestamptz,
    max_activations_snapshot integer NOT NULL CHECK (max_activations_snapshot > 0),
    created_at          timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK ((term_unit_snapshot = 'lifetime' AND expires_at IS NULL AND term_count_snapshot IS NULL) OR
           (term_unit_snapshot <> 'lifetime' AND expires_at > starts_at AND term_count_snapshot > 0)),
    UNIQUE (license_id, id),
    UNIQUE (tenant_id, id),
    UNIQUE (tenant_id, license_id, id),
    FOREIGN KEY (tenant_id, license_id)
        REFERENCES licensing.licenses(tenant_id, id)
);

CREATE TABLE licensing.license_activations (
    id                  uuid PRIMARY KEY DEFAULT uuidv7(),
    tenant_id           uuid NOT NULL,
    license_id          uuid NOT NULL,
    device_fingerprint_hash bytea NOT NULL,
    device_public_key   bytea,
    activated_at        timestamptz NOT NULL DEFAULT clock_timestamp(),
    last_seen_at        timestamptz NOT NULL DEFAULT clock_timestamp(),
    deactivated_at      timestamptz,
    UNIQUE (license_id, device_fingerprint_hash),
    UNIQUE (tenant_id, id),
    UNIQUE (tenant_id, license_id, id),
    FOREIGN KEY (tenant_id, license_id)
        REFERENCES licensing.licenses(tenant_id, id),
    CHECK (octet_length(device_fingerprint_hash) >= 32)
);

CREATE TABLE licensing.license_events (
    id                  uuid PRIMARY KEY DEFAULT uuidv7(),
    tenant_id           uuid NOT NULL,
    license_id          uuid NOT NULL,
    event_type          text NOT NULL CHECK (
                            event_type IN ('issued', 'activated', 'renewed', 'suspended', 'resumed', 'revoked', 'grant_issued')
                        ),
    actor_reference     text,
    occurred_at         timestamptz NOT NULL DEFAULT clock_timestamp(),
    event_data          jsonb NOT NULL DEFAULT '{}'::jsonb,
    FOREIGN KEY (tenant_id, license_id)
        REFERENCES licensing.licenses(tenant_id, id)
);

CREATE TABLE licensing.signing_keys (
    key_id              text PRIMARY KEY,
    algorithm           text NOT NULL,
    public_key          bytea NOT NULL,
    valid_from          timestamptz NOT NULL,
    valid_to            timestamptz,
    revoked_at          timestamptz,
    CHECK (valid_to IS NULL OR valid_to > valid_from)
);

CREATE TABLE licensing.signed_grants (
    id                  uuid PRIMARY KEY DEFAULT uuidv7(),
    tenant_id           uuid NOT NULL,
    license_id          uuid NOT NULL,
    license_term_id     uuid NOT NULL,
    activation_id       uuid,
    key_id              text NOT NULL REFERENCES licensing.signing_keys(key_id),
    grant_version       smallint NOT NULL CHECK (grant_version > 0),
    canonical_payload   bytea NOT NULL,
    signature           bytea NOT NULL,
    issued_at           timestamptz NOT NULL DEFAULT clock_timestamp(),
    grant_valid_until   timestamptz,
    FOREIGN KEY (tenant_id, license_id, license_term_id)
        REFERENCES licensing.license_terms(tenant_id, license_id, id),
    FOREIGN KEY (tenant_id, license_id, activation_id)
        REFERENCES licensing.license_activations(tenant_id, license_id, id)
);

-- Indexes for common company-scoped reads, joins, and ledger traversal.
CREATE INDEX ix_fiscal_periods_company_dates
    ON erp.fiscal_periods(company_id, starts_on, ends_on);
CREATE INDEX ix_accounts_parent
    ON erp.accounts(company_id, parent_account_id);
CREATE INDEX ix_partners_name
    ON erp.business_partners(company_id, display_name);
CREATE INDEX ix_partner_addresses_partner
    ON erp.partner_addresses(company_id, partner_id);
CREATE INDEX ix_company_tax_registrations_lookup
    ON erp.company_tax_registrations(company_id, jurisdiction_id, tax_type_id, valid_from DESC)
    WHERE is_active;
CREATE INDEX ix_partner_tax_registrations_lookup
    ON erp.partner_tax_registrations(company_id, partner_id, jurisdiction_id, tax_type_id, valid_from DESC)
    WHERE is_active;
CREATE INDEX ix_tax_codes_jurisdiction
    ON erp.tax_codes(company_id, tax_jurisdiction_id, code)
    WHERE is_active;
CREATE INDEX ix_tax_code_components_schedule
    ON erp.tax_code_components(company_id, tax_code_id, sequence_no);
CREATE INDEX ix_tax_rate_versions_effective
    ON erp.tax_rate_versions(jurisdiction_id, rate_schedule_id, valid_from DESC);
CREATE INDEX ix_tax_invoice_components_line
    ON erp.sales_invoice_tax_components(company_id, sales_invoice_id, sales_invoice_line_id);
CREATE INDEX ix_tax_bill_components_line
    ON erp.purchase_bill_tax_components(company_id, purchase_bill_id, purchase_bill_line_id);
CREATE INDEX ix_payment_tax_components_payment
    ON erp.payment_tax_components(company_id, payment_id);
CREATE INDEX ix_tax_filing_periods_registration_dates
    ON erp.tax_filing_periods(company_id, tax_registration_id, starts_on, ends_on);
CREATE INDEX ix_tax_returns_period_status
    ON erp.tax_returns(company_id, tax_filing_period_id, status);
CREATE INDEX ix_tax_assessments_period_status
    ON erp.tax_assessments(company_id, tax_filing_period_id, status);
CREATE INDEX ix_posting_rules_account
    ON erp.accounting_posting_rules(company_id, account_id);
CREATE INDEX ix_sales_orders_partner_date
    ON erp.sales_orders(company_id, partner_id, order_date DESC);
CREATE INDEX ix_sales_order_lines_order
    ON erp.sales_order_lines(company_id, sales_order_id);
CREATE INDEX ix_sales_order_lines_item
    ON erp.sales_order_lines(company_id, item_id);
CREATE INDEX ix_sales_order_lines_tax
    ON erp.sales_order_lines(company_id, tax_code_id)
    WHERE tax_code_id IS NOT NULL;
CREATE INDEX ix_purchase_orders_supplier_date
    ON erp.purchase_orders(company_id, supplier_id, order_date DESC);
CREATE INDEX ix_purchase_order_lines_order
    ON erp.purchase_order_lines(company_id, purchase_order_id);
CREATE INDEX ix_purchase_order_lines_item
    ON erp.purchase_order_lines(company_id, item_id);
CREATE INDEX ix_purchase_order_lines_tax
    ON erp.purchase_order_lines(company_id, tax_code_id)
    WHERE tax_code_id IS NOT NULL;
CREATE INDEX ix_journal_entries_date
    ON erp.journal_entries(company_id, entry_date, id);
CREATE INDEX ix_journal_entries_period_status
    ON erp.journal_entries(company_id, fiscal_period_id, status);
CREATE INDEX ix_journal_lines_account_entry
    ON erp.journal_lines(company_id, account_id, journal_entry_id);
CREATE INDEX ix_journal_lines_entry
    ON erp.journal_lines(company_id, journal_entry_id, line_no);
CREATE INDEX ix_journal_lines_partner
    ON erp.journal_lines(company_id, business_partner_id, journal_entry_id)
    WHERE business_partner_id IS NOT NULL;
CREATE INDEX ix_journal_lines_channel
    ON erp.journal_lines(company_id, sales_channel_id, journal_entry_id)
    WHERE sales_channel_id IS NOT NULL;
CREATE INDEX ix_journal_line_dimensions_value
    ON erp.journal_line_dimensions(company_id, dimension_type_id, dimension_value_id);
CREATE INDEX ix_sales_invoices_partner_due
    ON erp.sales_invoices(company_id, partner_id, due_date)
    WHERE status = 'posted';
CREATE INDEX ix_sales_invoices_order
    ON erp.sales_invoices(company_id, sales_order_id)
    WHERE sales_order_id IS NOT NULL;
CREATE INDEX ix_sales_invoice_lines_invoice
    ON erp.sales_invoice_lines(company_id, sales_invoice_id);
CREATE INDEX ix_sales_invoice_lines_item
    ON erp.sales_invoice_lines(company_id, item_id)
    WHERE item_id IS NOT NULL;
CREATE INDEX ix_sales_invoice_lines_account
    ON erp.sales_invoice_lines(company_id, line_account_id)
    WHERE line_account_id IS NOT NULL;
CREATE INDEX ix_sales_invoice_lines_tax
    ON erp.sales_invoice_lines(company_id, tax_code_id)
    WHERE tax_code_id IS NOT NULL;
CREATE INDEX ix_purchase_bills_supplier_due
    ON erp.purchase_bills(company_id, supplier_id, due_date)
    WHERE status = 'posted';
CREATE INDEX ix_purchase_bills_order
    ON erp.purchase_bills(company_id, purchase_order_id)
    WHERE purchase_order_id IS NOT NULL;
CREATE INDEX ix_purchase_bill_lines_bill
    ON erp.purchase_bill_lines(company_id, purchase_bill_id);
CREATE INDEX ix_purchase_bill_lines_item
    ON erp.purchase_bill_lines(company_id, item_id)
    WHERE item_id IS NOT NULL;
CREATE INDEX ix_purchase_bill_lines_account
    ON erp.purchase_bill_lines(company_id, line_account_id)
    WHERE line_account_id IS NOT NULL;
CREATE INDEX ix_purchase_bill_lines_tax
    ON erp.purchase_bill_lines(company_id, tax_code_id)
    WHERE tax_code_id IS NOT NULL;
CREATE INDEX ix_ar_allocations_invoice
    ON erp.ar_receipt_allocations(company_id, sales_invoice_id);
CREATE INDEX ix_ap_allocations_bill
    ON erp.ap_disbursement_allocations(company_id, purchase_bill_id);
CREATE INDEX ix_stock_movements_item_location_date
    ON erp.stock_movements(company_id, warehouse_id, item_id, occurred_at DESC);
CREATE INDEX ix_stock_movements_item_date
    ON erp.stock_movements(company_id, item_id, occurred_at DESC);
CREATE INDEX ix_stock_movements_journal
    ON erp.stock_movements(company_id, journal_entry_id)
    WHERE journal_entry_id IS NOT NULL;
CREATE INDEX ix_stock_movements_lot
    ON erp.stock_movements(company_id, item_id, lot_id)
    WHERE lot_id IS NOT NULL;
CREATE INDEX ix_inventory_cost_allocations_receipt
    ON erp.inventory_cost_allocations(company_id, receipt_movement_id);
CREATE INDEX ix_reservations_item_location
    ON erp.inventory_reservations(company_id, item_id, warehouse_id)
    WHERE status = 'active';
CREATE INDEX ix_reservations_lot
    ON erp.inventory_reservations(company_id, item_id, lot_id)
    WHERE lot_id IS NOT NULL AND status = 'active';
CREATE INDEX ix_reservations_available
    ON erp.inventory_reservations(company_id, warehouse_id, item_id)
    WHERE status = 'active';
CREATE INDEX ix_production_orders_status
    ON erp.production_orders(company_id, status, planned_start);
CREATE INDEX ix_production_orders_bom
    ON erp.production_orders(company_id, bom_id);
CREATE INDEX ix_production_orders_output_item
    ON erp.production_orders(company_id, output_item_id);
CREATE INDEX ix_production_orders_warehouse
    ON erp.production_orders(company_id, warehouse_id);
CREATE INDEX ix_bom_lines_component
    ON erp.bom_lines(company_id, component_item_id);
CREATE INDEX ix_production_material_issues_order
    ON erp.production_material_issues(company_id, production_order_id);
CREATE INDEX ix_production_material_issues_component
    ON erp.production_material_issues(company_id, component_item_id);
CREATE INDEX ix_production_outputs_order
    ON erp.production_outputs(company_id, production_order_id);
CREATE INDEX ix_production_outputs_item
    ON erp.production_outputs(company_id, output_item_id);
CREATE INDEX ix_integration_inbox_pending
    ON erp.integration_event_inbox(company_id, received_at)
    WHERE status IN ('received', 'failed');
CREATE INDEX ix_outbox_events_pending
    ON erp.outbox_events(claim_expires_at, occurred_at, id)
    WHERE delivered_at IS NULL;
CREATE INDEX ix_license_terms_tenant_expiry
    ON licensing.license_terms(tenant_id, license_id, starts_at, expires_at);
CREATE INDEX ix_license_activations_active
    ON licensing.license_activations(tenant_id, license_id, last_seen_at DESC)
    WHERE deactivated_at IS NULL;
CREATE INDEX ix_license_events_history
    ON licensing.license_events(tenant_id, license_id, occurred_at DESC);
CREATE INDEX ix_license_grants_lookup
    ON licensing.signed_grants(tenant_id, license_id, issued_at DESC);

-- Serialize rule edits per schedule and reject overlapping effective windows.
CREATE OR REPLACE FUNCTION erp.guard_tax_rate_version_overlap()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_OP = 'UPDATE' AND EXISTS (
        SELECT 1
          FROM erp.sales_invoice_tax_components c
          JOIN erp.sales_invoices i
            ON i.company_id = c.company_id AND i.id = c.sales_invoice_id
         WHERE c.tax_jurisdiction_id = OLD.jurisdiction_id
           AND c.rate_schedule_id = OLD.rate_schedule_id
           AND c.tax_rate_version_id = OLD.id
           AND i.status = 'posted'
        UNION ALL
        SELECT 1
          FROM erp.purchase_bill_tax_components c
          JOIN erp.purchase_bills b
            ON b.company_id = c.company_id AND b.id = c.purchase_bill_id
         WHERE c.tax_jurisdiction_id = OLD.jurisdiction_id
           AND c.rate_schedule_id = OLD.rate_schedule_id
           AND c.tax_rate_version_id = OLD.id
           AND b.status = 'posted'
        UNION ALL
        SELECT 1
          FROM erp.payment_tax_components c
          JOIN erp.payments p
            ON p.company_id = c.company_id AND p.id = c.payment_id
         WHERE c.tax_jurisdiction_id = OLD.jurisdiction_id
           AND c.rate_schedule_id = OLD.rate_schedule_id
           AND c.tax_rate_version_id = OLD.id
           AND p.status = 'posted'
    ) THEN
        IF (to_jsonb(NEW) - 'valid_to') IS DISTINCT FROM (to_jsonb(OLD) - 'valid_to') THEN
            RAISE EXCEPTION 'A tax rate version used by a posted transaction cannot be rewritten';
        END IF;
        IF NEW.valid_to IS NOT NULL AND EXISTS (
            SELECT 1
              FROM erp.sales_invoice_tax_components c
              JOIN erp.sales_invoices i ON i.company_id = c.company_id AND i.id = c.sales_invoice_id
             WHERE c.tax_rate_version_id = OLD.id AND i.status = 'posted'
               AND i.tax_point_date >= NEW.valid_to
            UNION ALL
            SELECT 1
              FROM erp.purchase_bill_tax_components c
              JOIN erp.purchase_bills b ON b.company_id = c.company_id AND b.id = c.purchase_bill_id
             WHERE c.tax_rate_version_id = OLD.id AND b.status = 'posted'
               AND b.tax_point_date >= NEW.valid_to
            UNION ALL
            SELECT 1
              FROM erp.payment_tax_components c
              JOIN erp.payments p ON p.company_id = c.company_id AND p.id = c.payment_id
             WHERE c.tax_rate_version_id = OLD.id AND p.status = 'posted'
               AND p.payment_date >= NEW.valid_to
        ) THEN
            RAISE EXCEPTION 'Tax rate end date cannot precede a posted transaction using it';
        END IF;
    END IF;

    PERFORM 1
      FROM erp.tax_rate_schedules s
     WHERE s.jurisdiction_id = NEW.jurisdiction_id
       AND s.id = NEW.rate_schedule_id
     FOR UPDATE;

    IF EXISTS (
        SELECT 1
          FROM erp.tax_rate_versions v
         WHERE v.jurisdiction_id = NEW.jurisdiction_id
           AND v.rate_schedule_id = NEW.rate_schedule_id
           AND v.id <> NEW.id
           AND daterange(v.valid_from, v.valid_to, '[)')
               && daterange(NEW.valid_from, NEW.valid_to, '[)')
    ) THEN
        RAISE EXCEPTION 'Tax rate validity windows cannot overlap for one schedule';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_tax_rate_version_no_overlap
    BEFORE INSERT OR UPDATE ON erp.tax_rate_versions
    FOR EACH ROW EXECUTE FUNCTION erp.guard_tax_rate_version_overlap();

CREATE OR REPLACE FUNCTION erp.guard_tax_filing_period_overlap()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    PERFORM 1
      FROM erp.company_tax_registrations r
     WHERE r.company_id = NEW.company_id AND r.id = NEW.tax_registration_id
     FOR UPDATE;
    IF EXISTS (
        SELECT 1
          FROM erp.tax_filing_periods p
         WHERE p.company_id = NEW.company_id
           AND p.tax_registration_id = NEW.tax_registration_id
           AND p.id <> NEW.id
           AND daterange(p.starts_on, p.ends_on + 1, '[)')
               && daterange(NEW.starts_on, NEW.ends_on + 1, '[)')
    ) THEN
        RAISE EXCEPTION 'Tax filing periods cannot overlap for one registration';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_tax_filing_period_no_overlap
    BEFORE INSERT OR UPDATE ON erp.tax_filing_periods
    FOR EACH ROW EXECUTE FUNCTION erp.guard_tax_filing_period_overlap();

-- License terms are issued snapshots; renewal is a new row. Locking the parent
-- makes overlapping concurrent activations/renewals deterministic.
CREATE OR REPLACE FUNCTION licensing.guard_license_term_insert()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    v_product_id uuid;
    v_license_status text;
    v_plan_product_id uuid;
    v_term_unit text;
    v_term_count integer;
    v_max_activations integer;
BEGIN
    SELECT l.product_id, l.status
      INTO v_product_id, v_license_status
      FROM licensing.licenses l
     WHERE l.tenant_id = NEW.tenant_id AND l.id = NEW.license_id
     FOR UPDATE;
    IF NOT FOUND OR v_license_status = 'revoked' THEN
        RAISE EXCEPTION 'License is missing or revoked';
    END IF;

    SELECT p.product_id, pv.term_unit, pv.term_count, pv.max_activations
      INTO v_plan_product_id, v_term_unit, v_term_count, v_max_activations
      FROM licensing.plan_versions pv
      JOIN licensing.plans p ON p.id = pv.plan_id
     WHERE pv.id = NEW.plan_version_id;
    IF NOT FOUND OR v_plan_product_id <> v_product_id
       OR NEW.term_unit_snapshot <> v_term_unit
       OR NEW.term_count_snapshot IS DISTINCT FROM v_term_count
       OR NEW.max_activations_snapshot <> v_max_activations THEN
        RAISE EXCEPTION 'License term snapshot does not match the purchased plan version';
    END IF;

    IF EXISTS (
        SELECT 1 FROM licensing.license_terms t
         WHERE t.license_id = NEW.license_id
           AND tstzrange(t.starts_at, t.expires_at, '[)')
               && tstzrange(NEW.starts_at, NEW.expires_at, '[)')
    ) THEN
        RAISE EXCEPTION 'License term overlaps an existing term';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_license_term_issue
    BEFORE INSERT ON licensing.license_terms
    FOR EACH ROW EXECUTE FUNCTION licensing.guard_license_term_insert();

CREATE OR REPLACE FUNCTION licensing.guard_license_activation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    v_status text;
    v_limit integer;
    v_active_count bigint;
BEGIN
    IF TG_OP = 'UPDATE' AND (
        NEW.tenant_id IS DISTINCT FROM OLD.tenant_id
        OR NEW.license_id IS DISTINCT FROM OLD.license_id
        OR NEW.device_fingerprint_hash IS DISTINCT FROM OLD.device_fingerprint_hash
    ) THEN
        RAISE EXCEPTION 'An activation cannot be moved or assigned to a different device';
    END IF;

    PERFORM 1 FROM licensing.licenses l
     WHERE l.tenant_id = NEW.tenant_id AND l.id = NEW.license_id
     FOR UPDATE;
    SELECT l.status INTO v_status
      FROM licensing.licenses l
     WHERE l.tenant_id = NEW.tenant_id AND l.id = NEW.license_id;
    IF v_status IS DISTINCT FROM 'active' THEN
        RAISE EXCEPTION 'Only an active license can be activated';
    END IF;

    SELECT t.max_activations_snapshot INTO v_limit
      FROM licensing.license_terms t
     WHERE t.license_id = NEW.license_id
       AND t.starts_at <= clock_timestamp()
       AND (t.expires_at IS NULL OR clock_timestamp() < t.expires_at)
     ORDER BY t.starts_at DESC
     LIMIT 1;
    IF v_limit IS NULL THEN
        RAISE EXCEPTION 'License has no currently valid access term';
    END IF;

    IF TG_OP = 'UPDATE' THEN
        SELECT count(*) INTO v_active_count
          FROM licensing.license_activations a
         WHERE a.license_id = NEW.license_id
           AND a.deactivated_at IS NULL
           AND a.id <> OLD.id;
    ELSE
        SELECT count(*) INTO v_active_count
          FROM licensing.license_activations a
         WHERE a.license_id = NEW.license_id
           AND a.deactivated_at IS NULL;
    END IF;
    IF NEW.deactivated_at IS NULL AND v_active_count >= v_limit THEN
        RAISE EXCEPTION 'License activation limit reached';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_license_activation_limit
    BEFORE INSERT OR UPDATE ON licensing.license_activations
    FOR EACH ROW EXECUTE FUNCTION licensing.guard_license_activation();

CREATE OR REPLACE FUNCTION licensing.reject_issued_record_change()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION '% rows are append-only; issue a new version/event instead', TG_TABLE_NAME;
END;
$$;

CREATE TRIGGER trg_license_terms_immutable
    BEFORE UPDATE OR DELETE ON licensing.license_terms
    FOR EACH ROW EXECUTE FUNCTION licensing.reject_issued_record_change();
CREATE TRIGGER trg_license_events_immutable
    BEFORE UPDATE OR DELETE ON licensing.license_events
    FOR EACH ROW EXECUTE FUNCTION licensing.reject_issued_record_change();
CREATE TRIGGER trg_license_grants_immutable
    BEFORE UPDATE OR DELETE ON licensing.signed_grants
    FOR EACH ROW EXECUTE FUNCTION licensing.reject_issued_record_change();

CREATE OR REPLACE FUNCTION licensing.guard_published_plan_version()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_OP = 'DELETE' AND OLD.published_at IS NOT NULL THEN
        RAISE EXCEPTION 'Published plan versions are immutable';
    ELSIF TG_OP = 'UPDATE' AND OLD.published_at IS NOT NULL THEN
        RAISE EXCEPTION 'Published plan versions are immutable; publish a new version';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_published_plan_version_immutable
    BEFORE UPDATE OR DELETE ON licensing.plan_versions
    FOR EACH ROW EXECUTE FUNCTION licensing.guard_published_plan_version();

CREATE OR REPLACE FUNCTION licensing.guard_published_plan_features()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    v_plan_version_id uuid;
    v_published_at timestamptz;
BEGIN
    IF TG_OP = 'UPDATE' OR TG_OP = 'DELETE' THEN
        v_plan_version_id := OLD.plan_version_id;
    ELSE
        v_plan_version_id := NEW.plan_version_id;
    END IF;
    SELECT published_at INTO v_published_at
      FROM licensing.plan_versions
     WHERE id = v_plan_version_id
     FOR UPDATE;
    IF v_published_at IS NOT NULL THEN
        RAISE EXCEPTION 'Features of a published plan version are immutable';
    END IF;
    IF TG_OP = 'UPDATE' AND NEW.plan_version_id IS DISTINCT FROM OLD.plan_version_id THEN
        SELECT published_at INTO v_published_at
          FROM licensing.plan_versions
         WHERE id = NEW.plan_version_id
         FOR UPDATE;
        IF v_published_at IS NOT NULL THEN
            RAISE EXCEPTION 'Features cannot be moved to a published plan version';
        END IF;
    END IF;
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_published_plan_features_immutable
    BEFORE INSERT OR UPDATE OR DELETE ON licensing.plan_features
    FOR EACH ROW EXECUTE FUNCTION licensing.guard_published_plan_features();

-- Sequence rows are configured per company, fiscal period, and document type.
-- This update locks one sequence row and avoids unsafe MAX(number) + 1 logic.
CREATE OR REPLACE FUNCTION erp.allocate_document_number(
    p_company_id uuid,
    p_fiscal_period_id uuid,
    p_sequence_code text
)
RETURNS text
LANGUAGE plpgsql
AS $$
DECLARE
    v_prefix text;
    v_number bigint;
    v_padding smallint;
BEGIN
    UPDATE erp.document_sequences
       SET next_value = next_value + 1
     WHERE company_id = p_company_id
       AND fiscal_period_id = p_fiscal_period_id
       AND sequence_code = p_sequence_code
     RETURNING prefix, next_value - 1, padding_length
          INTO v_prefix, v_number, v_padding;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'Document sequence is not configured';
    END IF;
    RETURN v_prefix || lpad(v_number::text, v_padding, '0');
END;
$$;

-- Small generic trigger: convenience timestamp only, never accounting behavior.
CREATE OR REPLACE FUNCTION erp.touch_updated_at()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    NEW.updated_at := clock_timestamp();
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_companies_updated_at
    BEFORE UPDATE ON erp.companies FOR EACH ROW EXECUTE FUNCTION erp.touch_updated_at();
CREATE TRIGGER trg_accounts_updated_at
    BEFORE UPDATE ON erp.accounts FOR EACH ROW EXECUTE FUNCTION erp.touch_updated_at();
CREATE TRIGGER trg_journals_updated_at
    BEFORE UPDATE ON erp.journals FOR EACH ROW EXECUTE FUNCTION erp.touch_updated_at();
CREATE TRIGGER trg_posting_rules_updated_at
    BEFORE UPDATE ON erp.accounting_posting_rules FOR EACH ROW EXECUTE FUNCTION erp.touch_updated_at();
CREATE TRIGGER trg_partners_updated_at
    BEFORE UPDATE ON erp.business_partners FOR EACH ROW EXECUTE FUNCTION erp.touch_updated_at();
CREATE TRIGGER trg_tax_codes_updated_at
    BEFORE UPDATE ON erp.tax_codes FOR EACH ROW EXECUTE FUNCTION erp.touch_updated_at();
CREATE TRIGGER trg_tax_returns_updated_at
    BEFORE UPDATE ON erp.tax_returns FOR EACH ROW EXECUTE FUNCTION erp.touch_updated_at();
CREATE TRIGGER trg_licenses_updated_at
    BEFORE UPDATE ON licensing.licenses FOR EACH ROW EXECUTE FUNCTION erp.touch_updated_at();
CREATE TRIGGER trg_items_updated_at
    BEFORE UPDATE ON erp.items FOR EACH ROW EXECUTE FUNCTION erp.touch_updated_at();
CREATE TRIGGER trg_item_profiles_updated_at
    BEFORE UPDATE ON erp.item_accounting_profiles FOR EACH ROW EXECUTE FUNCTION erp.touch_updated_at();
CREATE TRIGGER trg_sales_channels_updated_at
    BEFORE UPDATE ON erp.sales_channels FOR EACH ROW EXECUTE FUNCTION erp.touch_updated_at();
CREATE TRIGGER trg_warehouses_updated_at
    BEFORE UPDATE ON erp.warehouses FOR EACH ROW EXECUTE FUNCTION erp.touch_updated_at();
CREATE TRIGGER trg_sales_orders_updated_at
    BEFORE UPDATE ON erp.sales_orders FOR EACH ROW EXECUTE FUNCTION erp.touch_updated_at();
CREATE TRIGGER trg_purchase_orders_updated_at
    BEFORE UPDATE ON erp.purchase_orders FOR EACH ROW EXECUTE FUNCTION erp.touch_updated_at();
CREATE TRIGGER trg_sales_invoices_updated_at
    BEFORE UPDATE ON erp.sales_invoices FOR EACH ROW EXECUTE FUNCTION erp.touch_updated_at();
CREATE TRIGGER trg_purchase_bills_updated_at
    BEFORE UPDATE ON erp.purchase_bills FOR EACH ROW EXECUTE FUNCTION erp.touch_updated_at();
CREATE TRIGGER trg_payments_updated_at
    BEFORE UPDATE ON erp.payments FOR EACH ROW EXECUTE FUNCTION erp.touch_updated_at();
CREATE TRIGGER trg_reservations_updated_at
    BEFORE UPDATE ON erp.inventory_reservations FOR EACH ROW EXECUTE FUNCTION erp.touch_updated_at();
CREATE TRIGGER trg_boms_updated_at
    BEFORE UPDATE ON erp.boms FOR EACH ROW EXECUTE FUNCTION erp.touch_updated_at();
CREATE TRIGGER trg_production_orders_updated_at
    BEFORE UPDATE ON erp.production_orders FOR EACH ROW EXECUTE FUNCTION erp.touch_updated_at();

-- A posted journal is immutable. A reversal is a new journal entry that references it.
CREATE OR REPLACE FUNCTION erp.guard_posted_journal_entry()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    v_period erp.fiscal_periods%ROWTYPE;
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.status <> 'draft' THEN
            RAISE EXCEPTION 'Journal entries must be created as draft';
        END IF;
        RETURN NEW;
    END IF;

    IF TG_OP = 'DELETE' THEN
        IF OLD.status = 'posted' THEN
            RAISE EXCEPTION 'Posted journal entries cannot be deleted';
        END IF;
        RETURN OLD;
    END IF;

    IF OLD.status = 'posted' THEN
        RAISE EXCEPTION 'Posted journal entries cannot be changed';
    END IF;

    -- Enforce period rules even if a caller attempts to update status directly.
    IF NEW.status = 'posted' THEN
        IF OLD.status <> 'draft' THEN
            RAISE EXCEPTION 'Only draft journal entries can be posted';
        END IF;
        SELECT * INTO v_period
        FROM erp.fiscal_periods
        WHERE company_id = NEW.company_id AND id = NEW.fiscal_period_id
        FOR UPDATE;
        IF NOT FOUND OR v_period.state <> 'open' THEN
            RAISE EXCEPTION 'Fiscal period is missing or not open';
        END IF;
        IF NEW.entry_date < v_period.starts_on OR NEW.entry_date > v_period.ends_on THEN
            RAISE EXCEPTION 'Entry date is outside the selected fiscal period';
        END IF;
        IF NEW.entry_number IS NULL OR length(btrim(NEW.entry_number)) = 0 THEN
            RAISE EXCEPTION 'A journal entry number is required before posting';
        END IF;
        IF NEW.posted_at IS NULL THEN
            NEW.posted_at := clock_timestamp();
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_journal_entry_immutable
    BEFORE INSERT OR UPDATE OR DELETE ON erp.journal_entries
    FOR EACH ROW EXECUTE FUNCTION erp.guard_posted_journal_entry();

-- Lock the parent while a draft line changes; posting locks that same row.
CREATE OR REPLACE FUNCTION erp.guard_journal_line_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    v_company_id uuid;
    v_entry_id uuid;
    v_status text;
BEGIN
    IF TG_OP = 'UPDATE' THEN
        IF NEW.company_id IS DISTINCT FROM OLD.company_id
           OR NEW.journal_entry_id IS DISTINCT FROM OLD.journal_entry_id THEN
            RAISE EXCEPTION 'A journal line cannot be moved to another entry';
        END IF;
        v_company_id := OLD.company_id;
        v_entry_id := OLD.journal_entry_id;
    ELSIF TG_OP = 'DELETE' THEN
        v_company_id := OLD.company_id;
        v_entry_id := OLD.journal_entry_id;
    ELSE
        v_company_id := NEW.company_id;
        v_entry_id := NEW.journal_entry_id;
    END IF;

    SELECT status INTO v_status
    FROM erp.journal_entries
    WHERE company_id = v_company_id AND id = v_entry_id
    FOR UPDATE;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'Journal entry not found';
    END IF;
    IF v_status <> 'draft' THEN
        RAISE EXCEPTION 'Journal lines can only change while the entry is draft';
    END IF;

    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_journal_lines_draft_only
    BEFORE INSERT OR UPDATE OR DELETE ON erp.journal_lines
    FOR EACH ROW EXECUTE FUNCTION erp.guard_journal_line_mutation();

CREATE OR REPLACE FUNCTION erp.guard_journal_dimension_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    v_company_id uuid;
    v_line_id uuid;
    v_entry_status text;
BEGIN
    IF TG_OP = 'UPDATE' THEN
        IF NEW.company_id IS DISTINCT FROM OLD.company_id
           OR NEW.journal_line_id IS DISTINCT FROM OLD.journal_line_id
           OR NEW.dimension_type_id IS DISTINCT FROM OLD.dimension_type_id THEN
            RAISE EXCEPTION 'A journal dimension cannot be moved to another line or type';
        END IF;
        v_company_id := OLD.company_id;
        v_line_id := OLD.journal_line_id;
    ELSIF TG_OP = 'DELETE' THEN
        v_company_id := OLD.company_id;
        v_line_id := OLD.journal_line_id;
    ELSE
        v_company_id := NEW.company_id;
        v_line_id := NEW.journal_line_id;
    END IF;

    SELECT e.status INTO v_entry_status
    FROM erp.journal_lines l
    JOIN erp.journal_entries e
      ON e.company_id = l.company_id AND e.id = l.journal_entry_id
    WHERE l.company_id = v_company_id AND l.id = v_line_id
    FOR UPDATE OF e;

    IF v_entry_status IS NULL THEN
        RAISE EXCEPTION 'Journal line not found';
    END IF;
    IF v_entry_status <> 'draft' THEN
        RAISE EXCEPTION 'Journal dimensions can only change while the entry is draft';
    END IF;

    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_journal_dimensions_draft_only
    BEFORE INSERT OR UPDATE OR DELETE ON erp.journal_line_dimensions
    FOR EACH ROW EXECUTE FUNCTION erp.guard_journal_dimension_mutation();

CREATE OR REPLACE FUNCTION erp.assert_journal_entry_balanced(
    p_company_id uuid,
    p_entry_id uuid
)
RETURNS void
LANGUAGE plpgsql
AS $$
DECLARE
    v_line_count bigint;
    v_debits numeric(30, 6);
    v_credits numeric(30, 6);
BEGIN
    SELECT count(*), coalesce(sum(debit_amount), 0), coalesce(sum(credit_amount), 0)
      INTO v_line_count, v_debits, v_credits
    FROM erp.journal_lines
    WHERE company_id = p_company_id AND journal_entry_id = p_entry_id;

    IF v_line_count < 2 THEN
        RAISE EXCEPTION 'A posted journal entry requires at least two lines';
    END IF;
    IF v_debits <> v_credits THEN
        RAISE EXCEPTION 'Journal entry is out of balance: debits %, credits %',
            v_debits, v_credits;
    END IF;

    IF EXISTS (
        SELECT 1
        FROM erp.journal_lines l
        JOIN erp.accounts a
          ON a.company_id = l.company_id AND a.id = l.account_id
        WHERE l.company_id = p_company_id
          AND l.journal_entry_id = p_entry_id
          AND (NOT a.is_active OR NOT a.allow_posting)
    ) THEN
        RAISE EXCEPTION 'A journal line uses an inactive or non-postable account';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM erp.journal_lines l
        JOIN erp.accounts a
          ON a.company_id = l.company_id AND a.id = l.account_id
        WHERE l.company_id = p_company_id
          AND l.journal_entry_id = p_entry_id
          AND a.account_type IN ('receivable', 'payable')
          AND l.business_partner_id IS NULL
    ) THEN
        RAISE EXCEPTION 'Receivable and payable lines require a business partner';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM erp.journal_lines l
        JOIN erp.dimension_types d
          ON d.company_id = l.company_id AND d.is_required AND d.is_active
        LEFT JOIN erp.journal_line_dimensions ld
          ON ld.company_id = l.company_id
         AND ld.journal_line_id = l.id
         AND ld.dimension_type_id = d.id
        WHERE l.company_id = p_company_id
          AND l.journal_entry_id = p_entry_id
          AND ld.dimension_value_id IS NULL
    ) THEN
        RAISE EXCEPTION 'A required accounting dimension is missing from a journal line';
    END IF;
END;
$$;

CREATE OR REPLACE FUNCTION erp.check_posted_journal_entry()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.status = 'posted' THEN
        PERFORM erp.assert_journal_entry_balanced(NEW.company_id, NEW.id);
    END IF;
    RETURN NULL;
END;
$$;

CREATE CONSTRAINT TRIGGER trg_posted_journal_balanced
    AFTER INSERT OR UPDATE ON erp.journal_entries
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION erp.check_posted_journal_entry();

-- Use this inside the same transaction that posts the source business document.
CREATE OR REPLACE FUNCTION erp.post_journal_entry(
    p_company_id uuid,
    p_entry_id uuid
)
RETURNS void
LANGUAGE plpgsql
AS $$
DECLARE
    v_entry erp.journal_entries%ROWTYPE;
    v_period erp.fiscal_periods%ROWTYPE;
BEGIN
    SELECT * INTO v_entry
    FROM erp.journal_entries
    WHERE company_id = p_company_id AND id = p_entry_id
    FOR UPDATE;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'Journal entry not found';
    END IF;
    IF v_entry.status <> 'draft' THEN
        RAISE EXCEPTION 'Only draft journal entries can be posted';
    END IF;

    SELECT * INTO v_period
    FROM erp.fiscal_periods
    WHERE company_id = p_company_id AND id = v_entry.fiscal_period_id
    FOR UPDATE;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'Fiscal period not found';
    END IF;
    IF v_period.state <> 'open' THEN
        RAISE EXCEPTION 'Fiscal period is not open';
    END IF;
    IF v_entry.entry_date < v_period.starts_on OR v_entry.entry_date > v_period.ends_on THEN
        RAISE EXCEPTION 'Entry date is outside the selected fiscal period';
    END IF;
    IF v_entry.entry_number IS NULL OR length(btrim(v_entry.entry_number)) = 0 THEN
        RAISE EXCEPTION 'A journal entry number is required before posting';
    END IF;

    PERFORM erp.assert_journal_entry_balanced(p_company_id, p_entry_id);

    UPDATE erp.journal_entries
       SET status = 'posted',
           posted_at = clock_timestamp()
     WHERE company_id = p_company_id AND id = p_entry_id;
END;
$$;

-- Posted invoices and bills are immutable; issue a credit note/reversal to correct them.
CREATE OR REPLACE FUNCTION erp.guard_posted_document_header()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.status <> 'draft' THEN
            RAISE EXCEPTION 'Business documents must be created as draft';
        END IF;
        RETURN NEW;
    END IF;
    IF TG_OP = 'DELETE' THEN
        IF OLD.status = 'posted' THEN
            RAISE EXCEPTION 'Posted business documents cannot be deleted';
        END IF;
        RETURN OLD;
    END IF;
    IF OLD.status = 'posted' THEN
        RAISE EXCEPTION 'Posted business documents cannot be changed';
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION erp.check_posted_source_document()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    v_entry_status text;
    v_source_type text;
    v_source_id uuid;
    v_expected_source text;
    v_line_count bigint;
    v_tax_total numeric(20, 6);
BEGIN
    IF NEW.status <> 'posted' THEN
        RETURN NULL;
    END IF;
    IF NEW.journal_entry_id IS NULL THEN
        RAISE EXCEPTION 'A posted source document must link to a posted journal entry';
    END IF;

    v_expected_source := CASE TG_TABLE_NAME
        WHEN 'sales_invoices' THEN 'sales_invoice'
        WHEN 'purchase_bills' THEN 'purchase_bill'
        WHEN 'payments' THEN 'payment'
    END;

    SELECT status, source_type, source_id
      INTO v_entry_status, v_source_type, v_source_id
    FROM erp.journal_entries
    WHERE company_id = NEW.company_id AND id = NEW.journal_entry_id;

    IF v_entry_status IS DISTINCT FROM 'posted'
       OR v_source_type IS DISTINCT FROM v_expected_source
       OR v_source_id IS DISTINCT FROM NEW.id THEN
        RAISE EXCEPTION 'Source document and posted journal entry do not match';
    END IF;

    IF TG_TABLE_NAME = 'sales_invoices' THEN
        SELECT count(*) INTO v_line_count
        FROM erp.sales_invoice_lines
        WHERE company_id = NEW.company_id AND sales_invoice_id = NEW.id;
        IF v_line_count = 0 THEN
            RAISE EXCEPTION 'A posted sales invoice must have at least one line';
        END IF;
        IF EXISTS (
            SELECT 1
              FROM erp.sales_invoice_lines l
              LEFT JOIN erp.tax_codes tc
                ON tc.company_id = l.company_id AND tc.id = l.tax_code_id
              LEFT JOIN LATERAL (
                    SELECT coalesce(sum(c.tax_amount), 0) AS amount
                    FROM erp.sales_invoice_tax_components c
                    WHERE c.company_id = l.company_id
                      AND c.sales_invoice_id = l.sales_invoice_id
                      AND c.sales_invoice_line_id = l.id
              ) taxes ON true
             WHERE l.company_id = NEW.company_id
               AND l.sales_invoice_id = NEW.id
               AND (
                    taxes.amount <> l.tax_amount
                    OR (l.tax_amount > 0 AND l.tax_code_id IS NULL)
                    OR (l.tax_code_id IS NOT NULL AND
                        (NEW.tax_jurisdiction_id IS NULL
                         OR tc.tax_jurisdiction_id <> NEW.tax_jurisdiction_id
                         OR tc.tax_scope NOT IN ('sales', 'both')))
               )
        ) THEN
            RAISE EXCEPTION 'Sales invoice tax components must match line tax totals and document jurisdiction';
        END IF;
        IF EXISTS (
            SELECT 1
              FROM erp.sales_invoice_tax_components c
              JOIN erp.tax_rate_versions v
                ON v.jurisdiction_id = c.tax_jurisdiction_id
               AND v.rate_schedule_id = c.rate_schedule_id
               AND v.id = c.tax_rate_version_id
              JOIN erp.tax_rate_schedules rs
                ON rs.jurisdiction_id = c.tax_jurisdiction_id AND rs.id = c.rate_schedule_id
              JOIN erp.tax_types tt
                ON tt.jurisdiction_id = rs.jurisdiction_id AND tt.id = rs.tax_type_id
             WHERE c.company_id = NEW.company_id
               AND c.sales_invoice_id = NEW.id
               AND (tt.calculation_stage NOT IN ('line', 'document')
                    OR tt.tax_direction NOT IN ('output', 'bidirectional')
                    OR NEW.tax_point_date < v.valid_from OR
                    (v.valid_to IS NOT NULL AND NEW.tax_point_date >= v.valid_to))
        ) THEN
            RAISE EXCEPTION 'Sales invoice contains a tax rate version outside its effective period';
        END IF;
    ELSIF TG_TABLE_NAME = 'purchase_bills' THEN
        SELECT count(*) INTO v_line_count
        FROM erp.purchase_bill_lines
        WHERE company_id = NEW.company_id AND purchase_bill_id = NEW.id;
        IF v_line_count = 0 THEN
            RAISE EXCEPTION 'A posted purchase bill must have at least one line';
        END IF;
        IF EXISTS (
            SELECT 1
              FROM erp.purchase_bill_lines l
              LEFT JOIN erp.tax_codes tc
                ON tc.company_id = l.company_id AND tc.id = l.tax_code_id
              LEFT JOIN LATERAL (
                    SELECT coalesce(sum(c.tax_amount), 0) AS amount
                    FROM erp.purchase_bill_tax_components c
                    WHERE c.company_id = l.company_id
                      AND c.purchase_bill_id = l.purchase_bill_id
                      AND c.purchase_bill_line_id = l.id
              ) taxes ON true
             WHERE l.company_id = NEW.company_id
               AND l.purchase_bill_id = NEW.id
               AND (
                    taxes.amount <> l.tax_amount
                    OR (l.tax_amount > 0 AND l.tax_code_id IS NULL)
                    OR (l.tax_code_id IS NOT NULL AND
                        (NEW.tax_jurisdiction_id IS NULL
                         OR tc.tax_jurisdiction_id <> NEW.tax_jurisdiction_id
                         OR tc.tax_scope NOT IN ('purchase', 'both')))
               )
        ) THEN
            RAISE EXCEPTION 'Purchase bill tax components must match line tax totals and document jurisdiction';
        END IF;
        IF EXISTS (
            SELECT 1
              FROM erp.purchase_bill_tax_components c
              JOIN erp.tax_rate_versions v
                ON v.jurisdiction_id = c.tax_jurisdiction_id
               AND v.rate_schedule_id = c.rate_schedule_id
               AND v.id = c.tax_rate_version_id
              JOIN erp.tax_rate_schedules rs
                ON rs.jurisdiction_id = c.tax_jurisdiction_id AND rs.id = c.rate_schedule_id
              JOIN erp.tax_types tt
                ON tt.jurisdiction_id = rs.jurisdiction_id AND tt.id = rs.tax_type_id
             WHERE c.company_id = NEW.company_id
               AND c.purchase_bill_id = NEW.id
               AND (tt.calculation_stage NOT IN ('line', 'document')
                    OR tt.tax_direction NOT IN ('input', 'bidirectional')
                    OR NEW.tax_point_date < v.valid_from OR
                    (v.valid_to IS NOT NULL AND NEW.tax_point_date >= v.valid_to))
        ) THEN
            RAISE EXCEPTION 'Purchase bill contains a tax rate version outside its effective period';
        END IF;
    ELSIF TG_TABLE_NAME = 'payments' THEN
        SELECT coalesce(sum(c.tax_amount), 0) INTO v_tax_total
          FROM erp.payment_tax_components c
         WHERE c.company_id = NEW.company_id AND c.payment_id = NEW.id;
        IF v_tax_total <> NEW.withholding_total THEN
            RAISE EXCEPTION 'Payment withholding components must equal the payment withholding total';
        END IF;
        IF EXISTS (
            SELECT 1
              FROM erp.payment_tax_components c
              JOIN erp.tax_codes tc
                ON tc.company_id = c.company_id AND tc.id = c.tax_code_id
              JOIN erp.tax_rate_versions v
                ON v.jurisdiction_id = c.tax_jurisdiction_id
               AND v.rate_schedule_id = c.rate_schedule_id
               AND v.id = c.tax_rate_version_id
              JOIN erp.tax_rate_schedules rs
                ON rs.jurisdiction_id = c.tax_jurisdiction_id AND rs.id = c.rate_schedule_id
              JOIN erp.tax_types tt
                ON tt.jurisdiction_id = rs.jurisdiction_id AND tt.id = rs.tax_type_id
             WHERE c.company_id = NEW.company_id
               AND c.payment_id = NEW.id
               AND (tc.tax_scope <> 'withholding'
                    OR tt.calculation_stage <> 'payment'
                    OR tt.tax_direction NOT IN ('withheld', 'bidirectional')
                    OR NEW.payment_date < v.valid_from
                    OR (v.valid_to IS NOT NULL AND NEW.payment_date >= v.valid_to))
        ) THEN
            RAISE EXCEPTION 'Payment withholding uses an invalid tax code or expired tax rate version';
        END IF;
    END IF;
    RETURN NULL;
END;
$$;

CREATE TRIGGER trg_sales_invoice_immutable
    BEFORE INSERT OR UPDATE OR DELETE ON erp.sales_invoices
    FOR EACH ROW EXECUTE FUNCTION erp.guard_posted_document_header();
CREATE TRIGGER trg_purchase_bill_immutable
    BEFORE INSERT OR UPDATE OR DELETE ON erp.purchase_bills
    FOR EACH ROW EXECUTE FUNCTION erp.guard_posted_document_header();
CREATE TRIGGER trg_payment_immutable
    BEFORE INSERT OR UPDATE OR DELETE ON erp.payments
    FOR EACH ROW EXECUTE FUNCTION erp.guard_posted_document_header();

CREATE CONSTRAINT TRIGGER trg_sales_invoice_posted_link
    AFTER INSERT OR UPDATE ON erp.sales_invoices
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION erp.check_posted_source_document();
CREATE CONSTRAINT TRIGGER trg_purchase_bill_posted_link
    AFTER INSERT OR UPDATE ON erp.purchase_bills
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION erp.check_posted_source_document();
CREATE CONSTRAINT TRIGGER trg_payment_posted_link
    AFTER INSERT OR UPDATE ON erp.payments
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION erp.check_posted_source_document();

CREATE OR REPLACE FUNCTION erp.guard_document_line_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    v_company_id uuid;
    v_parent_id uuid;
    v_status text;
BEGIN
    IF TG_OP = 'UPDATE' THEN
        IF NEW.company_id IS DISTINCT FROM OLD.company_id
           OR (to_jsonb(NEW)->>TG_ARGV[1]) IS DISTINCT FROM (to_jsonb(OLD)->>TG_ARGV[1]) THEN
            RAISE EXCEPTION 'A document line cannot be moved to another document';
        END IF;
        v_company_id := OLD.company_id;
        v_parent_id := (to_jsonb(OLD)->>TG_ARGV[1])::uuid;
    ELSIF TG_OP = 'DELETE' THEN
        v_company_id := OLD.company_id;
        v_parent_id := (to_jsonb(OLD)->>TG_ARGV[1])::uuid;
    ELSE
        v_company_id := NEW.company_id;
        v_parent_id := (to_jsonb(NEW)->>TG_ARGV[1])::uuid;
    END IF;

    EXECUTE format(
        'SELECT status FROM erp.%I WHERE company_id = $1 AND id = $2 FOR UPDATE',
        TG_ARGV[0]
    ) INTO v_status USING v_company_id, v_parent_id;

    IF v_status IS NULL THEN
        RAISE EXCEPTION 'Parent document not found';
    END IF;
    IF v_status <> 'draft' THEN
        RAISE EXCEPTION 'Document lines can only change while the document is draft';
    END IF;

    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_sales_invoice_lines_draft_only
    BEFORE INSERT OR UPDATE OR DELETE ON erp.sales_invoice_lines
    FOR EACH ROW EXECUTE FUNCTION erp.guard_document_line_mutation('sales_invoices', 'sales_invoice_id');
CREATE TRIGGER trg_purchase_bill_lines_draft_only
    BEFORE INSERT OR UPDATE OR DELETE ON erp.purchase_bill_lines
    FOR EACH ROW EXECUTE FUNCTION erp.guard_document_line_mutation('purchase_bills', 'purchase_bill_id');
CREATE TRIGGER trg_sales_invoice_addresses_draft_only
    BEFORE INSERT OR UPDATE OR DELETE ON erp.sales_invoice_address_snapshots
    FOR EACH ROW EXECUTE FUNCTION erp.guard_document_line_mutation('sales_invoices', 'sales_invoice_id');
CREATE TRIGGER trg_purchase_bill_addresses_draft_only
    BEFORE INSERT OR UPDATE OR DELETE ON erp.purchase_bill_address_snapshots
    FOR EACH ROW EXECUTE FUNCTION erp.guard_document_line_mutation('purchase_bills', 'purchase_bill_id');
CREATE TRIGGER trg_tax_return_lines_draft_only
    BEFORE INSERT OR UPDATE OR DELETE ON erp.tax_return_lines
    FOR EACH ROW EXECUTE FUNCTION erp.guard_document_line_mutation('tax_returns', 'tax_return_id');
CREATE TRIGGER trg_tax_assessment_lines_draft_only
    BEFORE INSERT OR UPDATE OR DELETE ON erp.tax_assessment_lines
    FOR EACH ROW EXECUTE FUNCTION erp.guard_document_line_mutation('tax_assessments', 'tax_assessment_id');
CREATE TRIGGER trg_tax_assessments_immutable_when_posted
    BEFORE INSERT OR UPDATE OR DELETE ON erp.tax_assessments
    FOR EACH ROW EXECUTE FUNCTION erp.guard_posted_document_header();
CREATE TRIGGER trg_sales_invoice_tax_components_draft_only
    BEFORE INSERT OR UPDATE OR DELETE ON erp.sales_invoice_tax_components
    FOR EACH ROW EXECUTE FUNCTION erp.guard_document_line_mutation('sales_invoices', 'sales_invoice_id');
CREATE TRIGGER trg_purchase_bill_tax_components_draft_only
    BEFORE INSERT OR UPDATE OR DELETE ON erp.purchase_bill_tax_components
    FOR EACH ROW EXECUTE FUNCTION erp.guard_document_line_mutation('purchase_bills', 'purchase_bill_id');
CREATE TRIGGER trg_payment_tax_components_draft_only
    BEFORE INSERT OR UPDATE OR DELETE ON erp.payment_tax_components
    FOR EACH ROW EXECUTE FUNCTION erp.guard_document_line_mutation('payments', 'payment_id');

CREATE OR REPLACE FUNCTION erp.reject_stock_movement_change()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'Stock movements are append-only; post a reversing movement instead';
END;
$$;

CREATE TRIGGER trg_stock_movements_append_only
    BEFORE UPDATE OR DELETE ON erp.stock_movements
    FOR EACH ROW EXECUTE FUNCTION erp.reject_stock_movement_change();

-- Live views. Use WITH (security_invoker = true) so caller RLS applies to base tables.
CREATE VIEW erp.v_period_account_activity WITH (security_invoker = true) AS
SELECT
    e.company_id,
    e.fiscal_period_id,
    p.code AS fiscal_period_code,
    a.id AS account_id,
    a.code AS account_code,
    a.name AS account_name,
    a.account_type,
    sum(l.debit_amount) AS debits,
    sum(l.credit_amount) AS credits,
    sum(l.debit_amount - l.credit_amount) AS net_debit
FROM erp.journal_entries e
JOIN erp.journal_lines l
  ON l.company_id = e.company_id AND l.journal_entry_id = e.id
JOIN erp.accounts a
  ON a.company_id = l.company_id AND a.id = l.account_id
JOIN erp.fiscal_periods p
  ON p.company_id = e.company_id AND p.id = e.fiscal_period_id
WHERE e.status = 'posted'
GROUP BY e.company_id, e.fiscal_period_id, p.code, a.id, a.code, a.name, a.account_type;

CREATE VIEW erp.v_open_ar_invoices WITH (security_invoker = true) AS
WITH document_totals AS (
    SELECT l.company_id, l.sales_invoice_id, sum(l.gross_amount) AS total_amount
    FROM erp.sales_invoice_lines l
    JOIN erp.sales_invoices i
      ON i.company_id = l.company_id AND i.id = l.sales_invoice_id
    WHERE i.status = 'posted'
    GROUP BY l.company_id, l.sales_invoice_id
),
allocations AS (
    SELECT a.company_id, a.sales_invoice_id,
           sum(a.applied_document_amount) AS applied_amount
    FROM erp.ar_receipt_allocations a
    JOIN erp.payments p
      ON p.company_id = a.company_id AND p.id = a.payment_id
    WHERE p.status = 'posted' AND p.direction = 'receipt'
    GROUP BY a.company_id, a.sales_invoice_id
)
SELECT
    i.company_id,
    i.id AS sales_invoice_id,
    i.invoice_no,
    i.document_kind,
    i.partner_id,
    i.issue_date,
    i.due_date,
    i.currency_code,
    coalesce(t.total_amount, 0) AS document_total,
    coalesce(a.applied_amount, 0) AS applied_amount,
    CASE WHEN i.document_kind = 'invoice' THEN 1 ELSE -1 END
        * greatest(coalesce(t.total_amount, 0) - coalesce(a.applied_amount, 0), 0)
        AS open_amount
FROM erp.sales_invoices i
LEFT JOIN document_totals t
  ON t.company_id = i.company_id AND t.sales_invoice_id = i.id
LEFT JOIN allocations a
  ON a.company_id = i.company_id AND a.sales_invoice_id = i.id
WHERE i.status = 'posted';

CREATE VIEW erp.v_open_ap_bills WITH (security_invoker = true) AS
WITH document_totals AS (
    SELECT l.company_id, l.purchase_bill_id, sum(l.gross_amount) AS total_amount
    FROM erp.purchase_bill_lines l
    JOIN erp.purchase_bills b
      ON b.company_id = l.company_id AND b.id = l.purchase_bill_id
    WHERE b.status = 'posted'
    GROUP BY l.company_id, l.purchase_bill_id
),
allocations AS (
    SELECT a.company_id, a.purchase_bill_id,
           sum(a.applied_document_amount) AS applied_amount
    FROM erp.ap_disbursement_allocations a
    JOIN erp.payments p
      ON p.company_id = a.company_id AND p.id = a.payment_id
    WHERE p.status = 'posted' AND p.direction = 'disbursement'
    GROUP BY a.company_id, a.purchase_bill_id
)
SELECT
    b.company_id,
    b.id AS purchase_bill_id,
    b.bill_no,
    b.document_kind,
    b.supplier_id,
    b.bill_date,
    b.due_date,
    b.currency_code,
    coalesce(t.total_amount, 0) AS document_total,
    coalesce(a.applied_amount, 0) AS applied_amount,
    CASE WHEN b.document_kind = 'bill' THEN 1 ELSE -1 END
        * greatest(coalesce(t.total_amount, 0) - coalesce(a.applied_amount, 0), 0)
        AS open_amount
FROM erp.purchase_bills b
LEFT JOIN document_totals t
  ON t.company_id = b.company_id AND t.purchase_bill_id = b.id
LEFT JOIN allocations a
  ON a.company_id = b.company_id AND a.purchase_bill_id = b.id
WHERE b.status = 'posted';

CREATE VIEW erp.v_inventory_on_hand WITH (security_invoker = true) AS
WITH on_hand AS (
    SELECT company_id, warehouse_id, item_id, lot_id,
           sum(quantity_delta) AS on_hand_quantity
    FROM erp.stock_movements
    GROUP BY company_id, warehouse_id, item_id, lot_id
),
reserved AS (
    SELECT company_id, warehouse_id, item_id, lot_id,
           sum(quantity) AS reserved_quantity
    FROM erp.inventory_reservations
    WHERE status = 'active'
    GROUP BY company_id, warehouse_id, item_id, lot_id
)
SELECT
    coalesce(h.company_id, r.company_id) AS company_id,
    coalesce(h.warehouse_id, r.warehouse_id) AS warehouse_id,
    coalesce(h.item_id, r.item_id) AS item_id,
    coalesce(h.lot_id, r.lot_id) AS lot_id,
    coalesce(h.on_hand_quantity, 0) AS on_hand_quantity,
    coalesce(r.reserved_quantity, 0) AS reserved_quantity,
    coalesce(h.on_hand_quantity, 0) - coalesce(r.reserved_quantity, 0) AS available_quantity
FROM on_hand h
FULL JOIN reserved r
  ON r.company_id = h.company_id
 AND r.warehouse_id = h.warehouse_id
 AND r.item_id = h.item_id
 AND r.lot_id IS NOT DISTINCT FROM h.lot_id;

CREATE VIEW erp.v_inventory_valuation WITH (security_invoker = true) AS
SELECT
    company_id,
    warehouse_id,
    item_id,
    lot_id,
    sum(quantity_delta) AS on_hand_quantity,
    sum(value_delta_company) AS carrying_value_company_currency
FROM erp.stock_movements
GROUP BY company_id, warehouse_id, item_id, lot_id;

CREATE VIEW erp.v_sales_by_channel WITH (security_invoker = true) AS
SELECT
    e.company_id,
    e.fiscal_period_id,
    l.sales_channel_id,
    c.code AS channel_code,
    c.name AS channel_name,
    sum(l.credit_amount - l.debit_amount) AS net_revenue
FROM erp.journal_entries e
JOIN erp.journal_lines l
  ON l.company_id = e.company_id AND l.journal_entry_id = e.id
JOIN erp.accounts a
  ON a.company_id = l.company_id AND a.id = l.account_id
JOIN erp.sales_channels c
  ON c.company_id = l.company_id AND c.id = l.sales_channel_id
WHERE e.status = 'posted'
  AND a.account_type = 'revenue'
GROUP BY e.company_id, e.fiscal_period_id, l.sales_channel_id, c.code, c.name;

-- Jurisdiction-facing tax detail comes from the immutable tax component
-- snapshots. Adapters map these normalized facts into statutory return boxes.
CREATE VIEW erp.v_tax_transaction_components WITH (security_invoker = true) AS
SELECT
    'sales_invoice'::text AS source_kind,
    i.company_id,
    i.id AS source_document_id,
    i.invoice_no AS source_document_number,
    i.tax_point_date AS tax_date,
    i.partner_id,
    l.line_no,
    c.tax_jurisdiction_id,
    j.country_code,
    j.jurisdiction_code,
    j.name AS jurisdiction_name,
    tt.tax_family,
    tt.code AS tax_type_code,
    rs.schedule_code,
    c.taxable_base_amount,
    c.tax_amount,
    0::numeric(20, 6) AS recoverable_amount,
    NULL::text AS withholding_treatment,
    c.rate_snapshot
FROM erp.sales_invoice_tax_components c
JOIN erp.sales_invoices i
  ON i.company_id = c.company_id AND i.id = c.sales_invoice_id
JOIN erp.sales_invoice_lines l
  ON l.company_id = c.company_id AND l.sales_invoice_id = c.sales_invoice_id
 AND l.id = c.sales_invoice_line_id
JOIN erp.tax_jurisdictions j ON j.id = c.tax_jurisdiction_id
JOIN erp.tax_rate_schedules rs
  ON rs.jurisdiction_id = c.tax_jurisdiction_id AND rs.id = c.rate_schedule_id
JOIN erp.tax_types tt
  ON tt.jurisdiction_id = rs.jurisdiction_id AND tt.id = rs.tax_type_id
WHERE i.status = 'posted'
UNION ALL
SELECT
    'purchase_bill'::text,
    b.company_id,
    b.id,
    b.bill_no,
    b.tax_point_date,
    b.supplier_id,
    l.line_no,
    c.tax_jurisdiction_id,
    j.country_code,
    j.jurisdiction_code,
    j.name,
    tt.tax_family,
    tt.code,
    rs.schedule_code,
    c.taxable_base_amount,
    c.tax_amount,
    c.recoverable_amount,
    NULL::text,
    c.rate_snapshot
FROM erp.purchase_bill_tax_components c
JOIN erp.purchase_bills b
  ON b.company_id = c.company_id AND b.id = c.purchase_bill_id
JOIN erp.purchase_bill_lines l
  ON l.company_id = c.company_id AND l.purchase_bill_id = c.purchase_bill_id
 AND l.id = c.purchase_bill_line_id
JOIN erp.tax_jurisdictions j ON j.id = c.tax_jurisdiction_id
JOIN erp.tax_rate_schedules rs
  ON rs.jurisdiction_id = c.tax_jurisdiction_id AND rs.id = c.rate_schedule_id
JOIN erp.tax_types tt
  ON tt.jurisdiction_id = rs.jurisdiction_id AND tt.id = rs.tax_type_id
WHERE b.status = 'posted'
UNION ALL
SELECT
    'payment_withholding'::text,
    p.company_id,
    p.id,
    p.payment_no,
    p.payment_date,
    p.partner_id,
    NULL::integer,
    c.tax_jurisdiction_id,
    j.country_code,
    j.jurisdiction_code,
    j.name,
    tt.tax_family,
    tt.code,
    rs.schedule_code,
    c.taxable_base_amount,
    c.tax_amount,
    0::numeric(20, 6),
    c.tax_treatment,
    c.rate_snapshot
FROM erp.payment_tax_components c
JOIN erp.payments p
  ON p.company_id = c.company_id AND p.id = c.payment_id
JOIN erp.tax_jurisdictions j ON j.id = c.tax_jurisdiction_id
JOIN erp.tax_rate_schedules rs
  ON rs.jurisdiction_id = c.tax_jurisdiction_id AND rs.id = c.rate_schedule_id
JOIN erp.tax_types tt
  ON tt.jurisdiction_id = rs.jurisdiction_id AND tt.id = rs.tax_type_id
WHERE p.status = 'posted';

CREATE VIEW licensing.v_active_license_entitlements WITH (security_invoker = true) AS
SELECT
    l.tenant_id,
    l.id AS license_id,
    l.product_id,
    t.id AS license_term_id,
    t.starts_at,
    t.expires_at,
    t.support_valid_until,
    t.updates_valid_until,
    t.max_activations_snapshot,
    t.plan_version_id,
    pv.permits_offline_use
FROM licensing.licenses l
JOIN licensing.license_terms t
  ON t.tenant_id = l.tenant_id AND t.license_id = l.id
JOIN licensing.plan_versions pv ON pv.id = t.plan_version_id
WHERE l.status = 'active'
  AND t.starts_at <= statement_timestamp()
  AND (t.expires_at IS NULL OR statement_timestamp() < t.expires_at);

CREATE VIEW licensing.v_active_license_features WITH (security_invoker = true) AS
SELECT
    e.tenant_id,
    e.license_id,
    e.license_term_id,
    f.feature_code,
    f.is_enabled,
    f.limit_value
FROM licensing.v_active_license_entitlements e
JOIN licensing.plan_features f ON f.plan_version_id = e.plan_version_id;

-- Tenant context for shared-database deployments. The application sets this in
-- each transaction only after authenticating and authorizing the tenant.
CREATE OR REPLACE FUNCTION erp.current_tenant_id()
RETURNS uuid
LANGUAGE sql
STABLE
AS $$
    SELECT nullif(current_setting('app.tenant_id', true), '')::uuid
$$;

ALTER TABLE erp.tenants ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_row_scope ON erp.tenants
    USING (id = erp.current_tenant_id())
    WITH CHECK (id = erp.current_tenant_id());

ALTER TABLE erp.companies ENABLE ROW LEVEL SECURITY;
CREATE POLICY company_tenant_scope ON erp.companies
    USING (tenant_id = erp.current_tenant_id())
    WITH CHECK (tenant_id = erp.current_tenant_id());

DO $$
DECLARE
    v_table text;
    v_tables text[] := ARRAY[
        'fiscal_periods', 'accounts', 'journals', 'accounting_posting_rules',
        'document_sequences',
        'dimension_types', 'dimension_values', 'business_partners', 'partner_addresses',
        'company_tax_registrations', 'partner_tax_registrations', 'tax_codes', 'tax_code_components',
        'tax_filing_periods', 'tax_returns', 'tax_return_lines', 'tax_assessments', 'tax_assessment_lines',
        'items', 'item_accounting_profiles', 'sales_channels', 'warehouses',
        'sales_orders', 'sales_order_lines', 'purchase_orders', 'purchase_order_lines',
        'journal_entries', 'journal_lines', 'journal_line_dimensions',
        'sales_invoices', 'sales_invoice_address_snapshots', 'sales_invoice_lines',
        'sales_invoice_tax_components',
        'purchase_bills', 'purchase_bill_address_snapshots', 'purchase_bill_lines',
        'purchase_bill_tax_components',
        'payments', 'payment_tax_components', 'ar_receipt_allocations', 'ap_disbursement_allocations',
        'inventory_lots', 'stock_movements', 'inventory_cost_allocations', 'inventory_reservations',
        'boms', 'bom_lines', 'production_orders', 'production_material_issues',
        'production_outputs', 'integration_event_inbox', 'outbox_events'
    ];
BEGIN
    FOREACH v_table IN ARRAY v_tables LOOP
        EXECUTE format('ALTER TABLE erp.%I ENABLE ROW LEVEL SECURITY', v_table);
        EXECUTE format(
            'CREATE POLICY tenant_company_scope ON erp.%I '
            'USING (EXISTS (SELECT 1 FROM erp.companies c '
            'WHERE c.id = company_id AND c.tenant_id = erp.current_tenant_id())) '
            'WITH CHECK (EXISTS (SELECT 1 FROM erp.companies c '
            'WHERE c.id = company_id AND c.tenant_id = erp.current_tenant_id()))',
            v_table
        );
    END LOOP;
END;
$$;

-- License records are tenant scoped separately from company accounting data.
-- Global product/plan catalogs have no tenant data and should be SELECT-only
-- for runtime roles through explicit GRANTs.
ALTER TABLE licensing.licenses ENABLE ROW LEVEL SECURITY;
CREATE POLICY license_tenant_scope ON licensing.licenses
    USING (tenant_id = erp.current_tenant_id())
    WITH CHECK (tenant_id = erp.current_tenant_id());

DO $$
DECLARE
    v_table text;
    v_tables text[] := ARRAY[
        'license_terms', 'license_activations', 'license_events', 'signed_grants'
    ];
BEGIN
    FOREACH v_table IN ARRAY v_tables LOOP
        EXECUTE format('ALTER TABLE licensing.%I ENABLE ROW LEVEL SECURITY', v_table);
        EXECUTE format(
            'CREATE POLICY license_tenant_scope ON licensing.%I '
            'USING (tenant_id = erp.current_tenant_id()) '
            'WITH CHECK (tenant_id = erp.current_tenant_id())',
            v_table
        );
    END LOOP;
END;
$$;

-- Production app roles should not own these tables or have BYPASSRLS.
-- Example request setup, executed inside a transaction after authorization:
-- SELECT set_config('app.tenant_id', :authenticated_tenant_uuid, true);


-- ============================================================
-- 01-foundation.sql
-- ============================================================
-- Backend extension, version 2. One-time migration from the supplied baseline.
-- Run the full file only on an empty database; see the companion README.
CREATE SCHEMA identity;
CREATE SCHEMA platform;

CREATE TABLE platform.schema_releases (
    version text PRIMARY KEY,
    installed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    description text NOT NULL
);
INSERT INTO platform.schema_releases VALUES
 ('erp-backend-2', clock_timestamp(), 'Django backend persistence and baseline hardening');

CREATE TABLE identity.users (
    id uuid PRIMARY KEY DEFAULT uuidv7(),
    email varchar(254) NOT NULL CHECK (email = lower(btrim(email)) AND position('@' in email) > 1),
    password varchar(128) NOT NULL DEFAULT '!', -- Django encoded password, never plaintext
    first_name varchar(150) NOT NULL DEFAULT '',
    last_name varchar(150) NOT NULL DEFAULT '',
    is_active boolean NOT NULL DEFAULT true,
    is_staff boolean NOT NULL DEFAULT false,
    is_superuser boolean NOT NULL DEFAULT false,
    last_login timestamptz,
    date_joined timestamptz NOT NULL DEFAULT clock_timestamp(),
    auth_revision bigint NOT NULL DEFAULT 1 CHECK (auth_revision > 0),
    UNIQUE (email)
);
CREATE TABLE identity.tenant_memberships (
    id uuid PRIMARY KEY DEFAULT uuidv7(),
    tenant_id uuid NOT NULL REFERENCES erp.tenants(id),
    user_id uuid NOT NULL REFERENCES identity.users(id),
    tenant_role text NOT NULL DEFAULT 'member' CHECK (tenant_role IN ('owner','admin','member')),
    is_active boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (tenant_id,user_id), UNIQUE (tenant_id,id)
);
CREATE INDEX ix_tenant_membership_user ON identity.tenant_memberships(user_id,tenant_id) WHERE is_active;
CREATE TABLE identity.company_memberships (
    id uuid PRIMARY KEY DEFAULT uuidv7(),
    tenant_id uuid NOT NULL,
    company_id uuid NOT NULL,
    user_id uuid NOT NULL,
    is_active boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (company_id,user_id), UNIQUE (company_id,id),
    FOREIGN KEY (tenant_id,company_id) REFERENCES erp.companies(tenant_id,id),
    FOREIGN KEY (tenant_id,user_id) REFERENCES identity.tenant_memberships(tenant_id,user_id)
);
CREATE INDEX ix_company_membership_user ON identity.company_memberships(user_id,company_id) WHERE is_active;
CREATE TABLE identity.permissions (
    code text PRIMARY KEY CHECK (code <> ''),
    description text NOT NULL
);
CREATE TABLE identity.roles (
    id uuid PRIMARY KEY DEFAULT uuidv7(),
    company_id uuid NOT NULL REFERENCES erp.companies(id),
    code text NOT NULL,
    name text NOT NULL,
    UNIQUE (company_id,code), UNIQUE (company_id,id)
);
CREATE TABLE identity.role_permissions (
    company_id uuid NOT NULL,
    role_id uuid NOT NULL,
    permission_code text NOT NULL REFERENCES identity.permissions(code),
    PRIMARY KEY (company_id,role_id,permission_code),
    FOREIGN KEY (company_id,role_id) REFERENCES identity.roles(company_id,id)
);
CREATE TABLE identity.company_role_assignments (
    company_id uuid NOT NULL,
    membership_id uuid NOT NULL,
    role_id uuid NOT NULL,
    PRIMARY KEY (company_id,membership_id,role_id),
    FOREIGN KEY (company_id,membership_id) REFERENCES identity.company_memberships(company_id,id),
    FOREIGN KEY (company_id,role_id) REFERENCES identity.roles(company_id,id)
);
CREATE TABLE identity.service_accounts (
    id uuid PRIMARY KEY DEFAULT uuidv7(),
    tenant_id uuid NOT NULL REFERENCES erp.tenants(id),
    company_id uuid,
    name text NOT NULL,
    credential_hash bytea NOT NULL CHECK (octet_length(credential_hash)=32),
    is_active boolean NOT NULL DEFAULT true,
    expires_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (tenant_id,id), UNIQUE (credential_hash),
    FOREIGN KEY (tenant_id,company_id) REFERENCES erp.companies(tenant_id,id)
);
CREATE TABLE identity.service_account_permissions (
    tenant_id uuid NOT NULL,
    service_account_id uuid NOT NULL,
    permission_code text NOT NULL REFERENCES identity.permissions(code),
    PRIMARY KEY (tenant_id,service_account_id,permission_code),
    FOREIGN KEY (tenant_id,service_account_id) REFERENCES identity.service_accounts(tenant_id,id)
);
CREATE OR REPLACE FUNCTION identity.current_user_id() RETURNS uuid LANGUAGE sql STABLE AS $$
 SELECT nullif(current_setting('app.user_id',true),'')::uuid
$$;
CREATE OR REPLACE FUNCTION identity.current_service_id() RETURNS uuid LANGUAGE sql STABLE AS $$
 SELECT nullif(current_setting('app.service_id',true),'')::uuid
$$;
-- Session context is trusted backend input, not a client-supplied identity.
CREATE FUNCTION identity.has_company_permission(p_company uuid,p_permission text)
RETURNS boolean LANGUAGE sql STABLE AS $$
 SELECT EXISTS (
   SELECT 1 FROM erp.companies c
   JOIN identity.company_memberships cm ON cm.company_id=c.id
   JOIN identity.tenant_memberships tm ON tm.tenant_id=c.tenant_id AND tm.user_id=cm.user_id
   JOIN identity.users u ON u.id=cm.user_id
   WHERE c.id=p_company AND c.tenant_id=erp.current_tenant_id()
     AND cm.user_id=identity.current_user_id() AND identity.current_service_id() IS NULL
     AND cm.is_active AND tm.is_active AND u.is_active
     AND (tm.tenant_role='owner' OR EXISTS (
       SELECT 1 FROM identity.company_role_assignments a
       JOIN identity.role_permissions rp ON rp.company_id=a.company_id AND rp.role_id=a.role_id
       WHERE a.company_id=c.id AND a.membership_id=cm.id AND rp.permission_code=p_permission
     ))
 ) OR EXISTS (
   SELECT 1 FROM identity.service_accounts s
   JOIN identity.service_account_permissions sp ON sp.tenant_id=s.tenant_id AND sp.service_account_id=s.id
   JOIN erp.companies c ON c.tenant_id=s.tenant_id
   WHERE c.id=p_company AND c.tenant_id=erp.current_tenant_id()
     AND s.id=identity.current_service_id() AND identity.current_user_id() IS NULL
     AND s.is_active AND (s.expires_at IS NULL OR statement_timestamp()<s.expires_at)
     AND (s.company_id IS NULL OR s.company_id=c.id) AND sp.permission_code=p_permission
 )
$$;

CREATE TABLE erp.module_definitions (
    code text PRIMARY KEY,
    name text NOT NULL,
    license_feature_code text,
    is_required boolean NOT NULL DEFAULT false,
    release_status text NOT NULL DEFAULT 'available' CHECK (release_status IN ('planned','available','retired'))
);
CREATE TABLE erp.module_dependencies (
    module_code text NOT NULL REFERENCES erp.module_definitions(code),
    required_module_code text NOT NULL REFERENCES erp.module_definitions(code),
    PRIMARY KEY (module_code,required_module_code),
    CHECK (module_code<>required_module_code)
);
INSERT INTO erp.module_definitions VALUES
 ('core','Company administration',NULL,true,'available'),
 ('accounting','Accounting',NULL,true,'available'),
 ('tax_calculation','Tax calculation',NULL,true,'available'),
 ('sales','Sales','module.sales',false,'available'),
 ('purchasing','Purchasing','module.purchasing',false,'available'),
 ('payments','Payments','module.payments',false,'available'),
 ('inventory','Inventory','module.inventory',false,'available'),
 ('manufacturing','Manufacturing','module.manufacturing',false,'available'),
 ('ecommerce','E-commerce','module.ecommerce',false,'available'),
 ('tax_filing','Tax filing','module.tax_filing',false,'available'),
 ('advanced_reporting','Advanced reporting','module.advanced_reporting',false,'available'),
 ('retail_pos','Retail POS','module.retail_pos',false,'planned');
INSERT INTO erp.module_dependencies VALUES
 ('sales','accounting'),('sales','tax_calculation'),
 ('purchasing','accounting'),('purchasing','tax_calculation'),
 ('payments','accounting'),('payments','tax_calculation'),
 ('inventory','accounting'),('manufacturing','inventory'),
 ('manufacturing','accounting'),('ecommerce','sales'),
 ('tax_filing','accounting'),('tax_filing','tax_calculation'),
 ('advanced_reporting','accounting'),('retail_pos','sales'),('retail_pos','payments');

CREATE TABLE erp.company_policy_state (
    company_id uuid PRIMARY KEY REFERENCES erp.companies(id),
    revision bigint NOT NULL DEFAULT 1 CHECK (revision>0),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE erp.company_module_settings (
    company_id uuid NOT NULL REFERENCES erp.companies(id),
    module_code text NOT NULL REFERENCES erp.module_definitions(code),
    mode text NOT NULL DEFAULT 'disabled' CHECK (mode IN ('enabled','read_only','disabled')),
    changed_by uuid REFERENCES identity.users(id),
    changed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (company_id,module_code)
);
CREATE TABLE erp.company_audit_events (
    id uuid PRIMARY KEY DEFAULT uuidv7(),
    tenant_id uuid NOT NULL,
    company_id uuid NOT NULL,
    actor_user_id uuid REFERENCES identity.users(id),
    actor_service_id uuid REFERENCES identity.service_accounts(id),
    action text NOT NULL,
    object_type text NOT NULL,
    object_id text,
    old_data jsonb,
    new_data jsonb,
    reason text,
    request_id text,
    occurred_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK (actor_user_id IS NULL OR actor_service_id IS NULL),
    FOREIGN KEY (tenant_id,company_id) REFERENCES erp.companies(tenant_id,id),
    FOREIGN KEY (tenant_id,actor_service_id) REFERENCES identity.service_accounts(tenant_id,id)
);
CREATE INDEX ix_company_audit_timeline ON erp.company_audit_events(company_id,occurred_at DESC,id DESC);
CREATE FUNCTION erp.reject_audit_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'Audit records are append-only'; END $$;
CREATE TRIGGER trg_audit_append_only BEFORE UPDATE OR DELETE ON erp.company_audit_events
 FOR EACH ROW EXECUTE FUNCTION erp.reject_audit_mutation();

-- Revision may advance by more than one in a multi-row batch; treat it as an opaque ETag.
CREATE FUNCTION erp.guard_module_setting() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE v_company uuid; v_required boolean; v_release text;
BEGIN
 v_company := CASE WHEN TG_OP='DELETE' THEN OLD.company_id ELSE NEW.company_id END;
 PERFORM 1 FROM erp.company_policy_state WHERE company_id=v_company FOR UPDATE;
 IF NOT FOUND THEN RAISE EXCEPTION 'Company policy state is missing'; END IF;
 IF TG_OP='UPDATE' AND (NEW.company_id,NEW.module_code) IS DISTINCT FROM (OLD.company_id,OLD.module_code) THEN
   RAISE EXCEPTION 'Module setting identity is immutable';
 END IF;
 SELECT is_required,release_status INTO v_required,v_release FROM erp.module_definitions
 WHERE code=CASE WHEN TG_OP='DELETE' THEN OLD.module_code ELSE NEW.module_code END;
 IF v_required AND (TG_OP='DELETE' OR NEW.mode<>'enabled') THEN
   RAISE EXCEPTION 'Required modules must remain enabled';
 END IF;
 IF TG_OP<>'DELETE' AND NEW.mode='enabled' AND v_release<>'available' THEN
   RAISE EXCEPTION 'Module is not available in this release';
 END IF;
 UPDATE erp.company_policy_state SET revision=revision+1,updated_at=clock_timestamp() WHERE company_id=v_company;
 INSERT INTO erp.company_audit_events(tenant_id,company_id,actor_user_id,actor_service_id,action,object_type,object_id,old_data,new_data,reason,request_id)
 SELECT c.tenant_id,v_company,identity.current_user_id(),identity.current_service_id(),'module.'||lower(TG_OP),'module',
   CASE WHEN TG_OP='DELETE' THEN OLD.module_code ELSE NEW.module_code END,
   CASE WHEN TG_OP='INSERT' THEN NULL ELSE to_jsonb(OLD) END,
   CASE WHEN TG_OP='DELETE' THEN NULL ELSE to_jsonb(NEW) END,
   nullif(current_setting('app.change_reason',true),''),nullif(current_setting('app.request_id',true),'')
 FROM erp.companies c WHERE c.id=v_company;
 IF TG_OP='DELETE' THEN RETURN OLD; END IF;
 NEW.changed_at:=clock_timestamp(); NEW.changed_by:=identity.current_user_id(); RETURN NEW;
END $$;
CREATE TRIGGER trg_module_setting_guard BEFORE INSERT OR UPDATE OR DELETE ON erp.company_module_settings
 FOR EACH ROW EXECUTE FUNCTION erp.guard_module_setting();
CREATE FUNCTION erp.check_module_dependencies() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE v_company uuid;
BEGIN
 v_company:=CASE WHEN TG_OP='DELETE' THEN OLD.company_id ELSE NEW.company_id END;
 IF EXISTS (
   SELECT 1 FROM erp.company_module_settings s
   JOIN erp.module_dependencies d ON d.module_code=s.module_code
   LEFT JOIN erp.company_module_settings r ON r.company_id=s.company_id AND r.module_code=d.required_module_code
   WHERE s.company_id=v_company AND s.mode='enabled' AND coalesce(r.mode,'disabled')<>'enabled'
 ) THEN RAISE EXCEPTION 'Enabled module has a disabled/read-only dependency'; END IF;
 RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER trg_module_dependencies_valid AFTER INSERT OR UPDATE OR DELETE ON erp.company_module_settings
 DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION erp.check_module_dependencies();
CREATE FUNCTION erp.reject_module_cycle() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF EXISTS (WITH RECURSIVE reach(a,b) AS (
   SELECT module_code,required_module_code FROM erp.module_dependencies
   UNION SELECT r.a,d.required_module_code FROM reach r JOIN erp.module_dependencies d ON d.module_code=r.b
 ) SELECT 1 FROM reach WHERE a=b) THEN RAISE EXCEPTION 'Module dependencies contain a cycle'; END IF;
 RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER trg_module_catalog_acyclic AFTER INSERT OR UPDATE ON erp.module_dependencies
 DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION erp.reject_module_cycle();
CREATE FUNCTION erp.provision_company_policy() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 INSERT INTO erp.company_policy_state(company_id) VALUES(NEW.id);
 INSERT INTO erp.company_module_settings(company_id,module_code,mode)
 SELECT NEW.id,code,CASE WHEN is_required THEN 'enabled' ELSE 'disabled' END FROM erp.module_definitions;
 RETURN NEW;
END $$;
CREATE TRIGGER trg_company_policy_provision AFTER INSERT ON erp.companies
 FOR EACH ROW EXECUTE FUNCTION erp.provision_company_policy();
-- Existing companies start with required modules enabled and optional modules disabled.
INSERT INTO erp.company_policy_state(company_id) SELECT id FROM erp.companies;
INSERT INTO erp.company_module_settings(company_id,module_code,mode)
 SELECT c.id,m.code,CASE WHEN m.is_required THEN 'enabled' ELSE 'disabled' END
 FROM erp.companies c CROSS JOIN erp.module_definitions m;

CREATE FUNCTION erp.bump_company_authorization() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE v_company uuid;
BEGIN
 v_company:=CASE WHEN TG_OP='DELETE' THEN OLD.company_id ELSE NEW.company_id END;
 UPDATE erp.company_policy_state SET revision=revision+1,updated_at=clock_timestamp() WHERE company_id=v_company;
 IF TG_OP='DELETE' THEN RETURN OLD; END IF; RETURN NEW;
END $$;
CREATE TRIGGER trg_membership_policy BEFORE INSERT OR UPDATE OR DELETE ON identity.company_memberships
 FOR EACH ROW EXECUTE FUNCTION erp.bump_company_authorization();
CREATE TRIGGER trg_role_grant_policy BEFORE INSERT OR UPDATE OR DELETE ON identity.role_permissions
 FOR EACH ROW EXECUTE FUNCTION erp.bump_company_authorization();
CREATE TRIGGER trg_role_assignment_policy BEFORE INSERT OR UPDATE OR DELETE ON identity.company_role_assignments
 FOR EACH ROW EXECUTE FUNCTION erp.bump_company_authorization();
CREATE VIEW erp.v_company_module_configuration WITH (security_invoker=true) AS
 SELECT c.tenant_id,c.id AS company_id,p.revision,m.code AS module_code,m.name,m.is_required,m.release_status,
        coalesce(s.mode,'disabled') AS configured_mode,m.license_feature_code
 FROM erp.companies c JOIN erp.company_policy_state p ON p.company_id=c.id
 CROSS JOIN erp.module_definitions m LEFT JOIN erp.company_module_settings s ON s.company_id=c.id AND s.module_code=m.code;

-- ============================================================
-- 02-licensing.sql
-- ============================================================
ALTER TABLE licensing.licenses ADD CONSTRAINT uq_license_tenant_product_id UNIQUE(tenant_id,product_id,id);
ALTER TABLE licensing.licenses ADD COLUMN lock_stub boolean NOT NULL DEFAULT true CHECK(lock_stub);
ALTER TABLE licensing.plan_versions ADD CONSTRAINT ck_fixed_plan_term_complete
 CHECK(term_unit='lifetime' OR (term_count IS NOT NULL AND term_count>0));
ALTER TABLE licensing.license_terms ADD CONSTRAINT ck_fixed_license_term_complete
 CHECK(term_unit_snapshot='lifetime' OR
       (term_count_snapshot IS NOT NULL AND term_count_snapshot>0 AND expires_at IS NOT NULL AND expires_at>starts_at));
CREATE TABLE licensing.tenant_product_bindings (
    tenant_id uuid NOT NULL REFERENCES erp.tenants(id),
    product_id uuid NOT NULL REFERENCES licensing.products(id),
    license_id uuid,
    entitlement_revision bigint NOT NULL DEFAULT 1 CHECK(entitlement_revision>0),
    billing_timezone text NOT NULL DEFAULT 'UTC',
    billing_anchor_day smallint CHECK(billing_anchor_day BETWEEN 1 AND 31),
    month_end_anchor boolean NOT NULL DEFAULT false,
    lock_stub boolean NOT NULL DEFAULT true CHECK(lock_stub),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY(tenant_id,product_id),
    FOREIGN KEY(tenant_id,product_id,license_id) REFERENCES licensing.licenses(tenant_id,product_id,id)
);
-- Do not silently select among multiple licenses. Operator binds explicitly after migration.
INSERT INTO licensing.tenant_product_bindings(tenant_id,product_id)
 SELECT DISTINCT tenant_id,product_id FROM licensing.licenses;
CREATE FUNCTION licensing.ensure_binding_coordinator() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 INSERT INTO licensing.tenant_product_bindings(tenant_id,product_id) VALUES(NEW.tenant_id,NEW.product_id)
 ON CONFLICT DO NOTHING; RETURN NEW;
END $$;
CREATE TRIGGER trg_license_coordinator AFTER INSERT ON licensing.licenses
 FOR EACH ROW EXECUTE FUNCTION licensing.ensure_binding_coordinator();
CREATE FUNCTION licensing.touch_binding() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF (NEW.tenant_id,NEW.product_id) IS DISTINCT FROM (OLD.tenant_id,OLD.product_id) THEN
   RAISE EXCEPTION 'Binding scope is immutable'; END IF;
 NEW.entitlement_revision:=OLD.entitlement_revision+1; NEW.updated_at:=clock_timestamp(); RETURN NEW;
END $$;
CREATE TRIGGER trg_binding_revision BEFORE UPDATE ON licensing.tenant_product_bindings
 FOR EACH ROW EXECUTE FUNCTION licensing.touch_binding();
CREATE FUNCTION licensing.invalidate_entitlement() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE v_product uuid;
BEGIN
 IF TG_TABLE_NAME='licenses' THEN
   IF (NEW.tenant_id,NEW.product_id) IS DISTINCT FROM (OLD.tenant_id,OLD.product_id) THEN
     RAISE EXCEPTION 'License scope is immutable'; END IF;
   IF NEW.status IS NOT DISTINCT FROM OLD.status THEN RETURN NEW; END IF;
   v_product:=NEW.product_id;
 ELSE SELECT product_id INTO v_product FROM licensing.licenses WHERE id=NEW.license_id;
 END IF;
 UPDATE licensing.tenant_product_bindings SET entitlement_revision=entitlement_revision+1
 WHERE tenant_id=NEW.tenant_id AND product_id=v_product;
 RETURN NEW;
END $$;
CREATE TRIGGER trg_license_status_revision AFTER UPDATE ON licensing.licenses
 FOR EACH ROW EXECUTE FUNCTION licensing.invalidate_entitlement();
CREATE TRIGGER trg_license_term_revision AFTER INSERT ON licensing.license_terms
 FOR EACH ROW EXECUTE FUNCTION licensing.invalidate_entitlement();

CREATE OR REPLACE FUNCTION licensing.guard_license_term_insert() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE v_license licensing.licenses%ROWTYPE; v_plan licensing.plan_versions%ROWTYPE;
BEGIN
 SELECT * INTO v_license FROM licensing.licenses WHERE tenant_id=NEW.tenant_id AND id=NEW.license_id;
 IF NOT FOUND THEN RAISE EXCEPTION 'License is missing'; END IF;
 PERFORM 1 FROM licensing.tenant_product_bindings WHERE tenant_id=NEW.tenant_id AND product_id=v_license.product_id FOR UPDATE;
 IF NOT FOUND THEN RAISE EXCEPTION 'Entitlement coordinator is missing'; END IF;
 SELECT * INTO v_license FROM licensing.licenses WHERE tenant_id=NEW.tenant_id AND id=NEW.license_id FOR UPDATE;
 IF v_license.status='revoked' THEN RAISE EXCEPTION 'License is revoked'; END IF;
 SELECT * INTO v_plan FROM licensing.plan_versions WHERE id=NEW.plan_version_id FOR SHARE;
 IF NOT FOUND OR v_plan.published_at IS NULL OR v_plan.product_id<>v_license.product_id
 OR NEW.term_unit_snapshot<>v_plan.term_unit OR NEW.term_count_snapshot IS DISTINCT FROM v_plan.term_count
 OR NEW.max_activations_snapshot<>v_plan.max_activations THEN
   RAISE EXCEPTION 'Term must match a published plan for the same product'; END IF;
 IF EXISTS(SELECT 1 FROM licensing.license_terms t WHERE t.license_id=NEW.license_id
 AND tstzrange(t.starts_at,t.expires_at,'[)') && tstzrange(NEW.starts_at,NEW.expires_at,'[)')) THEN
   RAISE EXCEPTION 'License term overlaps an existing term'; END IF;
 RETURN NEW;
END $$;
CREATE OR REPLACE FUNCTION licensing.guard_license_activation() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE v_license licensing.licenses%ROWTYPE; v_limit integer; v_count bigint; v_now timestamptz;
BEGIN
 IF TG_OP='UPDATE' THEN
   IF (NEW.tenant_id,NEW.license_id,NEW.device_fingerprint_hash,NEW.device_public_key,NEW.activated_at)
   IS DISTINCT FROM (OLD.tenant_id,OLD.license_id,OLD.device_fingerprint_hash,OLD.device_public_key,OLD.activated_at) THEN
     RAISE EXCEPTION 'Activation identity is immutable'; END IF;
   -- Heartbeats do not consume another slot, and cleanup works after expiration/revocation.
   IF NEW.deactivated_at IS NOT NULL OR
      (NEW.deactivated_at IS NOT DISTINCT FROM OLD.deactivated_at) THEN RETURN NEW; END IF;
 END IF;
 SELECT * INTO v_license FROM licensing.licenses WHERE tenant_id=NEW.tenant_id AND id=NEW.license_id;
 IF NOT FOUND THEN RAISE EXCEPTION 'License is missing'; END IF;
 PERFORM 1 FROM licensing.tenant_product_bindings WHERE tenant_id=NEW.tenant_id AND product_id=v_license.product_id FOR UPDATE;
 SELECT * INTO v_license FROM licensing.licenses WHERE id=NEW.license_id AND tenant_id=NEW.tenant_id FOR UPDATE;
 v_now:=clock_timestamp();
 IF v_license.status<>'active' THEN RAISE EXCEPTION 'License is not active'; END IF;
 SELECT max_activations_snapshot INTO v_limit FROM licensing.license_terms
 WHERE license_id=NEW.license_id AND starts_at<=v_now AND (expires_at IS NULL OR v_now<expires_at);
 IF v_limit IS NULL THEN RAISE EXCEPTION 'No valid license term'; END IF;
 SELECT count(*) INTO v_count FROM licensing.license_activations
 WHERE license_id=NEW.license_id AND deactivated_at IS NULL AND id<>NEW.id;
 IF NEW.deactivated_at IS NULL AND v_count>=v_limit THEN RAISE EXCEPTION 'Activation quota exceeded'; END IF;
 RETURN NEW;
END $$;
-- Correct the draft-plan DELETE return path in the baseline trigger.
CREATE OR REPLACE FUNCTION licensing.guard_published_plan_version() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF OLD.published_at IS NOT NULL THEN RAISE EXCEPTION 'Published plans are immutable'; END IF;
 IF TG_OP='DELETE' THEN RETURN OLD; END IF; RETURN NEW;
END $$;
CREATE FUNCTION licensing.validate_signed_grant() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE t licensing.license_terms%ROWTYPE; k licensing.signing_keys%ROWTYPE; v_now timestamptz;
BEGIN
 SELECT * INTO t FROM licensing.license_terms WHERE tenant_id=NEW.tenant_id AND license_id=NEW.license_id AND id=NEW.license_term_id;
 SELECT * INTO k FROM licensing.signing_keys WHERE key_id=NEW.key_id;
 v_now:=clock_timestamp();
 IF t.id IS NULL OR k.key_id IS NULL OR k.revoked_at IS NOT NULL OR NEW.issued_at>v_now
 OR NEW.issued_at<t.starts_at OR NEW.issued_at<k.valid_from OR
 (t.expires_at IS NOT NULL AND NEW.issued_at>=t.expires_at) OR
 (k.valid_to IS NOT NULL AND NEW.issued_at>=k.valid_to) OR
 NEW.grant_valid_until IS NULL OR NEW.grant_valid_until<=NEW.issued_at OR
 (t.expires_at IS NOT NULL AND NEW.grant_valid_until>t.expires_at) OR
 (k.valid_to IS NOT NULL AND NEW.grant_valid_until>k.valid_to) THEN
   RAISE EXCEPTION 'Grant validity exceeds its term or signing key'; END IF;
 IF NOT EXISTS(SELECT 1 FROM licensing.licenses WHERE id=NEW.license_id AND tenant_id=NEW.tenant_id AND status='active') THEN
   RAISE EXCEPTION 'Cannot sign for an inactive license'; END IF;
 IF NEW.activation_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM licensing.license_activations
 WHERE id=NEW.activation_id AND license_id=NEW.license_id AND tenant_id=NEW.tenant_id AND deactivated_at IS NULL) THEN
   RAISE EXCEPTION 'Grant activation is inactive'; END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER trg_grant_validity BEFORE INSERT ON licensing.signed_grants
 FOR EACH ROW EXECUTE FUNCTION licensing.validate_signed_grant();

CREATE TABLE licensing.billing_orders (
    id uuid PRIMARY KEY DEFAULT uuidv7(),
    tenant_id uuid NOT NULL REFERENCES erp.tenants(id),
    license_id uuid NOT NULL,
    plan_version_id uuid NOT NULL REFERENCES licensing.plan_versions(id),
    order_key varchar(128) NOT NULL,
    currency_code char(3) NOT NULL REFERENCES erp.currencies(code),
    amount numeric(20,6) NOT NULL CHECK(amount>=0),
    provider text NOT NULL,
    provider_order_reference text,
    status text NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','paid','fulfilled','cancelled','refunded')),
    fulfilled_term_id uuid,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    fulfilled_at timestamptz,
    UNIQUE(tenant_id,id), UNIQUE(tenant_id,order_key), UNIQUE(provider,provider_order_reference),
    FOREIGN KEY(tenant_id,license_id) REFERENCES licensing.licenses(tenant_id,id),
    FOREIGN KEY(tenant_id,license_id,fulfilled_term_id) REFERENCES licensing.license_terms(tenant_id,license_id,id),
    CHECK(status<>'fulfilled' OR (fulfilled_term_id IS NOT NULL AND fulfilled_at IS NOT NULL))
);
CREATE TABLE licensing.billing_events (
    id uuid PRIMARY KEY DEFAULT uuidv7(),
    tenant_id uuid NOT NULL REFERENCES erp.tenants(id),
    billing_order_id uuid NOT NULL,
    provider text NOT NULL,
    external_event_id text NOT NULL,
    payload_hash bytea NOT NULL CHECK(octet_length(payload_hash)=32),
    event_type text NOT NULL,
    received_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    verified_at timestamptz NOT NULL,
    event_data jsonb NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE(provider,external_event_id),
    FOREIGN KEY(tenant_id,billing_order_id) REFERENCES licensing.billing_orders(tenant_id,id)
);
CREATE TRIGGER trg_billing_event_immutable BEFORE UPDATE OR DELETE ON licensing.billing_events
 FOR EACH ROW EXECUTE FUNCTION licensing.reject_issued_record_change();
CREATE VIEW licensing.v_bound_entitlements WITH(security_invoker=true) AS
 SELECT b.tenant_id,b.product_id,b.license_id,b.entitlement_revision,l.status,
        t.id AS license_term_id,t.plan_version_id,t.starts_at,t.expires_at,
        t.support_valid_until,t.updates_valid_until
 FROM licensing.tenant_product_bindings b JOIN licensing.licenses l ON l.id=b.license_id AND l.tenant_id=b.tenant_id
 JOIN licensing.license_terms t ON t.license_id=l.id AND t.tenant_id=l.tenant_id
 WHERE l.status='active' AND t.starts_at<=statement_timestamp()
 AND (t.expires_at IS NULL OR statement_timestamp()<t.expires_at);

-- Same transaction as each business command; no remote license request is needed.
CREATE FUNCTION erp.assert_company_write(p_company uuid,p_product uuid,p_module text,p_permission text)
RETURNS uuid LANGUAGE plpgsql AS $$
DECLARE v_license uuid; v_plan uuid; v_status text; v_now timestamptz;
BEGIN
 IF NOT EXISTS(SELECT 1 FROM erp.companies WHERE id=p_company AND tenant_id=erp.current_tenant_id() AND is_active) THEN
   RAISE EXCEPTION 'Company is not accessible/active' USING ERRCODE='42501'; END IF;
 SELECT license_id INTO v_license FROM licensing.tenant_product_bindings
 WHERE tenant_id=erp.current_tenant_id() AND product_id=p_product FOR SHARE;
 IF v_license IS NULL THEN RAISE EXCEPTION 'No designated license' USING ERRCODE='42501'; END IF;
 SELECT status INTO v_status FROM licensing.licenses WHERE id=v_license AND tenant_id=erp.current_tenant_id() FOR SHARE;
 PERFORM 1 FROM erp.company_policy_state WHERE company_id=p_company FOR SHARE;
 IF NOT FOUND OR NOT identity.has_company_permission(p_company,p_permission) THEN
   RAISE EXCEPTION 'Company permission denied' USING ERRCODE='42501'; END IF;
 -- The company can have been deactivated while this transaction waited for policy.
 IF NOT EXISTS(SELECT 1 FROM erp.companies WHERE id=p_company AND is_active) THEN
   RAISE EXCEPTION 'Company inactive' USING ERRCODE='42501'; END IF;
 v_now:=clock_timestamp();
 SELECT plan_version_id INTO v_plan FROM licensing.license_terms
 WHERE license_id=v_license AND starts_at<=v_now AND (expires_at IS NULL OR v_now<expires_at);
 IF v_status IS DISTINCT FROM 'active' OR v_plan IS NULL THEN
   RAISE EXCEPTION 'License inactive or expired' USING ERRCODE='42501'; END IF;
 IF NOT EXISTS(SELECT 1 FROM erp.module_definitions WHERE code=p_module) THEN RAISE EXCEPTION 'Unknown module'; END IF;
 IF EXISTS(WITH RECURSIVE required(code) AS (
   SELECT p_module UNION SELECT d.required_module_code FROM required r JOIN erp.module_dependencies d ON d.module_code=r.code
 ) SELECT 1 FROM required r JOIN erp.module_definitions m ON m.code=r.code
 LEFT JOIN erp.company_module_settings s ON s.company_id=p_company AND s.module_code=r.code
 WHERE coalesce(s.mode,'disabled')<>'enabled' OR m.release_status<>'available' OR
 (m.license_feature_code IS NOT NULL AND NOT EXISTS(SELECT 1 FROM licensing.plan_features f
 WHERE f.plan_version_id=v_plan AND f.feature_code=m.license_feature_code AND f.is_enabled))) THEN
   RAISE EXCEPTION 'Module/dependency is unavailable or not entitled' USING ERRCODE='42501'; END IF;
 RETURN v_plan;
END $$;

CREATE FUNCTION erp.set_company_modules(p_company uuid,p_product uuid,p_expected_revision bigint,p_changes jsonb,p_reason text)
RETURNS bigint LANGUAGE plpgsql AS $$
DECLARE v_revision bigint; v_license uuid; v_plan uuid; v_status text; v_now timestamptz;
BEGIN
 IF jsonb_typeof(p_changes) IS DISTINCT FROM 'array' OR jsonb_array_length(p_changes) NOT BETWEEN 1 AND 24 THEN
   RAISE EXCEPTION 'changes must be a bounded nonempty array'; END IF;
 IF nullif(btrim(p_reason),'') IS NULL THEN RAISE EXCEPTION 'Change reason required'; END IF;
 SELECT license_id INTO v_license FROM licensing.tenant_product_bindings
 WHERE tenant_id=erp.current_tenant_id() AND product_id=p_product FOR SHARE;
 IF v_license IS NOT NULL THEN
   SELECT status INTO v_status FROM licensing.licenses WHERE id=v_license AND tenant_id=erp.current_tenant_id() FOR SHARE;
 END IF;
 SELECT revision INTO v_revision FROM erp.company_policy_state WHERE company_id=p_company FOR UPDATE;
 IF NOT FOUND OR NOT identity.has_company_permission(p_company,'company.modules.manage') THEN
   RAISE EXCEPTION 'Company administration denied' USING ERRCODE='42501'; END IF;
 IF p_expected_revision IS NULL OR v_revision<>p_expected_revision THEN RAISE EXCEPTION 'Policy revision conflict' USING ERRCODE='40001'; END IF;
 IF EXISTS(SELECT 1 FROM jsonb_to_recordset(p_changes) AS x(module_code text,mode text)
           GROUP BY module_code HAVING count(*)>1) THEN RAISE EXCEPTION 'Duplicate module in batch'; END IF;
 v_now:=clock_timestamp();
 SELECT plan_version_id INTO v_plan FROM licensing.license_terms WHERE license_id=v_license
 AND starts_at<=v_now AND (expires_at IS NULL OR v_now<expires_at);
 IF EXISTS(SELECT 1 FROM jsonb_to_recordset(p_changes) AS x(module_code text,mode text)
 LEFT JOIN erp.module_definitions m ON m.code=x.module_code
 WHERE m.code IS NULL OR x.mode IS NULL OR x.mode NOT IN ('enabled','read_only','disabled') OR
 (x.mode='enabled' AND (v_status IS DISTINCT FROM 'active' OR v_plan IS NULL OR
  (m.license_feature_code IS NOT NULL AND NOT EXISTS(SELECT 1 FROM licensing.plan_features f
  WHERE f.plan_version_id=v_plan AND f.feature_code=m.license_feature_code AND f.is_enabled))))) THEN
   RAISE EXCEPTION 'Invalid or unlicensed module change' USING ERRCODE='42501'; END IF;
 PERFORM set_config('app.change_reason',p_reason,true);
 INSERT INTO erp.company_module_settings(company_id,module_code,mode)
 SELECT p_company,x.module_code,x.mode FROM jsonb_to_recordset(p_changes) AS x(module_code text,mode text)
 ON CONFLICT(company_id,module_code) DO UPDATE SET mode=excluded.mode;
 IF EXISTS(SELECT 1 FROM erp.company_module_settings s JOIN erp.module_dependencies d ON d.module_code=s.module_code
 LEFT JOIN erp.company_module_settings r ON r.company_id=s.company_id AND r.module_code=d.required_module_code
 WHERE s.company_id=p_company AND s.mode='enabled' AND coalesce(r.mode,'disabled')<>'enabled') THEN
   RAISE EXCEPTION 'Module dependency conflict'; END IF;
 SELECT revision INTO v_revision FROM erp.company_policy_state WHERE company_id=p_company;
 INSERT INTO erp.outbox_events(company_id,event_key,aggregate_type,aggregate_id,event_type,payload)
 VALUES(p_company,'policy:'||v_revision,'company',p_company,'company.modules.changed',
 jsonb_build_object('policy_revision',v_revision,'changes',p_changes));
 RETURN v_revision;
END $$;

-- ============================================================
-- 03-operations.sql
-- ============================================================
CREATE TABLE erp.api_command_receipts (
    id uuid PRIMARY KEY DEFAULT uuidv7(),
    tenant_id uuid NOT NULL REFERENCES erp.tenants(id),
    company_id uuid,
    actor_user_id uuid REFERENCES identity.users(id),
    actor_service_id uuid,
    operation text NOT NULL,
    resource_key text NOT NULL DEFAULT '',
    idempotency_key varchar(128) NOT NULL CHECK(length(btrim(idempotency_key))>0),
    request_hash bytea NOT NULL CHECK(octet_length(request_hash)=32),
    status text NOT NULL DEFAULT 'processing' CHECK(status IN ('processing','completed')),
    response_status smallint CHECK(response_status BETWEEN 200 AND 599),
    result_type text,
    result_id uuid,
    response_body jsonb,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    completed_at timestamptz,
    retain_until timestamptz NOT NULL,
    UNIQUE(tenant_id,id),
    UNIQUE NULLS NOT DISTINCT(tenant_id,company_id,actor_user_id,actor_service_id,operation,resource_key,idempotency_key),
    FOREIGN KEY(tenant_id,company_id) REFERENCES erp.companies(tenant_id,id),
    FOREIGN KEY(tenant_id,actor_service_id) REFERENCES identity.service_accounts(tenant_id,id),
    CHECK(num_nonnulls(actor_user_id,actor_service_id)=1),
    CHECK(status<>'completed' OR (response_status IS NOT NULL AND completed_at IS NOT NULL)),
    CHECK(retain_until>created_at)
);
CREATE INDEX ix_receipts_retention ON erp.api_command_receipts(retain_until);
-- A receipt is intentionally insertable in `processing` state. The command
-- service completes it in the same business transaction, while a sweeper can
-- recover a crashed processing claim after a bounded lease/retention policy.
CREATE FUNCTION erp.guard_command_receipt() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF OLD.status='completed' THEN RAISE EXCEPTION 'Completed command outcome is immutable'; END IF;
 IF (NEW.tenant_id,NEW.company_id,NEW.actor_user_id,NEW.actor_service_id,NEW.operation,NEW.resource_key,NEW.idempotency_key,NEW.request_hash)
 IS DISTINCT FROM (OLD.tenant_id,OLD.company_id,OLD.actor_user_id,OLD.actor_service_id,OLD.operation,OLD.resource_key,OLD.idempotency_key,OLD.request_hash) THEN
   RAISE EXCEPTION 'Command identity/payload cannot change'; END IF; RETURN NEW;
END $$;
CREATE TRIGGER trg_receipt_identity BEFORE UPDATE ON erp.api_command_receipts
 FOR EACH ROW EXECUTE FUNCTION erp.guard_command_receipt();

CREATE TABLE erp.background_jobs (
    id uuid PRIMARY KEY DEFAULT uuidv7(),
    tenant_id uuid NOT NULL REFERENCES erp.tenants(id),
    company_id uuid,
    requester_user_id uuid REFERENCES identity.users(id),
    requester_service_id uuid,
    job_type text NOT NULL,
    queue_name text NOT NULL CHECK(queue_name IN ('integrations','documents','reports','imports','tax_submission','maintenance')),
    job_key varchar(200) NOT NULL,
    parameters jsonb NOT NULL DEFAULT '{}'::jsonb,
    status text NOT NULL DEFAULT 'queued' CHECK(status IN ('queued','running','succeeded','failed','cancel_requested','cancelled')),
    progress numeric(5,2) NOT NULL DEFAULT 0 CHECK(progress BETWEEN 0 AND 100),
    run_after timestamptz NOT NULL DEFAULT clock_timestamp(),
    claim_token uuid,
    claim_expires_at timestamptz,
    heartbeat_at timestamptz,
    attempt_count integer NOT NULL DEFAULT 0 CHECK(attempt_count>=0),
    max_attempts integer NOT NULL DEFAULT 5 CHECK(max_attempts BETWEEN 1 AND 100),
    result_object_key text,
    result_metadata jsonb,
    last_error_code text,
    last_error_message text,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    started_at timestamptz,
    finished_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE(tenant_id,id), UNIQUE(company_id,id),
    UNIQUE NULLS NOT DISTINCT(tenant_id,company_id,job_type,job_key),
    FOREIGN KEY(tenant_id,company_id) REFERENCES erp.companies(tenant_id,id),
    FOREIGN KEY(tenant_id,requester_service_id) REFERENCES identity.service_accounts(tenant_id,id),
    CHECK(num_nonnulls(requester_user_id,requester_service_id)<=1), -- maintenance may be system-owned
    CHECK((claim_token IS NULL)=(claim_expires_at IS NULL)),
    CHECK(status NOT IN ('running','cancel_requested') OR claim_token IS NOT NULL),
    CHECK(status NOT IN ('succeeded','failed','cancelled') OR finished_at IS NOT NULL)
);
CREATE INDEX ix_jobs_ready ON erp.background_jobs(tenant_id,queue_name,run_after,id) WHERE status='queued';
CREATE INDEX ix_jobs_expired_claim ON erp.background_jobs(tenant_id,claim_expires_at) WHERE status='running';
CREATE INDEX ix_jobs_company_history ON erp.background_jobs(company_id,created_at DESC,id DESC);
CREATE FUNCTION erp.guard_job_request() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF (NEW.tenant_id,NEW.company_id,NEW.requester_user_id,NEW.requester_service_id,NEW.job_type,NEW.queue_name,NEW.job_key,NEW.parameters)
 IS DISTINCT FROM (OLD.tenant_id,OLD.company_id,OLD.requester_user_id,OLD.requester_service_id,OLD.job_type,OLD.queue_name,OLD.job_key,OLD.parameters) THEN
   RAISE EXCEPTION 'Job request is immutable; submit a new job'; END IF;
 NEW.updated_at:=clock_timestamp(); RETURN NEW;
END $$;
CREATE TRIGGER trg_job_request_immutable BEFORE UPDATE ON erp.background_jobs
 FOR EACH ROW EXECUTE FUNCTION erp.guard_job_request();
CREATE FUNCTION erp.claim_jobs(p_queue text,p_limit integer DEFAULT 10,p_lease_seconds integer DEFAULT 120)
RETURNS SETOF erp.background_jobs LANGUAGE plpgsql AS $$
BEGIN
 IF p_limit NOT BETWEEN 1 AND 100 OR p_lease_seconds NOT BETWEEN 10 AND 900 THEN RAISE EXCEPTION 'Invalid claim bounds'; END IF;
 RETURN QUERY WITH selected AS (
   SELECT id FROM erp.background_jobs WHERE tenant_id=erp.current_tenant_id() AND queue_name=p_queue
   AND attempt_count<max_attempts AND
   ((status='queued' AND run_after<=statement_timestamp()) OR (status='running' AND claim_expires_at<statement_timestamp()))
   ORDER BY run_after,id FOR UPDATE SKIP LOCKED LIMIT p_limit
 ) UPDATE erp.background_jobs j SET status='running',claim_token=uuidv7(),
   claim_expires_at=clock_timestamp()+make_interval(secs=>p_lease_seconds),heartbeat_at=clock_timestamp(),
   attempt_count=j.attempt_count+1,started_at=coalesce(j.started_at,clock_timestamp()),finished_at=NULL
 FROM selected s WHERE j.id=s.id RETURNING j.*;
END $$;
COMMENT ON FUNCTION erp.claim_jobs(text,integer,integer) IS
 'Claim and commit before network work. Complete/heartbeat only WHERE id AND claim_token match. Sweeper handles exhausted claims and cancellations.';

CREATE TABLE erp.integration_connections (
    id uuid PRIMARY KEY DEFAULT uuidv7(),
    company_id uuid NOT NULL REFERENCES erp.companies(id),
    channel_id uuid,
    provider text NOT NULL,
    name text NOT NULL,
    public_routing_id uuid NOT NULL DEFAULT uuidv7() UNIQUE,
    secret_reference text NOT NULL, -- reference into a secret manager, not the secret itself
    mode text NOT NULL DEFAULT 'disabled' CHECK(mode IN ('enabled','paused','disabled')),
    provider_account_reference text,
    settings jsonb NOT NULL DEFAULT '{}'::jsonb,
    row_version bigint NOT NULL DEFAULT 1,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE(company_id,id), UNIQUE(company_id,provider,name),
    FOREIGN KEY(company_id,channel_id) REFERENCES erp.sales_channels(company_id,id)
);
ALTER TABLE erp.integration_event_inbox ADD COLUMN connection_id uuid;
ALTER TABLE erp.integration_event_inbox ADD COLUMN payload_hash bytea CHECK(payload_hash IS NULL OR octet_length(payload_hash)=32);
ALTER TABLE erp.integration_event_inbox ADD COLUMN claim_token uuid;
ALTER TABLE erp.integration_event_inbox ADD COLUMN claim_expires_at timestamptz;
ALTER TABLE erp.integration_event_inbox ADD COLUMN attempt_count integer NOT NULL DEFAULT 0 CHECK(attempt_count>=0);
ALTER TABLE erp.integration_event_inbox ADD COLUMN run_after timestamptz NOT NULL DEFAULT clock_timestamp();
ALTER TABLE erp.integration_event_inbox ADD CONSTRAINT fk_inbox_connection FOREIGN KEY(company_id,connection_id) REFERENCES erp.integration_connections(company_id,id);
ALTER TABLE erp.integration_event_inbox DROP CONSTRAINT integration_event_inbox_status_check;
ALTER TABLE erp.integration_event_inbox ADD CONSTRAINT ck_inbox_status CHECK(status IN ('received','processing','processed','failed','paused'));
ALTER TABLE erp.integration_event_inbox ADD CONSTRAINT ck_inbox_claim CHECK((claim_token IS NULL)=(claim_expires_at IS NULL));
CREATE UNIQUE INDEX uq_inbox_connection_event ON erp.integration_event_inbox(company_id,connection_id,external_event_id) WHERE connection_id IS NOT NULL;
CREATE INDEX ix_inbox_ready ON erp.integration_event_inbox(company_id,run_after,id) WHERE status IN ('received','failed');
-- Existing rows may have null connection/hash; new webhooks require both at the application boundary.
CREATE FUNCTION erp.guard_inbox_payload() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF (NEW.company_id,NEW.channel_id,NEW.connection_id,NEW.external_event_id,NEW.event_type,NEW.payload,NEW.payload_hash)
 IS DISTINCT FROM (OLD.company_id,OLD.channel_id,OLD.connection_id,OLD.external_event_id,OLD.event_type,OLD.payload,OLD.payload_hash) THEN
 RAISE EXCEPTION 'Inbox event identity and payload are immutable'; END IF; RETURN NEW;
END $$;
CREATE TRIGGER trg_inbox_payload_immutable BEFORE UPDATE ON erp.integration_event_inbox
 FOR EACH ROW EXECUTE FUNCTION erp.guard_inbox_payload();

CREATE TABLE platform.operator_command_receipts (
    id uuid PRIMARY KEY DEFAULT uuidv7(),
    operator_user_id uuid NOT NULL REFERENCES identity.users(id),
    operation text NOT NULL,
    resource_key text NOT NULL DEFAULT '',
    idempotency_key varchar(128) NOT NULL,
    request_hash bytea NOT NULL CHECK(octet_length(request_hash)=32),
    response_status smallint NOT NULL CHECK(response_status BETWEEN 200 AND 599),
    result_reference jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE(operator_user_id,operation,resource_key,idempotency_key)
);
CREATE TABLE platform.operator_audit_events (
    id uuid PRIMARY KEY DEFAULT uuidv7(),
    operator_user_id uuid REFERENCES identity.users(id),
    action text NOT NULL,
    object_type text NOT NULL,
    object_id text NOT NULL,
    reason text NOT NULL,
    event_data jsonb NOT NULL DEFAULT '{}'::jsonb,
    occurred_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TRIGGER trg_operator_audit_immutable BEFORE UPDATE OR DELETE ON platform.operator_audit_events
 FOR EACH ROW EXECUTE FUNCTION erp.reject_audit_mutation();
CREATE TRIGGER trg_operator_receipt_immutable BEFORE UPDATE OR DELETE ON platform.operator_command_receipts
 FOR EACH ROW EXECUTE FUNCTION erp.reject_audit_mutation();

-- ============================================================
-- 04-inventory-tax.sql
-- ============================================================
-- A synchronized operational projection. Ledger rows remain the rebuildable truth.
CREATE TABLE erp.inventory_positions (
    id uuid PRIMARY KEY DEFAULT uuidv7(),
    company_id uuid NOT NULL REFERENCES erp.companies(id),
    warehouse_id uuid NOT NULL,
    item_id uuid NOT NULL,
    lot_id uuid,
    on_hand_quantity numeric(20,6) NOT NULL DEFAULT 0,
    reserved_quantity numeric(20,6) NOT NULL DEFAULT 0 CHECK(reserved_quantity>=0),
    value_company numeric(20,6) NOT NULL DEFAULT 0,
    lock_stub boolean NOT NULL DEFAULT true CHECK(lock_stub),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE NULLS NOT DISTINCT(company_id,warehouse_id,item_id,lot_id),
    FOREIGN KEY(company_id,warehouse_id) REFERENCES erp.warehouses(company_id,id),
    FOREIGN KEY(company_id,item_id) REFERENCES erp.items(company_id,id),
    FOREIGN KEY(company_id,item_id,lot_id) REFERENCES erp.inventory_lots(company_id,item_id,id),
    CHECK(on_hand_quantity>=reserved_quantity) -- this release disallows negative available stock
);
WITH scopes AS (
 SELECT company_id,warehouse_id,item_id,lot_id FROM erp.stock_movements
 UNION SELECT company_id,warehouse_id,item_id,lot_id FROM erp.inventory_reservations
), stock AS (
 SELECT company_id,warehouse_id,item_id,lot_id,sum(quantity_delta) AS qty,sum(value_delta_company) AS val
 FROM erp.stock_movements GROUP BY company_id,warehouse_id,item_id,lot_id
), reserved AS (
 SELECT company_id,warehouse_id,item_id,lot_id,sum(quantity) AS qty FROM erp.inventory_reservations
 WHERE status='active' GROUP BY company_id,warehouse_id,item_id,lot_id
)
INSERT INTO erp.inventory_positions(company_id,warehouse_id,item_id,lot_id,on_hand_quantity,reserved_quantity,value_company)
 SELECT s.company_id,s.warehouse_id,s.item_id,s.lot_id,coalesce(q.qty,0),coalesce(r.qty,0),coalesce(q.val,0)
 FROM scopes s LEFT JOIN stock q ON q.company_id=s.company_id AND q.warehouse_id=s.warehouse_id
 AND q.item_id=s.item_id AND q.lot_id IS NOT DISTINCT FROM s.lot_id
 LEFT JOIN reserved r ON r.company_id=s.company_id AND r.warehouse_id=s.warehouse_id
 AND r.item_id=s.item_id AND r.lot_id IS NOT DISTINCT FROM s.lot_id;
-- Narrow SECURITY DEFINER triggers permit ledger writers without granting direct projection mutation.
CREATE FUNCTION erp.project_stock_insert() RETURNS trigger LANGUAGE plpgsql
 SECURITY DEFINER SET search_path=pg_catalog AS $$
BEGIN
 IF NOT EXISTS(SELECT 1 FROM erp.companies WHERE id=NEW.company_id AND tenant_id=erp.current_tenant_id()) THEN
   RAISE EXCEPTION 'Stock projection tenant context missing/mismatched' USING ERRCODE='42501'; END IF;
 INSERT INTO erp.inventory_positions(company_id,warehouse_id,item_id,lot_id,on_hand_quantity,value_company)
 VALUES(NEW.company_id,NEW.warehouse_id,NEW.item_id,NEW.lot_id,NEW.quantity_delta,NEW.value_delta_company)
 ON CONFLICT(company_id,warehouse_id,item_id,lot_id) DO UPDATE
 SET on_hand_quantity=erp.inventory_positions.on_hand_quantity+excluded.on_hand_quantity,
 value_company=erp.inventory_positions.value_company+excluded.value_company,updated_at=clock_timestamp();
 RETURN NEW;
END $$;
CREATE TRIGGER trg_stock_projection AFTER INSERT ON erp.stock_movements
 FOR EACH ROW EXECUTE FUNCTION erp.project_stock_insert();
CREATE FUNCTION erp.project_reservation_change() RETURNS trigger LANGUAGE plpgsql
 SECURITY DEFINER SET search_path=pg_catalog AS $$
DECLARE v_delta numeric(20,6);
BEGIN
 IF TG_OP='DELETE' THEN RAISE EXCEPTION 'Release reservations; do not delete their history'; END IF;
 IF NOT EXISTS(SELECT 1 FROM erp.companies WHERE id=NEW.company_id AND tenant_id=erp.current_tenant_id()) THEN
   RAISE EXCEPTION 'Reservation tenant context missing/mismatched' USING ERRCODE='42501'; END IF;
 v_delta:=CASE WHEN NEW.status='active' THEN NEW.quantity ELSE 0 END;
 IF TG_OP='UPDATE' THEN
   IF (NEW.company_id,NEW.warehouse_id,NEW.item_id,NEW.lot_id,NEW.source_type,NEW.source_id,NEW.reservation_key)
   IS DISTINCT FROM (OLD.company_id,OLD.warehouse_id,OLD.item_id,OLD.lot_id,OLD.source_type,OLD.source_id,OLD.reservation_key) THEN
     RAISE EXCEPTION 'Reservation scope/source cannot change'; END IF;
   v_delta:=v_delta-CASE WHEN OLD.status='active' THEN OLD.quantity ELSE 0 END;
 END IF;
 IF v_delta<>0 THEN
   UPDATE erp.inventory_positions SET reserved_quantity=reserved_quantity+v_delta,updated_at=clock_timestamp()
   WHERE company_id=NEW.company_id AND warehouse_id=NEW.warehouse_id AND item_id=NEW.item_id AND lot_id IS NOT DISTINCT FROM NEW.lot_id;
   IF NOT FOUND THEN RAISE EXCEPTION 'Stock scope missing; cannot reserve unavailable stock'; END IF;
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER trg_reservation_projection AFTER INSERT OR UPDATE OR DELETE ON erp.inventory_reservations
 FOR EACH ROW EXECUTE FUNCTION erp.project_reservation_change();
CREATE VIEW erp.v_inventory_availability WITH(security_invoker=true) AS
 SELECT company_id,warehouse_id,item_id,lot_id,on_hand_quantity,reserved_quantity,
 on_hand_quantity-reserved_quantity AS available_quantity,value_company,updated_at
 FROM erp.inventory_positions;

ALTER TABLE erp.sales_order_lines ADD CONSTRAINT uq_sales_order_line_company_id UNIQUE(company_id,id);
ALTER TABLE erp.purchase_order_lines ADD CONSTRAINT uq_purchase_order_line_company_id UNIQUE(company_id,id);
CREATE TABLE erp.inventory_documents (
    id uuid PRIMARY KEY DEFAULT uuidv7(),
    company_id uuid NOT NULL REFERENCES erp.companies(id),
    document_no text NOT NULL,
    document_kind text NOT NULL CHECK(document_kind IN ('receipt','shipment','transfer','adjustment_in','adjustment_out')),
    document_date date NOT NULL,
    status text NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','posted','void')),
    journal_entry_id uuid,
    reversal_of_document_id uuid,
    row_version bigint NOT NULL DEFAULT 1 CHECK(row_version>0),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    posted_at timestamptz,
    UNIQUE(company_id,id), UNIQUE(company_id,document_no),
    FOREIGN KEY(company_id,journal_entry_id) REFERENCES erp.journal_entries(company_id,id),
    FOREIGN KEY(company_id,reversal_of_document_id) REFERENCES erp.inventory_documents(company_id,id),
    CHECK(status<>'posted' OR posted_at IS NOT NULL)
);
CREATE TABLE erp.inventory_document_lines (
    id uuid PRIMARY KEY DEFAULT uuidv7(),
    company_id uuid NOT NULL,
    inventory_document_id uuid NOT NULL,
    line_no integer NOT NULL CHECK(line_no>0),
    item_id uuid NOT NULL,
    lot_id uuid,
    from_warehouse_id uuid,
    to_warehouse_id uuid,
    quantity numeric(20,6) NOT NULL CHECK(quantity>0),
    unit_cost_company numeric(20,6) NOT NULL CHECK(unit_cost_company>=0),
    sales_order_line_id uuid,
    purchase_order_line_id uuid,
    UNIQUE(company_id,id), UNIQUE(company_id,inventory_document_id,line_no),
    FOREIGN KEY(company_id,inventory_document_id) REFERENCES erp.inventory_documents(company_id,id),
    FOREIGN KEY(company_id,item_id) REFERENCES erp.items(company_id,id),
    FOREIGN KEY(company_id,item_id,lot_id) REFERENCES erp.inventory_lots(company_id,item_id,id),
    FOREIGN KEY(company_id,from_warehouse_id) REFERENCES erp.warehouses(company_id,id),
    FOREIGN KEY(company_id,to_warehouse_id) REFERENCES erp.warehouses(company_id,id),
    FOREIGN KEY(company_id,sales_order_line_id) REFERENCES erp.sales_order_lines(company_id,id),
    FOREIGN KEY(company_id,purchase_order_line_id) REFERENCES erp.purchase_order_lines(company_id,id),
    CHECK(num_nonnulls(from_warehouse_id,to_warehouse_id)>=1),
    CHECK(from_warehouse_id IS NULL OR to_warehouse_id IS NULL OR from_warehouse_id<>to_warehouse_id),
    CHECK(num_nonnulls(sales_order_line_id,purchase_order_line_id)<=1)
);
ALTER TABLE erp.stock_movements ADD COLUMN inventory_document_line_id uuid;
ALTER TABLE erp.stock_movements ADD CONSTRAINT fk_stock_inventory_line
 FOREIGN KEY(company_id,inventory_document_line_id) REFERENCES erp.inventory_document_lines(company_id,id);
CREATE INDEX ix_inventory_document_movements ON erp.stock_movements(company_id,inventory_document_line_id) WHERE inventory_document_line_id IS NOT NULL;
CREATE INDEX ix_inventory_document_history ON erp.inventory_documents(company_id,document_date DESC,id DESC);
CREATE FUNCTION erp.guard_inventory_document() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF TG_OP='INSERT' AND NEW.status<>'draft' THEN RAISE EXCEPTION 'Create inventory document as draft'; END IF;
 IF TG_OP<>'INSERT' AND OLD.status='posted' THEN RAISE EXCEPTION 'Posted inventory documents are immutable'; END IF;
 IF TG_OP='DELETE' THEN RETURN OLD; END IF;
 IF NEW.status='posted' THEN NEW.posted_at:=coalesce(NEW.posted_at,clock_timestamp()); END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER trg_inventory_document_immutable BEFORE INSERT OR UPDATE OR DELETE ON erp.inventory_documents
 FOR EACH ROW EXECUTE FUNCTION erp.guard_inventory_document();
CREATE FUNCTION erp.guard_inventory_document_line() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE v_parent uuid; v_company uuid; v_status text;
BEGIN
 v_parent:=CASE WHEN TG_OP='DELETE' THEN OLD.inventory_document_id ELSE NEW.inventory_document_id END;
 v_company:=CASE WHEN TG_OP='DELETE' THEN OLD.company_id ELSE NEW.company_id END;
 IF TG_OP='UPDATE' AND NEW.inventory_document_id IS DISTINCT FROM OLD.inventory_document_id THEN
   RAISE EXCEPTION 'Cannot move inventory document line'; END IF;
 SELECT status INTO v_status FROM erp.inventory_documents WHERE company_id=v_company AND id=v_parent FOR UPDATE;
 IF v_status IS DISTINCT FROM 'draft' THEN RAISE EXCEPTION 'Inventory lines require a draft document'; END IF;
 IF TG_OP='DELETE' THEN RETURN OLD; END IF; RETURN NEW;
END $$;
CREATE TRIGGER trg_inventory_line_guard BEFORE INSERT OR UPDATE OR DELETE ON erp.inventory_document_lines
 FOR EACH ROW EXECUTE FUNCTION erp.guard_inventory_document_line();
CREATE FUNCTION erp.assert_inventory_document(p_company uuid,p_document uuid) RETURNS void LANGUAGE plpgsql AS $$
DECLARE d erp.inventory_documents%ROWTYPE;
BEGIN
 SELECT * INTO d FROM erp.inventory_documents WHERE company_id=p_company AND id=p_document;
 IF d.status<>'posted' THEN
   IF EXISTS(SELECT 1 FROM erp.stock_movements m JOIN erp.inventory_document_lines l
   ON l.company_id=m.company_id AND l.id=m.inventory_document_line_id
   WHERE l.company_id=p_company AND l.inventory_document_id=p_document) THEN
     RAISE EXCEPTION 'Stock effects cannot commit against an unposted inventory document'; END IF;
   RETURN;
 END IF;
 IF NOT EXISTS(SELECT 1 FROM erp.inventory_document_lines WHERE company_id=p_company AND inventory_document_id=p_document) THEN
   RAISE EXCEPTION 'Inventory document has no lines'; END IF;
 IF d.journal_entry_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM erp.journal_entries WHERE company_id=p_company AND id=d.journal_entry_id AND status='posted') THEN
   RAISE EXCEPTION 'Inventory journal must be posted'; END IF;
 IF EXISTS(SELECT 1 FROM erp.inventory_document_lines l WHERE l.company_id=p_company AND l.inventory_document_id=p_document AND (
   (d.document_kind IN ('receipt','adjustment_in') AND (l.from_warehouse_id IS NOT NULL OR l.to_warehouse_id IS NULL)) OR
   (d.document_kind IN ('shipment','adjustment_out') AND (l.from_warehouse_id IS NULL OR l.to_warehouse_id IS NOT NULL)) OR
   (d.document_kind='transfer' AND num_nonnulls(l.from_warehouse_id,l.to_warehouse_id)<>2) OR
   (l.from_warehouse_id IS NOT NULL AND coalesce((SELECT sum(m.quantity_delta) FROM erp.stock_movements m
      WHERE m.company_id=l.company_id AND m.inventory_document_line_id=l.id AND m.warehouse_id=l.from_warehouse_id),0)<>-l.quantity) OR
   (l.to_warehouse_id IS NOT NULL AND coalesce((SELECT sum(m.quantity_delta) FROM erp.stock_movements m
      WHERE m.company_id=l.company_id AND m.inventory_document_line_id=l.id AND m.warehouse_id=l.to_warehouse_id),0)<>l.quantity) OR
   EXISTS(SELECT 1 FROM erp.stock_movements m WHERE m.company_id=l.company_id AND m.inventory_document_line_id=l.id AND
      (m.item_id<>l.item_id OR m.lot_id IS DISTINCT FROM l.lot_id OR
      (m.warehouse_id IS DISTINCT FROM l.from_warehouse_id AND m.warehouse_id IS DISTINCT FROM l.to_warehouse_id)))
 )) THEN RAISE EXCEPTION 'Inventory line and movement quantities/scopes disagree'; END IF;
END $$;
CREATE FUNCTION erp.check_inventory_document_event() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE v_document uuid;
BEGIN
 IF TG_TABLE_NAME='inventory_documents' THEN v_document:=NEW.id;
 ELSE SELECT inventory_document_id INTO v_document FROM erp.inventory_document_lines
 WHERE company_id=NEW.company_id AND id=NEW.inventory_document_line_id; END IF;
 IF v_document IS NOT NULL THEN PERFORM erp.assert_inventory_document(NEW.company_id,v_document); END IF; RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER trg_inventory_document_consistent AFTER INSERT OR UPDATE ON erp.inventory_documents
 DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION erp.check_inventory_document_event();
CREATE CONSTRAINT TRIGGER trg_inventory_effect_consistent AFTER INSERT ON erp.stock_movements
 DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION erp.check_inventory_document_event();

-- A return is a stable period/kind container. Amendments are separate frozen revisions.
CREATE TABLE erp.tax_return_revisions (
    id uuid PRIMARY KEY DEFAULT uuidv7(),
    company_id uuid NOT NULL,
    tax_return_id uuid NOT NULL,
    revision_no integer NOT NULL CHECK(revision_no>0),
    prior_revision_id uuid,
    currency_code char(3) NOT NULL REFERENCES erp.currencies(code),
    taxable_base_total numeric(20,6) NOT NULL DEFAULT 0,
    tax_due_total numeric(20,6) NOT NULL DEFAULT 0,
    ruleset_reference text NOT NULL,
    source_cutoff timestamptz NOT NULL,
    filing_payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    frozen_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE(company_id,id), UNIQUE(company_id,tax_return_id,id), UNIQUE(company_id,tax_return_id,revision_no),
    FOREIGN KEY(company_id,tax_return_id) REFERENCES erp.tax_returns(company_id,id),
    FOREIGN KEY(company_id,tax_return_id,prior_revision_id) REFERENCES erp.tax_return_revisions(company_id,tax_return_id,id),
    CHECK((revision_no=1 AND prior_revision_id IS NULL) OR (revision_no>1 AND prior_revision_id IS NOT NULL)),
    CHECK(id IS DISTINCT FROM prior_revision_id)
);
CREATE TABLE erp.tax_return_revision_lines (
    id uuid PRIMARY KEY DEFAULT uuidv7(),
    company_id uuid NOT NULL,
    revision_id uuid NOT NULL,
    box_code text NOT NULL,
    description text NOT NULL,
    taxable_base_amount numeric(20,6) NOT NULL DEFAULT 0,
    output_tax_amount numeric(20,6) NOT NULL DEFAULT 0,
    recoverable_tax_amount numeric(20,6) NOT NULL DEFAULT 0,
    withholding_amount numeric(20,6) NOT NULL DEFAULT 0,
    adjustment_amount numeric(20,6) NOT NULL DEFAULT 0,
    UNIQUE(company_id,revision_id,box_code),
    FOREIGN KEY(company_id,revision_id) REFERENCES erp.tax_return_revisions(company_id,id)
);
CREATE TABLE erp.tax_submission_attempts (
    id uuid PRIMARY KEY DEFAULT uuidv7(),
    company_id uuid NOT NULL,
    revision_id uuid NOT NULL,
    attempt_no integer NOT NULL CHECK(attempt_no>0),
    provider text NOT NULL,
    idempotency_key text NOT NULL,
    status text NOT NULL DEFAULT 'queued' CHECK(status IN ('queued','sent','accepted','rejected','unknown')),
    request_hash bytea NOT NULL CHECK(octet_length(request_hash)=32),
    request_object_key text,
    response_object_key text,
    authority_receipt_ref text,
    last_error_code text,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    sent_at timestamptz,
    resolved_at timestamptz,
    UNIQUE(company_id,revision_id,attempt_no), UNIQUE(company_id,provider,idempotency_key),
    FOREIGN KEY(company_id,revision_id) REFERENCES erp.tax_return_revisions(company_id,id)
);
CREATE FUNCTION erp.guard_tax_revision() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE v_prior_no integer; v_prior_frozen timestamptz;
BEGIN
 IF TG_OP<>'INSERT' AND OLD.frozen_at IS NOT NULL THEN RAISE EXCEPTION 'Frozen tax revision is immutable'; END IF;
 IF TG_OP='DELETE' THEN RETURN OLD; END IF;
 IF NEW.prior_revision_id IS NOT NULL THEN
   SELECT revision_no,frozen_at INTO v_prior_no,v_prior_frozen FROM erp.tax_return_revisions
   WHERE company_id=NEW.company_id AND tax_return_id=NEW.tax_return_id AND id=NEW.prior_revision_id FOR SHARE;
   IF v_prior_no IS NULL OR v_prior_frozen IS NULL OR NEW.revision_no<>v_prior_no+1 THEN
     RAISE EXCEPTION 'Amendment must follow a frozen preceding revision'; END IF;
 END IF; RETURN NEW;
END $$;
CREATE TRIGGER trg_tax_revision_guard BEFORE INSERT OR UPDATE OR DELETE ON erp.tax_return_revisions
 FOR EACH ROW EXECUTE FUNCTION erp.guard_tax_revision();
CREATE FUNCTION erp.guard_tax_revision_line() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE v_company uuid; v_revision uuid; v_frozen timestamptz;
BEGIN
 v_company:=CASE WHEN TG_OP='DELETE' THEN OLD.company_id ELSE NEW.company_id END;
 v_revision:=CASE WHEN TG_OP='DELETE' THEN OLD.revision_id ELSE NEW.revision_id END;
 IF TG_OP='UPDATE' AND NEW.revision_id IS DISTINCT FROM OLD.revision_id THEN RAISE EXCEPTION 'Cannot move tax revision line'; END IF;
 SELECT frozen_at INTO v_frozen FROM erp.tax_return_revisions WHERE company_id=v_company AND id=v_revision FOR UPDATE;
 IF NOT FOUND OR v_frozen IS NOT NULL THEN RAISE EXCEPTION 'Tax revision missing or frozen'; END IF;
 IF TG_OP='DELETE' THEN RETURN OLD; END IF; RETURN NEW;
END $$;
CREATE TRIGGER trg_tax_revision_line_guard BEFORE INSERT OR UPDATE OR DELETE ON erp.tax_return_revision_lines
 FOR EACH ROW EXECUTE FUNCTION erp.guard_tax_revision_line();
CREATE FUNCTION erp.guard_tax_submission() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF NOT EXISTS(SELECT 1 FROM erp.tax_return_revisions WHERE company_id=NEW.company_id AND id=NEW.revision_id AND frozen_at IS NOT NULL) THEN
   RAISE EXCEPTION 'Submit only a frozen return revision'; END IF;
 IF TG_OP='UPDATE' AND (
 (NEW.company_id,NEW.revision_id,NEW.attempt_no,NEW.provider,NEW.idempotency_key,NEW.request_hash)
 IS DISTINCT FROM (OLD.company_id,OLD.revision_id,OLD.attempt_no,OLD.provider,OLD.idempotency_key,OLD.request_hash)
 OR OLD.status IN ('accepted','rejected')) THEN RAISE EXCEPTION 'Submission identity/final outcome is immutable'; END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER trg_tax_submission_guard BEFORE INSERT OR UPDATE ON erp.tax_submission_attempts
 FOR EACH ROW EXECUTE FUNCTION erp.guard_tax_submission();
CREATE TRIGGER trg_tax_submission_no_delete BEFORE DELETE ON erp.tax_submission_attempts
 FOR EACH ROW EXECUTE FUNCTION erp.reject_audit_mutation();
CREATE INDEX ix_tax_submission_pending ON erp.tax_submission_attempts(company_id,created_at,id) WHERE status IN ('queued','sent','unknown');

CREATE TABLE erp.tax_packs (
    id uuid PRIMARY KEY DEFAULT uuidv7(),
    jurisdiction_id uuid NOT NULL REFERENCES erp.tax_jurisdictions(id),
    version_code text NOT NULL,
    adapter_code text NOT NULL,
    source_reference text NOT NULL,
    content_hash bytea NOT NULL CHECK(octet_length(content_hash)=32),
    effective_from date NOT NULL,
    published_at timestamptz,
    UNIQUE(jurisdiction_id,version_code)
);
CREATE TABLE erp.tax_pack_rate_versions (
    tax_pack_id uuid NOT NULL REFERENCES erp.tax_packs(id),
    tax_rate_version_id uuid NOT NULL REFERENCES erp.tax_rate_versions(id),
    PRIMARY KEY(tax_pack_id,tax_rate_version_id)
);
COMMENT ON TABLE erp.tax_packs IS 'Reviewed country/jurisdiction release metadata; no statutory rates are pre-seeded.';

-- ============================================================
-- 05-hardening-security.sql
-- ============================================================
-- Baseline corrections and database security policies.
-- This section is kept separate so an existing installation can apply it in one reviewed migration.

-- Existing posting paths held the fiscal period FOR UPDATE. Shared readers allow unrelated posters to proceed;
-- period close/reopen acquires FOR UPDATE in the application/service path.
CREATE OR REPLACE FUNCTION erp.guard_posted_journal_entry()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE v_period erp.fiscal_periods%ROWTYPE;
BEGIN
    IF TG_OP='INSERT' THEN
        IF NEW.status<>'draft' THEN RAISE EXCEPTION 'Journal entries must be created as draft'; END IF;
        RETURN NEW;
    END IF;
    IF TG_OP='DELETE' THEN
        IF OLD.status='posted' THEN RAISE EXCEPTION 'Posted journal entries cannot be deleted'; END IF;
        RETURN OLD;
    END IF;
    IF OLD.status='posted' THEN RAISE EXCEPTION 'Posted journal entries cannot be changed'; END IF;
    IF NEW.status='posted' THEN
        IF OLD.status<>'draft' THEN RAISE EXCEPTION 'Only draft journal entries can be posted'; END IF;
        SELECT * INTO v_period FROM erp.fiscal_periods
        WHERE company_id=NEW.company_id AND id=NEW.fiscal_period_id FOR SHARE;
        IF NOT FOUND OR v_period.state<>'open' THEN RAISE EXCEPTION 'Fiscal period is missing or not open'; END IF;
        IF NEW.entry_date<v_period.starts_on OR NEW.entry_date>v_period.ends_on THEN
            RAISE EXCEPTION 'Entry date is outside the selected fiscal period'; END IF;
        IF NEW.entry_number IS NULL OR length(btrim(NEW.entry_number))=0 THEN
            RAISE EXCEPTION 'A journal entry number is required before posting'; END IF;
        IF NEW.posted_at IS NULL THEN NEW.posted_at:=clock_timestamp(); END IF;
    END IF;
    RETURN NEW;
END $$;
CREATE OR REPLACE FUNCTION erp.post_journal_entry(p_company_id uuid,p_entry_id uuid)
RETURNS void LANGUAGE plpgsql AS $$
DECLARE v_entry erp.journal_entries%ROWTYPE; v_period erp.fiscal_periods%ROWTYPE;
BEGIN
    SELECT * INTO v_entry FROM erp.journal_entries
    WHERE company_id=p_company_id AND id=p_entry_id FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'Journal entry not found'; END IF;
    IF v_entry.status<>'draft' THEN RAISE EXCEPTION 'Only draft journal entries can be posted'; END IF;
    SELECT * INTO v_period FROM erp.fiscal_periods
    WHERE company_id=p_company_id AND id=v_entry.fiscal_period_id FOR SHARE;
    IF NOT FOUND THEN RAISE EXCEPTION 'Fiscal period not found'; END IF;
    IF v_period.state<>'open' THEN RAISE EXCEPTION 'Fiscal period is not open'; END IF;
    IF v_entry.entry_date<v_period.starts_on OR v_entry.entry_date>v_period.ends_on THEN
        RAISE EXCEPTION 'Entry date is outside the selected fiscal period'; END IF;
    IF v_entry.entry_number IS NULL OR length(btrim(v_entry.entry_number))=0 THEN
        RAISE EXCEPTION 'A journal entry number is required before posting'; END IF;
    PERFORM erp.assert_journal_entry_balanced(p_company_id,p_entry_id);
    UPDATE erp.journal_entries SET status='posted',posted_at=clock_timestamp()
    WHERE company_id=p_company_id AND id=p_entry_id;
END $$;

-- A period close must use this lock order and then recheck all reconciliation rules in the same transaction.
CREATE FUNCTION erp.close_fiscal_period(p_company_id uuid,p_period_id uuid)
RETURNS void LANGUAGE plpgsql AS $$
DECLARE v_period erp.fiscal_periods%ROWTYPE;
BEGIN
 SELECT * INTO v_period FROM erp.fiscal_periods
 WHERE company_id=p_company_id AND id=p_period_id FOR UPDATE;
 IF NOT FOUND THEN RAISE EXCEPTION 'Fiscal period not found'; END IF;
 IF v_period.state<>'open' THEN RAISE EXCEPTION 'Only an open period can be closed'; END IF;
 -- Domain services perform tax, inventory, AR/AP, and unposted-draft checks before calling this function.
 UPDATE erp.fiscal_periods SET state='closed' WHERE company_id=p_company_id AND id=p_period_id;
END $$;

-- Optimistic concurrency counters for mutable aggregates. Application updates use WHERE row_version=:expected.
DO $$
DECLARE t text;
BEGIN
 FOREACH t IN ARRAY ARRAY['sales_orders','purchase_orders','sales_invoices','purchase_bills','payments','production_orders','boms'] LOOP
   EXECUTE format('ALTER TABLE erp.%I ADD COLUMN IF NOT EXISTS row_version bigint NOT NULL DEFAULT 1',t);
   EXECUTE format('ALTER TABLE erp.%I ADD CONSTRAINT %I CHECK(row_version>0)',t,'ck_'||t||'_row_version');
 END LOOP;
END $$;
CREATE FUNCTION erp.bump_row_version() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF NEW.row_version<>OLD.row_version+1 THEN NEW.row_version:=OLD.row_version+1; END IF;
 NEW.updated_at:=clock_timestamp(); RETURN NEW;
END $$;
CREATE TRIGGER trg_sales_order_version BEFORE UPDATE ON erp.sales_orders FOR EACH ROW EXECUTE FUNCTION erp.bump_row_version();
CREATE TRIGGER trg_purchase_order_version BEFORE UPDATE ON erp.purchase_orders FOR EACH ROW EXECUTE FUNCTION erp.bump_row_version();
CREATE TRIGGER trg_sales_invoice_version BEFORE UPDATE ON erp.sales_invoices FOR EACH ROW EXECUTE FUNCTION erp.bump_row_version();
CREATE TRIGGER trg_purchase_bill_version BEFORE UPDATE ON erp.purchase_bills FOR EACH ROW EXECUTE FUNCTION erp.bump_row_version();
CREATE TRIGGER trg_payment_version BEFORE UPDATE ON erp.payments FOR EACH ROW EXECUTE FUNCTION erp.bump_row_version();
CREATE TRIGGER trg_production_order_version BEFORE UPDATE ON erp.production_orders FOR EACH ROW EXECUTE FUNCTION erp.bump_row_version();
CREATE TRIGGER trg_bom_version BEFORE UPDATE ON erp.boms FOR EACH ROW EXECUTE FUNCTION erp.bump_row_version();

-- Typed credit links. Posted originals remain immutable; credit notes point to the original document.
ALTER TABLE erp.sales_invoices ADD COLUMN IF NOT EXISTS credit_of_invoice_id uuid;
ALTER TABLE erp.sales_invoices ADD CONSTRAINT fk_sales_credit_of_invoice
 FOREIGN KEY(company_id,credit_of_invoice_id) REFERENCES erp.sales_invoices(company_id,id);
ALTER TABLE erp.sales_invoices ADD CONSTRAINT ck_sales_credit_link
 CHECK(document_kind='invoice' OR credit_of_invoice_id IS NOT NULL);
ALTER TABLE erp.purchase_bills ADD COLUMN IF NOT EXISTS credit_of_bill_id uuid;
ALTER TABLE erp.purchase_bills ADD CONSTRAINT fk_purchase_credit_of_bill
 FOREIGN KEY(company_id,credit_of_bill_id) REFERENCES erp.purchase_bills(company_id,id);
ALTER TABLE erp.purchase_bills ADD CONSTRAINT ck_purchase_credit_link
 CHECK(document_kind='bill' OR credit_of_bill_id IS NOT NULL);
CREATE INDEX ix_sales_credit_link ON erp.sales_invoices(company_id,credit_of_invoice_id) WHERE credit_of_invoice_id IS NOT NULL;
CREATE INDEX ix_purchase_credit_link ON erp.purchase_bills(company_id,credit_of_bill_id) WHERE credit_of_bill_id IS NOT NULL;

-- Durable outbox claiming; a claim lease is not a business transaction.
CREATE FUNCTION erp.claim_outbox_events(p_limit integer DEFAULT 50,p_lease_seconds integer DEFAULT 120)
RETURNS SETOF erp.outbox_events LANGUAGE plpgsql AS $$
BEGIN
 IF p_limit NOT BETWEEN 1 AND 500 OR p_lease_seconds NOT BETWEEN 10 AND 900 THEN RAISE EXCEPTION 'Invalid claim bounds'; END IF;
 RETURN QUERY WITH selected AS (
   SELECT id FROM erp.outbox_events
   WHERE company_id IN (SELECT id FROM erp.companies WHERE tenant_id=erp.current_tenant_id())
   AND delivered_at IS NULL AND (claim_expires_at IS NULL OR claim_expires_at<statement_timestamp())
   ORDER BY occurred_at,id FOR UPDATE SKIP LOCKED LIMIT p_limit
 ) UPDATE erp.outbox_events o SET claim_token=uuidv7(),
    claim_expires_at=clock_timestamp()+make_interval(secs=>p_lease_seconds),attempt_count=o.attempt_count+1
 FROM selected s WHERE o.id=s.id RETURNING o.*;
END $$;
CREATE FUNCTION erp.complete_outbox_event(p_id uuid,p_claim uuid,p_error text DEFAULT NULL)
RETURNS boolean LANGUAGE plpgsql AS $$
BEGIN
 UPDATE erp.outbox_events SET delivered_at=CASE WHEN p_error IS NULL THEN clock_timestamp() ELSE NULL END,
 claim_token=NULL,claim_expires_at=NULL,last_error=p_error WHERE id=p_id AND claim_token=p_claim;
 RETURN FOUND;
END $$;

-- Corrected tax component view with currency and signed document polarity. The original view is retained for compatibility.
CREATE VIEW erp.v_tax_transaction_components_v2 WITH(security_invoker=true) AS
SELECT 'sales_invoice'::text source_kind,i.company_id,i.id source_document_id,i.invoice_no source_document_number,
 i.tax_point_date tax_date,i.partner_id,l.line_no,c.tax_jurisdiction_id,j.country_code,j.jurisdiction_code,j.name jurisdiction_name,
 tt.tax_family,tt.code tax_type_code,rs.schedule_code,i.currency_code,i.document_kind,
 CASE WHEN i.document_kind='credit_note' THEN -1 ELSE 1 END polarity,
 c.taxable_base_amount*CASE WHEN i.document_kind='credit_note' THEN -1 ELSE 1 END taxable_base_amount,
 c.tax_amount*CASE WHEN i.document_kind='credit_note' THEN -1 ELSE 1 END tax_amount,
 0::numeric(20,6)*CASE WHEN i.document_kind='credit_note' THEN -1 ELSE 1 END recoverable_amount,
 NULL::text withholding_treatment,c.rate_snapshot
FROM erp.sales_invoice_tax_components c JOIN erp.sales_invoices i ON i.company_id=c.company_id AND i.id=c.sales_invoice_id
JOIN erp.sales_invoice_lines l ON l.company_id=c.company_id AND l.sales_invoice_id=c.sales_invoice_id AND l.id=c.sales_invoice_line_id
JOIN erp.tax_jurisdictions j ON j.id=c.tax_jurisdiction_id JOIN erp.tax_rate_schedules rs ON rs.jurisdiction_id=c.tax_jurisdiction_id AND rs.id=c.rate_schedule_id
JOIN erp.tax_types tt ON tt.jurisdiction_id=rs.jurisdiction_id AND tt.id=rs.tax_type_id WHERE i.status='posted'
UNION ALL
SELECT 'purchase_bill',b.company_id,b.id,b.bill_no,b.tax_point_date,b.supplier_id,l.line_no,c.tax_jurisdiction_id,j.country_code,j.jurisdiction_code,j.name,
 tt.tax_family,tt.code,rs.schedule_code,b.currency_code,b.document_kind,CASE WHEN b.document_kind='supplier_credit' THEN -1 ELSE 1 END polarity,
 c.taxable_base_amount*CASE WHEN b.document_kind='supplier_credit' THEN -1 ELSE 1 END,
 c.tax_amount*CASE WHEN b.document_kind='supplier_credit' THEN -1 ELSE 1 END,c.recoverable_amount*CASE WHEN b.document_kind='supplier_credit' THEN -1 ELSE 1 END,
 NULL::text,c.rate_snapshot
FROM erp.purchase_bill_tax_components c JOIN erp.purchase_bills b ON b.company_id=c.company_id AND b.id=c.purchase_bill_id
JOIN erp.purchase_bill_lines l ON l.company_id=c.company_id AND l.purchase_bill_id=c.purchase_bill_id AND l.id=c.purchase_bill_line_id
JOIN erp.tax_jurisdictions j ON j.id=c.tax_jurisdiction_id JOIN erp.tax_rate_schedules rs ON rs.jurisdiction_id=c.tax_jurisdiction_id AND rs.id=c.rate_schedule_id
JOIN erp.tax_types tt ON tt.jurisdiction_id=rs.jurisdiction_id AND tt.id=rs.tax_type_id WHERE b.status='posted'
UNION ALL
SELECT 'payment_withholding',p.company_id,p.id,p.payment_no,p.payment_date,p.partner_id,NULL::integer,c.tax_jurisdiction_id,j.country_code,j.jurisdiction_code,j.name,
 tt.tax_family,tt.code,rs.schedule_code,p.currency_code,'payment',CASE WHEN p.direction='disbursement' THEN 1 ELSE -1 END,
 c.taxable_base_amount*CASE WHEN p.direction='disbursement' THEN 1 ELSE -1 END,
 c.tax_amount*CASE WHEN p.direction='disbursement' THEN 1 ELSE -1 END,0::numeric(20,6),c.tax_treatment,c.rate_snapshot
FROM erp.payment_tax_components c JOIN erp.payments p ON p.company_id=c.company_id AND p.id=c.payment_id
JOIN erp.tax_jurisdictions j ON j.id=c.tax_jurisdiction_id JOIN erp.tax_rate_schedules rs ON rs.jurisdiction_id=c.tax_jurisdiction_id AND rs.id=c.rate_schedule_id
JOIN erp.tax_types tt ON tt.jurisdiction_id=rs.jurisdiction_id AND tt.id=rs.tax_type_id WHERE p.status='posted';
COMMENT ON VIEW erp.v_tax_transaction_components_v2 IS 'Signed, currency-aware tax facts; country adapters define statutory aggregation.';

-- New signed open-item views preserve credit balances for the settlement service.
CREATE VIEW erp.v_open_ar_items_v2 WITH(security_invoker=true) AS
WITH totals AS (SELECT company_id,sales_invoice_id,sum(gross_amount) total_amount FROM erp.sales_invoice_lines GROUP BY company_id,sales_invoice_id),
apps AS (SELECT a.company_id,a.sales_invoice_id,sum(a.applied_document_amount) applied_amount FROM erp.ar_receipt_allocations a JOIN erp.payments p ON p.company_id=a.company_id AND p.id=a.payment_id WHERE p.status='posted' GROUP BY a.company_id,a.sales_invoice_id)
SELECT i.company_id,i.id sales_invoice_id,i.invoice_no,i.document_kind,i.partner_id,i.issue_date,i.due_date,i.currency_code,
 (CASE WHEN i.document_kind='credit_note' THEN -1 ELSE 1 END)*coalesce(t.total_amount,0) signed_document_total,
 coalesce(a.applied_amount,0) applied_amount,
 (CASE WHEN i.document_kind='credit_note' THEN -1 ELSE 1 END)*coalesce(t.total_amount,0)-coalesce(a.applied_amount,0) signed_open_amount
FROM erp.sales_invoices i LEFT JOIN totals t ON t.company_id=i.company_id AND t.sales_invoice_id=i.id LEFT JOIN apps a ON a.company_id=i.company_id AND a.sales_invoice_id=i.id WHERE i.status='posted';
CREATE VIEW erp.v_open_ap_items_v2 WITH(security_invoker=true) AS
WITH totals AS (SELECT company_id,purchase_bill_id,sum(gross_amount) total_amount FROM erp.purchase_bill_lines GROUP BY company_id,purchase_bill_id),
apps AS (SELECT a.company_id,a.purchase_bill_id,sum(a.applied_document_amount) applied_amount FROM erp.ap_disbursement_allocations a JOIN erp.payments p ON p.company_id=a.company_id AND p.id=a.payment_id WHERE p.status='posted' GROUP BY a.company_id,a.purchase_bill_id)
SELECT b.company_id,b.id purchase_bill_id,b.bill_no,b.document_kind,b.supplier_id,b.bill_date,b.due_date,b.currency_code,
 (CASE WHEN b.document_kind='supplier_credit' THEN -1 ELSE 1 END)*coalesce(t.total_amount,0) signed_document_total,
 coalesce(a.applied_amount,0) applied_amount,
 (CASE WHEN b.document_kind='supplier_credit' THEN -1 ELSE 1 END)*coalesce(t.total_amount,0)-coalesce(a.applied_amount,0) signed_open_amount
FROM erp.purchase_bills b LEFT JOIN totals t ON t.company_id=b.company_id AND t.purchase_bill_id=b.id LEFT JOIN apps a ON a.company_id=b.company_id AND a.purchase_bill_id=b.id WHERE b.status='posted';

-- Tax assessments must point to a posted journal when marked posted.
CREATE FUNCTION erp.guard_tax_assessment_posted() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF TG_OP='DELETE' THEN
   IF OLD.status='posted' THEN RAISE EXCEPTION 'Posted tax assessment is immutable'; END IF;
   RETURN OLD;
 END IF;
 IF NEW.status='posted' THEN
   IF NEW.journal_entry_id IS NULL OR NOT EXISTS(SELECT 1 FROM erp.journal_entries j WHERE j.company_id=NEW.company_id AND j.id=NEW.journal_entry_id AND j.status='posted') THEN
      RAISE EXCEPTION 'Posted tax assessment needs a posted journal'; END IF;
   IF NEW.calculated_at IS NULL THEN NEW.calculated_at:=clock_timestamp(); END IF;
 END IF;
 IF TG_OP='UPDATE' AND OLD.status='posted' THEN RAISE EXCEPTION 'Posted tax assessment is immutable'; END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER trg_tax_assessment_posted BEFORE INSERT OR UPDATE OR DELETE ON erp.tax_assessments
 FOR EACH ROW EXECUTE FUNCTION erp.guard_tax_assessment_posted();

-- Explicitly expose the baseline license view with the binding revision.
CREATE VIEW licensing.v_active_license_features_v2 WITH(security_invoker=true) AS
SELECT b.tenant_id,b.product_id,b.license_id,b.entitlement_revision,t.id license_term_id,
 f.feature_code,f.is_enabled,f.limit_value,t.starts_at,t.expires_at
FROM licensing.tenant_product_bindings b JOIN licensing.licenses l ON l.tenant_id=b.tenant_id AND l.id=b.license_id
JOIN licensing.license_terms t ON t.tenant_id=l.tenant_id AND t.license_id=l.id
JOIN licensing.plan_features f ON f.plan_version_id=t.plan_version_id
WHERE l.status='active' AND t.starts_at<=statement_timestamp() AND (t.expires_at IS NULL OR statement_timestamp()<t.expires_at);

-- Runtime tenant RLS for the new persistence. Global catalogs remain operator-controlled.
ALTER TABLE identity.tenant_memberships ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_membership_scope ON identity.tenant_memberships
 USING(tenant_id=erp.current_tenant_id() AND (user_id=identity.current_user_id() OR identity.current_service_id() IS NOT NULL))
 WITH CHECK(tenant_id=erp.current_tenant_id());
ALTER TABLE identity.company_memberships ENABLE ROW LEVEL SECURITY;
CREATE POLICY company_membership_scope ON identity.company_memberships
 USING(tenant_id=erp.current_tenant_id() AND (user_id=identity.current_user_id() OR identity.current_service_id() IS NOT NULL))
 WITH CHECK(tenant_id=erp.current_tenant_id());
ALTER TABLE identity.roles ENABLE ROW LEVEL SECURITY;
CREATE POLICY role_company_scope ON identity.roles
 USING(EXISTS(SELECT 1 FROM erp.companies c WHERE c.id=company_id AND c.tenant_id=erp.current_tenant_id()))
 WITH CHECK(EXISTS(SELECT 1 FROM erp.companies c WHERE c.id=company_id AND c.tenant_id=erp.current_tenant_id()));
ALTER TABLE identity.role_permissions ENABLE ROW LEVEL SECURITY;
CREATE POLICY role_permission_company_scope ON identity.role_permissions
 USING(EXISTS(SELECT 1 FROM erp.companies c WHERE c.id=company_id AND c.tenant_id=erp.current_tenant_id()))
 WITH CHECK(EXISTS(SELECT 1 FROM erp.companies c WHERE c.id=company_id AND c.tenant_id=erp.current_tenant_id()));
ALTER TABLE identity.company_role_assignments ENABLE ROW LEVEL SECURITY;
CREATE POLICY role_assignment_company_scope ON identity.company_role_assignments
 USING(EXISTS(SELECT 1 FROM erp.companies c WHERE c.id=company_id AND c.tenant_id=erp.current_tenant_id()))
 WITH CHECK(EXISTS(SELECT 1 FROM erp.companies c WHERE c.id=company_id AND c.tenant_id=erp.current_tenant_id()));
ALTER TABLE identity.service_accounts ENABLE ROW LEVEL SECURITY;
CREATE POLICY service_account_tenant_scope ON identity.service_accounts
 USING(tenant_id=erp.current_tenant_id()) WITH CHECK(tenant_id=erp.current_tenant_id());
ALTER TABLE identity.service_account_permissions ENABLE ROW LEVEL SECURITY;
CREATE POLICY service_account_permission_scope ON identity.service_account_permissions
 USING(tenant_id=erp.current_tenant_id()) WITH CHECK(tenant_id=erp.current_tenant_id());

DO $$
DECLARE t text;
BEGIN
 FOREACH t IN ARRAY ARRAY[
  'company_policy_state','company_module_settings','company_audit_events',
  'integration_connections','inventory_positions','inventory_documents','inventory_document_lines','tax_return_revisions',
  'tax_return_revision_lines','tax_submission_attempts'
 ] LOOP
  EXECUTE format('ALTER TABLE erp.%I ENABLE ROW LEVEL SECURITY',t);
  EXECUTE format('CREATE POLICY %I ON erp.%I USING (EXISTS(SELECT 1 FROM erp.companies c WHERE c.id=company_id AND c.tenant_id=erp.current_tenant_id())) WITH CHECK (EXISTS(SELECT 1 FROM erp.companies c WHERE c.id=company_id AND c.tenant_id=erp.current_tenant_id()))', 'tenant_'||t,t);
 END LOOP;
END $$;
ALTER TABLE erp.api_command_receipts ENABLE ROW LEVEL SECURITY;
CREATE POLICY command_receipt_tenant_scope ON erp.api_command_receipts
 USING(tenant_id=erp.current_tenant_id() AND
       (company_id IS NULL OR EXISTS(SELECT 1 FROM erp.companies c WHERE c.id=company_id AND c.tenant_id=erp.current_tenant_id())))
 WITH CHECK(tenant_id=erp.current_tenant_id() AND
       (company_id IS NULL OR EXISTS(SELECT 1 FROM erp.companies c WHERE c.id=company_id AND c.tenant_id=erp.current_tenant_id())));
ALTER TABLE erp.background_jobs ENABLE ROW LEVEL SECURITY;
CREATE POLICY background_job_tenant_scope ON erp.background_jobs
 USING(tenant_id=erp.current_tenant_id() AND
       (company_id IS NULL OR EXISTS(SELECT 1 FROM erp.companies c WHERE c.id=company_id AND c.tenant_id=erp.current_tenant_id())))
 WITH CHECK(tenant_id=erp.current_tenant_id() AND
       (company_id IS NULL OR EXISTS(SELECT 1 FROM erp.companies c WHERE c.id=company_id AND c.tenant_id=erp.current_tenant_id())));
ALTER TABLE licensing.tenant_product_bindings ENABLE ROW LEVEL SECURITY;
CREATE POLICY binding_tenant_scope ON licensing.tenant_product_bindings USING(tenant_id=erp.current_tenant_id()) WITH CHECK(tenant_id=erp.current_tenant_id());
ALTER TABLE licensing.billing_orders ENABLE ROW LEVEL SECURITY;
CREATE POLICY billing_order_tenant_scope ON licensing.billing_orders USING(tenant_id=erp.current_tenant_id()) WITH CHECK(tenant_id=erp.current_tenant_id());
ALTER TABLE licensing.billing_events ENABLE ROW LEVEL SECURITY;
CREATE POLICY billing_event_tenant_scope ON licensing.billing_events USING(tenant_id=erp.current_tenant_id()) WITH CHECK(tenant_id=erp.current_tenant_id());
ALTER TABLE platform.schema_releases ENABLE ROW LEVEL SECURITY;
CREATE POLICY platform_release_operator_scope ON platform.schema_releases USING(false) WITH CHECK(false);

-- Seed common action codes. Product-specific country packs add their own reviewed codes.
INSERT INTO identity.permissions(code,description) VALUES
 ('company.modules.manage','Change company module mode'),('company.settings.manage','Change typed company settings'),
 ('sales.invoice.view','Read sales invoices'),('sales.invoice.edit_draft','Edit invoice drafts'),('sales.invoice.post','Post sales invoices'),
 ('purchasing.bill.post','Post supplier bills'),('payments.post','Post payments'),('inventory.post','Post stock documents'),
 ('manufacturing.order.post','Post production effects'),('accounting.entry.post','Post manual journals'),('accounting.entry.reverse','Reverse journals'),
 ('accounting.period.close','Close fiscal periods'),('accounting.period.reopen','Reopen fiscal periods'),
 ('tax.return.submit','Submit tax returns'),('tax.configuration.manage','Manage tax setup'),('reports.view','View reports')
ON CONFLICT(code) DO NOTHING;

-- Keep direct runtime privileges minimal; deployment migrations grant ownership separately.
REVOKE ALL ON SCHEMA platform FROM PUBLIC;
REVOKE ALL ON ALL TABLES IN SCHEMA platform FROM PUBLIC;

COMMIT;
