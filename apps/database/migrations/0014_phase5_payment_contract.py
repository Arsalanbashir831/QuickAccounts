from django.db import migrations

FORWARD_SQL = r"""
ALTER TABLE erp.payments
    ADD COLUMN cash_account_id uuid;

ALTER TABLE erp.payments
    ADD CONSTRAINT fk_payment_cash_account
    FOREIGN KEY (company_id, cash_account_id)
    REFERENCES erp.accounts(company_id, id);

ALTER TABLE erp.payment_tax_components
    ADD COLUMN withholding_account_id_snapshot uuid;

ALTER TABLE erp.payment_tax_components
    ADD CONSTRAINT fk_payment_withholding_account_snapshot
    FOREIGN KEY (company_id, withholding_account_id_snapshot)
    REFERENCES erp.accounts(company_id, id);
"""


REVERSE_SQL = r"""
ALTER TABLE erp.payment_tax_components
    DROP CONSTRAINT fk_payment_withholding_account_snapshot,
    DROP COLUMN withholding_account_id_snapshot;
ALTER TABLE erp.payments
    DROP CONSTRAINT fk_payment_cash_account,
    DROP COLUMN cash_account_id;
"""


class Migration(migrations.Migration):
    dependencies = [("database", "0013_phase5_purchase_bills")]
    operations = [migrations.RunSQL(FORWARD_SQL, REVERSE_SQL)]
