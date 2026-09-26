from django.db import migrations

FORWARD_SQL = r"""
ALTER TABLE erp.partner_addresses
    ADD COLUMN row_version bigint NOT NULL DEFAULT 1,
    ADD COLUMN updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    ADD CONSTRAINT partner_addresses_row_version_positive CHECK (row_version > 0);

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM erp.partner_addresses
        WHERE is_default
        GROUP BY company_id, partner_id, address_kind
        HAVING count(*) > 1
    ) THEN
        RAISE EXCEPTION
            'Partner address migration requires at most one default per partner/address kind';
    END IF;
END;
$$;

CREATE UNIQUE INDEX ux_partner_addresses_default_kind
    ON erp.partner_addresses(company_id, partner_id, address_kind)
    WHERE is_default;

CREATE FUNCTION erp.bump_partner_address_version()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, erp
AS $$
BEGIN
    NEW.row_version := OLD.row_version + 1;
    NEW.updated_at := clock_timestamp();
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_partner_address_version
    BEFORE UPDATE ON erp.partner_addresses
    FOR EACH ROW EXECUTE FUNCTION erp.bump_partner_address_version();

CREATE FUNCTION erp.audit_partner_detail_change()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, erp, identity
AS $$
DECLARE
    v_company_id uuid := CASE WHEN TG_OP = 'DELETE' THEN OLD.company_id ELSE NEW.company_id END;
    v_object_id uuid := CASE WHEN TG_OP = 'DELETE' THEN OLD.id ELSE NEW.id END;
    v_tenant_id uuid;
    v_action_prefix text;
    v_object_type text;
BEGIN
    SELECT tenant_id INTO STRICT v_tenant_id
    FROM erp.companies
    WHERE id = v_company_id;

    IF TG_TABLE_NAME = 'partner_addresses' THEN
        v_action_prefix := 'party.address';
        v_object_type := 'partner_address';
    ELSE
        v_action_prefix := 'party.tax_registration';
        v_object_type := 'partner_tax_registration';
    END IF;

    INSERT INTO erp.company_audit_events(
        tenant_id, company_id, actor_user_id, actor_service_id, action,
        object_type, object_id, old_data, new_data, request_id
    ) VALUES (
        v_tenant_id,
        v_company_id,
        identity.current_user_id(),
        identity.current_service_id(),
        v_action_prefix || CASE TG_OP
            WHEN 'INSERT' THEN '.created'
            WHEN 'UPDATE' THEN '.updated'
            ELSE '.deleted'
        END,
        v_object_type,
        v_object_id::text,
        CASE WHEN TG_OP = 'INSERT' THEN NULL ELSE to_jsonb(OLD) END,
        CASE WHEN TG_OP = 'DELETE' THEN NULL ELSE to_jsonb(NEW) END,
        nullif(current_setting('app.request_id', true), '')
    );

    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_partner_address_audit
    AFTER INSERT OR UPDATE OR DELETE ON erp.partner_addresses
    FOR EACH ROW EXECUTE FUNCTION erp.audit_partner_detail_change();

CREATE TRIGGER trg_partner_tax_registration_audit
    AFTER INSERT OR UPDATE OR DELETE ON erp.partner_tax_registrations
    FOR EACH ROW EXECUTE FUNCTION erp.audit_partner_detail_change();
"""

REVERSE_SQL = r"""
DROP TRIGGER IF EXISTS trg_partner_tax_registration_audit
    ON erp.partner_tax_registrations;
DROP TRIGGER IF EXISTS trg_partner_address_audit ON erp.partner_addresses;
DROP FUNCTION IF EXISTS erp.audit_partner_detail_change();
DROP TRIGGER IF EXISTS trg_partner_address_version ON erp.partner_addresses;
DROP FUNCTION IF EXISTS erp.bump_partner_address_version();
DROP INDEX IF EXISTS erp.ux_partner_addresses_default_kind;
ALTER TABLE erp.partner_addresses
    DROP CONSTRAINT IF EXISTS partner_addresses_row_version_positive,
    DROP COLUMN IF EXISTS updated_at,
    DROP COLUMN IF EXISTS row_version;
"""


class Migration(migrations.Migration):
    dependencies = [("database", "0007_phase5_party_foundation")]
    operations = [migrations.RunSQL(FORWARD_SQL, REVERSE_SQL)]
