from django.db import migrations

FORWARD_SQL = r"""
ALTER TABLE erp.items
    ADD COLUMN row_version bigint NOT NULL DEFAULT 1,
    ADD CONSTRAINT items_row_version_positive CHECK (row_version > 0);

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM erp.items
        WHERE item_kind <> 'stock' AND (track_lots OR track_serials)
    ) THEN
        RAISE EXCEPTION
            'Item migration requires lot/serial tracking only on stock items';
    END IF;
END;
$$;

ALTER TABLE erp.items
    ADD CONSTRAINT ck_items_tracking_requires_stock CHECK (
        item_kind = 'stock' OR (NOT track_lots AND NOT track_serials)
    );

CREATE FUNCTION erp.bump_item_version()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, erp
AS $$
BEGIN
    NEW.row_version := OLD.row_version + 1;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_item_version
    BEFORE UPDATE ON erp.items
    FOR EACH ROW EXECUTE FUNCTION erp.bump_item_version();

CREATE FUNCTION erp.audit_item_configuration_change()
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

    IF TG_TABLE_NAME = 'items' THEN
        v_action_prefix := 'item';
        v_object_type := 'item';
    ELSE
        v_action_prefix := 'item.accounting_profile';
        v_object_type := 'item_accounting_profile';
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

CREATE TRIGGER trg_item_audit
    AFTER INSERT OR UPDATE OR DELETE ON erp.items
    FOR EACH ROW EXECUTE FUNCTION erp.audit_item_configuration_change();

CREATE TRIGGER trg_item_accounting_profile_audit
    AFTER INSERT OR UPDATE OR DELETE ON erp.item_accounting_profiles
    FOR EACH ROW EXECUTE FUNCTION erp.audit_item_configuration_change();

INSERT INTO identity.permissions(code, description) VALUES
    ('item.view', 'View company items and services'),
    ('item.manage', 'Create and update company items and services')
ON CONFLICT(code) DO NOTHING;
"""

REVERSE_SQL = r"""
DROP TRIGGER IF EXISTS trg_item_accounting_profile_audit
    ON erp.item_accounting_profiles;
DROP TRIGGER IF EXISTS trg_item_audit ON erp.items;
DROP FUNCTION IF EXISTS erp.audit_item_configuration_change();
DELETE FROM identity.permissions WHERE code IN ('item.view', 'item.manage');
DROP TRIGGER IF EXISTS trg_item_version ON erp.items;
DROP FUNCTION IF EXISTS erp.bump_item_version();
ALTER TABLE erp.items
    DROP CONSTRAINT IF EXISTS ck_items_tracking_requires_stock,
    DROP CONSTRAINT IF EXISTS items_row_version_positive,
    DROP COLUMN IF EXISTS row_version;
"""


class Migration(migrations.Migration):
    dependencies = [("database", "0008_phase5_partner_details")]
    operations = [migrations.RunSQL(FORWARD_SQL, REVERSE_SQL)]
