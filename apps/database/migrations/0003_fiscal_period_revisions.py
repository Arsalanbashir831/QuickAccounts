from django.db import migrations

FORWARD_SQL = r"""
ALTER TABLE erp.fiscal_periods
    ADD COLUMN row_version bigint NOT NULL DEFAULT 1 CHECK (row_version > 0);

CREATE FUNCTION erp.bump_fiscal_period_version()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, erp
AS $$
BEGIN
    IF NEW.row_version <> OLD.row_version + 1 THEN
        NEW.row_version := OLD.row_version + 1;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_fiscal_period_version
    BEFORE UPDATE ON erp.fiscal_periods
    FOR EACH ROW EXECUTE FUNCTION erp.bump_fiscal_period_version();
"""

REVERSE_SQL = r"""
DROP TRIGGER IF EXISTS trg_fiscal_period_version ON erp.fiscal_periods;
DROP FUNCTION IF EXISTS erp.bump_fiscal_period_version();
ALTER TABLE erp.fiscal_periods DROP COLUMN IF EXISTS row_version;
"""


class Migration(migrations.Migration):
    dependencies = [("database", "0002_accounting_api_support")]
    operations = [migrations.RunSQL(FORWARD_SQL, REVERSE_SQL)]
