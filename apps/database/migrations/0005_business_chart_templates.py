from django.db import migrations

FORWARD_SQL = r"""
CREATE TABLE erp.chart_of_account_templates (
    code                text PRIMARY KEY,
    name                text NOT NULL,
    business_profile    text NOT NULL CHECK (
                            business_profile IN (
                                'retail_wholesale', 'ecommerce', 'manufacturing'
                            )
                        ),
    version             integer NOT NULL CHECK (version > 0),
    is_active           boolean NOT NULL DEFAULT true,
    created_at          timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (business_profile, version),
    UNIQUE (code, version)
);

CREATE UNIQUE INDEX uq_chart_of_account_templates_active_profile
    ON erp.chart_of_account_templates(business_profile)
    WHERE is_active;

CREATE TABLE erp.chart_of_account_template_accounts (
    template_code       text NOT NULL REFERENCES erp.chart_of_account_templates(code),
    code                text NOT NULL,
    parent_code         text,
    name                text NOT NULL,
    account_type        text NOT NULL CHECK (
                            account_type IN (
                                'asset', 'liability', 'equity', 'revenue', 'expense',
                                'cost_of_sales', 'receivable', 'payable'
                            )
                        ),
    normal_balance      text NOT NULL CHECK (normal_balance IN ('debit', 'credit')),
    is_control_account  boolean NOT NULL DEFAULT false,
    allow_posting       boolean NOT NULL DEFAULT true,
    sort_order          integer NOT NULL CHECK (sort_order > 0),
    PRIMARY KEY (template_code, code),
    UNIQUE (template_code, sort_order),
    CHECK (parent_code IS NULL OR parent_code <> code),
    CHECK (allow_posting OR NOT is_control_account),
    FOREIGN KEY (template_code, parent_code)
        REFERENCES erp.chart_of_account_template_accounts(template_code, code)
        DEFERRABLE INITIALLY DEFERRED
);

ALTER TABLE erp.companies
    ADD COLUMN business_type text,
    ADD COLUMN chart_template_code text REFERENCES erp.chart_of_account_templates(code),
    ADD COLUMN chart_template_applied_at timestamptz,
    ADD CONSTRAINT ck_companies_business_type CHECK (
        business_type IS NULL OR business_type IN (
            'retail', 'wholesale', 'ecommerce', 'manufacturing'
        )
    ),
    ADD CONSTRAINT ck_companies_chart_template_application CHECK (
        (chart_template_code IS NULL AND chart_template_applied_at IS NULL)
        OR (chart_template_code IS NOT NULL AND chart_template_applied_at IS NOT NULL)
    );

ALTER TABLE erp.accounts
    ADD COLUMN source_template_code text,
    ADD COLUMN source_template_account_code text,
    ADD CONSTRAINT ck_accounts_template_source CHECK (
        (source_template_code IS NULL AND source_template_account_code IS NULL)
        OR (source_template_code IS NOT NULL AND source_template_account_code IS NOT NULL)
    ),
    ADD CONSTRAINT fk_accounts_template_source FOREIGN KEY (
        source_template_code, source_template_account_code
    ) REFERENCES erp.chart_of_account_template_accounts(template_code, code),
    ADD CONSTRAINT uq_accounts_template_source UNIQUE (
        company_id, source_template_code, source_template_account_code
    );

INSERT INTO erp.chart_of_account_templates(code, name, business_profile, version) VALUES
    ('retail-wholesale-v1', 'Retail and Wholesale Standard', 'retail_wholesale', 1),
    ('ecommerce-v1', 'E-commerce Standard', 'ecommerce', 1),
    ('manufacturing-v1', 'Manufacturing Standard', 'manufacturing', 1);

-- Every profile starts with the same financial-statement backbone. Profile-specific
-- accounts are inserted below without changing the meaning of these shared codes.
INSERT INTO erp.chart_of_account_template_accounts(
    template_code, code, parent_code, name, account_type, normal_balance,
    is_control_account, allow_posting, sort_order
)
SELECT template.code, account.code, account.parent_code, account.name,
       account.account_type, account.normal_balance, account.is_control_account,
       account.allow_posting, account.sort_order
FROM erp.chart_of_account_templates template
CROSS JOIN (VALUES
    ('1000', NULL,   'Assets',                    'asset',         'debit',  false, false,  10),
    ('1100', '1000', 'Cash and Banks',            'asset',         'debit',  false, false,  20),
    ('1110', '1100', 'Cash on Hand',              'asset',         'debit',  false, true,   30),
    ('1120', '1100', 'Bank Accounts',             'asset',         'debit',  false, true,   40),
    ('1200', '1000', 'Accounts Receivable',       'receivable',    'debit',  true,  true,   50),
    ('1300', '1000', 'Inventory',                 'asset',         'debit',  true,  true,   60),
    ('1400', '1000', 'Recoverable Input Tax',     'asset',         'debit',  true,  true,   70),
    ('2000', NULL,   'Liabilities',               'liability',     'credit', false, false,  80),
    ('2100', '2000', 'Accounts Payable',          'payable',       'credit', true,  true,   90),
    ('2200', '2000', 'Output Tax Payable',        'liability',     'credit', true,  true,  100),
    ('2300', '2000', 'Accrued Expenses',          'liability',     'credit', false, true,  110),
    ('3000', NULL,   'Equity',                    'equity',        'credit', false, false, 120),
    ('3100', '3000', 'Owner Capital',             'equity',        'credit', false, true,  130),
    ('3200', '3000', 'Retained Earnings',         'equity',        'credit', false, true,  140),
    ('4000', NULL,   'Revenue',                   'revenue',       'credit', false, false, 150),
    ('4100', '4000', 'Sales Revenue',             'revenue',       'credit', false, true,  160),
    ('4200', '4000', 'Sales Returns and Discounts','revenue',      'debit',  false, true,  170),
    ('5000', NULL,   'Cost of Sales',             'cost_of_sales', 'debit',  false, false, 180),
    ('5100', '5000', 'Cost of Goods Sold',        'cost_of_sales', 'debit',  false, true,  190),
    ('6000', NULL,   'Operating Expenses',        'expense',       'debit',  false, false, 200),
    ('6100', '6000', 'Payroll Expense',           'expense',       'debit',  false, true,  210),
    ('6200', '6000', 'Rent and Utilities',        'expense',       'debit',  false, true,  220),
    ('6300', '6000', 'General and Administrative','expense',       'debit',  false, true,  230)
) AS account(
    code, parent_code, name, account_type, normal_balance,
    is_control_account, allow_posting, sort_order
);

INSERT INTO erp.chart_of_account_template_accounts(
    template_code, code, parent_code, name, account_type, normal_balance,
    is_control_account, allow_posting, sort_order
) VALUES
    ('retail-wholesale-v1', '1310', '1300', 'Merchandise Inventory',
     'asset', 'debit', false, true, 65),
    ('retail-wholesale-v1', '4110', '4000', 'Retail and Wholesale Sales',
     'revenue', 'credit', false, true, 165),
    ('retail-wholesale-v1', '5110', '5000', 'Merchandise Cost of Sales',
     'cost_of_sales', 'debit', false, true, 195),

    ('ecommerce-v1', '1130', '1100', 'Payment Gateway Clearing',
     'asset', 'debit', true, true, 45),
    ('ecommerce-v1', '1210', '1000', 'Marketplace Receivable',
     'receivable', 'debit', true, true, 55),
    ('ecommerce-v1', '1310', '1300', 'Fulfilment Inventory',
     'asset', 'debit', false, true, 65),
    ('ecommerce-v1', '4110', '4000', 'Webstore Sales',
     'revenue', 'credit', false, true, 165),
    ('ecommerce-v1', '4120', '4000', 'Marketplace Sales',
     'revenue', 'credit', false, true, 166),
    ('ecommerce-v1', '4300', '4000', 'Shipping Income',
     'revenue', 'credit', false, true, 175),
    ('ecommerce-v1', '5110', '5000', 'Fulfilment Cost of Sales',
     'cost_of_sales', 'debit', false, true, 195),
    ('ecommerce-v1', '6400', '6000', 'Marketplace and Gateway Fees',
     'expense', 'debit', false, true, 240),
    ('ecommerce-v1', '6410', '6000', 'Digital Advertising',
     'expense', 'debit', false, true, 250),
    ('ecommerce-v1', '6420', '6000', 'Shipping and Fulfilment Expense',
     'expense', 'debit', false, true, 260),
    ('ecommerce-v1', '6430', '6000', 'Chargebacks and Payment Disputes',
     'expense', 'debit', false, true, 270),

    ('manufacturing-v1', '1310', '1300', 'Raw Materials Inventory',
     'asset', 'debit', true, true, 61),
    ('manufacturing-v1', '1320', '1300', 'Work in Process Inventory',
     'asset', 'debit', true, true, 62),
    ('manufacturing-v1', '1330', '1300', 'Finished Goods Inventory',
     'asset', 'debit', true, true, 63),
    ('manufacturing-v1', '1340', '1300', 'Factory Supplies Inventory',
     'asset', 'debit', false, true, 64),
    ('manufacturing-v1', '4110', '4000', 'Manufactured Goods Sales',
     'revenue', 'credit', false, true, 165),
    ('manufacturing-v1', '5110', '5000', 'Finished Goods Cost of Sales',
     'cost_of_sales', 'debit', false, true, 191),
    ('manufacturing-v1', '5200', '5000', 'Direct Materials Consumed',
     'cost_of_sales', 'debit', false, true, 192),
    ('manufacturing-v1', '5210', '5000', 'Direct Labour Applied',
     'cost_of_sales', 'debit', false, true, 193),
    ('manufacturing-v1', '5220', '5000', 'Manufacturing Overhead Applied',
     'cost_of_sales', 'debit', false, true, 194),
    ('manufacturing-v1', '5230', '5000', 'Manufacturing Variance',
     'cost_of_sales', 'debit', false, true, 196),
    ('manufacturing-v1', '6400', '6000', 'Factory Overhead Expense',
     'expense', 'debit', false, true, 240),
    ('manufacturing-v1', '6410', '6000', 'Repairs and Maintenance',
     'expense', 'debit', false, true, 250);
"""


REVERSE_SQL = r"""
ALTER TABLE erp.accounts
    DROP CONSTRAINT IF EXISTS uq_accounts_template_source,
    DROP CONSTRAINT IF EXISTS fk_accounts_template_source,
    DROP CONSTRAINT IF EXISTS ck_accounts_template_source,
    DROP COLUMN IF EXISTS source_template_account_code,
    DROP COLUMN IF EXISTS source_template_code;

ALTER TABLE erp.companies
    DROP CONSTRAINT IF EXISTS ck_companies_chart_template_application,
    DROP CONSTRAINT IF EXISTS ck_companies_business_type,
    DROP COLUMN IF EXISTS chart_template_applied_at,
    DROP COLUMN IF EXISTS chart_template_code,
    DROP COLUMN IF EXISTS business_type;

DROP TABLE IF EXISTS erp.chart_of_account_template_accounts;
DROP TABLE IF EXISTS erp.chart_of_account_templates;
"""


class Migration(migrations.Migration):
    dependencies = [("database", "0004_module_change_audit_once")]
    operations = [migrations.RunSQL(FORWARD_SQL, REVERSE_SQL)]
