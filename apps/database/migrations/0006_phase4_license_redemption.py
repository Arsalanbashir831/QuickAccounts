from django.db import migrations

FORWARD_SQL = r"""
ALTER TABLE licensing.licenses
    ADD COLUMN redeemed_at timestamptz,
    ADD COLUMN redeemed_by uuid REFERENCES identity.users(id);

-- Licenses already selected before this migration are legacy-redemptions. Their original
-- actor is unknown, so redeemed_by intentionally remains null.
UPDATE licensing.licenses license
SET redeemed_at = license.issued_at
WHERE EXISTS (
    SELECT 1
    FROM licensing.tenant_product_bindings binding
    WHERE binding.tenant_id = license.tenant_id
      AND binding.product_id = license.product_id
      AND binding.license_id = license.id
);

ALTER TABLE licensing.license_events
    DROP CONSTRAINT license_events_event_type_check,
    ADD CONSTRAINT license_events_event_type_check CHECK (
        event_type IN (
            'issued', 'redeemed', 'activated', 'renewed',
            'suspended', 'resumed', 'revoked', 'grant_issued'
        )
    );

CREATE INDEX ix_licenses_unredeemed_credential
    ON licensing.licenses(tenant_id, product_id, license_number_hash)
    WHERE redeemed_at IS NULL;

CREATE OR REPLACE FUNCTION erp.assert_company_write(
    p_company uuid,
    p_product uuid,
    p_module text,
    p_permission text
)
RETURNS uuid
LANGUAGE plpgsql
AS $$
DECLARE
    v_license uuid;
    v_plan uuid;
    v_status text;
    v_now timestamptz;
BEGIN
    IF NOT EXISTS(
        SELECT 1 FROM erp.companies
        WHERE id=p_company AND tenant_id=erp.current_tenant_id() AND is_active
    ) THEN
        RAISE EXCEPTION 'Company is not accessible/active' USING ERRCODE='42501';
    END IF;
    SELECT license_id INTO v_license
    FROM licensing.tenant_product_bindings
    WHERE tenant_id=erp.current_tenant_id() AND product_id=p_product
    FOR SHARE;
    IF v_license IS NULL THEN
        RAISE EXCEPTION 'No designated license' USING ERRCODE='42501';
    END IF;
    SELECT status INTO v_status
    FROM licensing.licenses
    WHERE id=v_license AND tenant_id=erp.current_tenant_id()
    FOR SHARE;
    PERFORM 1 FROM erp.company_policy_state WHERE company_id=p_company FOR SHARE;
    IF NOT FOUND OR NOT identity.has_company_permission(p_company,p_permission) THEN
        RAISE EXCEPTION 'Company permission denied' USING ERRCODE='42501';
    END IF;
    IF NOT EXISTS(SELECT 1 FROM erp.companies WHERE id=p_company AND is_active) THEN
        RAISE EXCEPTION 'Company inactive' USING ERRCODE='42501';
    END IF;
    IF v_status IS DISTINCT FROM 'active' THEN
        RAISE EXCEPTION 'License inactive' USING ERRCODE='42501';
    END IF;
    v_now:=clock_timestamp();
    SELECT plan_version_id INTO v_plan
    FROM licensing.license_terms
    WHERE license_id=v_license AND starts_at<=v_now
      AND (expires_at IS NULL OR v_now<expires_at)
    ORDER BY starts_at DESC,id DESC LIMIT 1;
    IF v_plan IS NULL THEN
        RAISE EXCEPTION 'License expired' USING ERRCODE='42501';
    END IF;
    IF NOT EXISTS(SELECT 1 FROM erp.module_definitions WHERE code=p_module) THEN
        RAISE EXCEPTION 'Unknown module';
    END IF;
    IF EXISTS(
        WITH RECURSIVE required(code) AS (
            SELECT p_module
            UNION
            SELECT dependency.required_module_code
            FROM required current
            JOIN erp.module_dependencies dependency ON dependency.module_code=current.code
        )
        SELECT 1
        FROM required
        JOIN erp.module_definitions module ON module.code=required.code
        LEFT JOIN erp.company_module_settings setting
          ON setting.company_id=p_company AND setting.module_code=required.code
        WHERE coalesce(setting.mode,'disabled')<>'enabled'
           OR module.release_status<>'available'
           OR (
               module.license_feature_code IS NOT NULL
               AND NOT EXISTS(
                   SELECT 1 FROM licensing.plan_features feature
                   WHERE feature.plan_version_id=v_plan
                     AND feature.feature_code=module.license_feature_code
                     AND feature.is_enabled
               )
           )
    ) THEN
        RAISE EXCEPTION 'Module/dependency is unavailable or not entitled'
            USING ERRCODE='42501';
    END IF;
    RETURN v_plan;
END
$$;

-- Module-policy tables are mutated only through guarded functions in production. These
-- functions retain the request's transaction-local tenant/user context while using the
-- schema owner's narrowly scoped table privileges.
ALTER FUNCTION erp.set_company_modules(uuid, uuid, bigint, jsonb, text) SECURITY DEFINER;
ALTER FUNCTION erp.set_company_modules(uuid, uuid, bigint, jsonb, text)
    SET search_path = pg_catalog, erp, identity, licensing;
ALTER FUNCTION erp.guard_module_setting() SECURITY DEFINER;
ALTER FUNCTION erp.guard_module_setting()
    SET search_path = pg_catalog, erp, identity;
ALTER FUNCTION erp.provision_company_policy() SECURITY DEFINER;
ALTER FUNCTION erp.provision_company_policy()
    SET search_path = pg_catalog, erp;
ALTER FUNCTION erp.bump_company_authorization() SECURITY DEFINER;
ALTER FUNCTION erp.bump_company_authorization()
    SET search_path = pg_catalog, erp;
"""

REVERSE_SQL = r"""
DROP INDEX IF EXISTS licensing.ix_licenses_unredeemed_credential;
ALTER TABLE licensing.licenses
    DROP COLUMN IF EXISTS redeemed_by,
    DROP COLUMN IF EXISTS redeemed_at;
-- Keep `redeemed` accepted by the event constraint so append-only audit history never has
-- to be deleted during a rollback.
ALTER FUNCTION erp.set_company_modules(uuid, uuid, bigint, jsonb, text) SECURITY INVOKER;
ALTER FUNCTION erp.guard_module_setting() SECURITY INVOKER;
ALTER FUNCTION erp.provision_company_policy() SECURITY INVOKER;
ALTER FUNCTION erp.bump_company_authorization() SECURITY INVOKER;
"""


class Migration(migrations.Migration):
    dependencies = [("database", "0005_business_chart_templates")]
    operations = [migrations.RunSQL(FORWARD_SQL, REVERSE_SQL)]
