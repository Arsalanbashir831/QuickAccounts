from django.db import migrations

FORWARD_SQL = r"""
ALTER TABLE erp.business_partners
    ADD COLUMN row_version bigint NOT NULL DEFAULT 1,
    ADD CONSTRAINT business_partners_row_version_positive CHECK (row_version > 0);

CREATE FUNCTION erp.bump_business_partner_version()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, erp
AS $$
BEGIN
    NEW.row_version := OLD.row_version + 1;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_business_partner_version
    BEFORE UPDATE ON erp.business_partners
    FOR EACH ROW EXECUTE FUNCTION erp.bump_business_partner_version();

CREATE FUNCTION erp.audit_business_partner_change()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, erp, identity
AS $$
DECLARE
    v_company_id uuid := CASE WHEN TG_OP = 'DELETE' THEN OLD.company_id ELSE NEW.company_id END;
    v_partner_id uuid := CASE WHEN TG_OP = 'DELETE' THEN OLD.id ELSE NEW.id END;
    v_tenant_id uuid;
BEGIN
    SELECT tenant_id INTO STRICT v_tenant_id
    FROM erp.companies
    WHERE id = v_company_id;

    INSERT INTO erp.company_audit_events(
        tenant_id, company_id, actor_user_id, actor_service_id, action,
        object_type, object_id, old_data, new_data, request_id
    ) VALUES (
        v_tenant_id,
        v_company_id,
        identity.current_user_id(),
        identity.current_service_id(),
        CASE TG_OP
            WHEN 'INSERT' THEN 'party.created'
            WHEN 'UPDATE' THEN 'party.updated'
            ELSE 'party.deleted'
        END,
        'business_partner',
        v_partner_id::text,
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

CREATE TRIGGER trg_business_partner_audit
    AFTER INSERT OR UPDATE OR DELETE ON erp.business_partners
    FOR EACH ROW EXECUTE FUNCTION erp.audit_business_partner_change();

INSERT INTO identity.permissions(code, description) VALUES
    ('party.view', 'View company business partners'),
    ('party.manage', 'Create and update company business partners')
ON CONFLICT(code) DO NOTHING;
"""

REVERSE_SQL = r"""
DROP TRIGGER IF EXISTS trg_business_partner_audit ON erp.business_partners;
DROP FUNCTION IF EXISTS erp.audit_business_partner_change();
DELETE FROM identity.permissions WHERE code IN ('party.view', 'party.manage');
DROP TRIGGER IF EXISTS trg_business_partner_version ON erp.business_partners;
DROP FUNCTION IF EXISTS erp.bump_business_partner_version();
ALTER TABLE erp.business_partners
    DROP CONSTRAINT IF EXISTS business_partners_row_version_positive,
    DROP COLUMN IF EXISTS row_version;
"""


class Migration(migrations.Migration):
    dependencies = [("database", "0006_phase4_license_redemption")]
    operations = [migrations.RunSQL(FORWARD_SQL, REVERSE_SQL)]
