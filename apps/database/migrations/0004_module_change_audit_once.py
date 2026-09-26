from django.db import migrations

FORWARD_SQL = r"""
CREATE OR REPLACE FUNCTION erp.set_company_modules(
    p_company uuid,
    p_product uuid,
    p_expected_revision bigint,
    p_changes jsonb,
    p_reason text
)
RETURNS bigint
LANGUAGE plpgsql
AS $$
DECLARE
    v_revision bigint;
    v_license uuid;
    v_plan uuid;
    v_status text;
    v_now timestamptz;
    v_updated integer;
BEGIN
    IF jsonb_typeof(p_changes) IS DISTINCT FROM 'array'
       OR jsonb_array_length(p_changes) NOT BETWEEN 1 AND 24 THEN
        RAISE EXCEPTION 'changes must be a bounded nonempty array';
    END IF;
    IF nullif(btrim(p_reason), '') IS NULL THEN
        RAISE EXCEPTION 'Change reason required';
    END IF;

    SELECT license_id INTO v_license
    FROM licensing.tenant_product_bindings
    WHERE tenant_id=erp.current_tenant_id() AND product_id=p_product
    FOR SHARE;

    IF v_license IS NOT NULL THEN
        SELECT status INTO v_status
        FROM licensing.licenses
        WHERE id=v_license AND tenant_id=erp.current_tenant_id()
        FOR SHARE;
    END IF;

    SELECT revision INTO v_revision
    FROM erp.company_policy_state
    WHERE company_id=p_company
    FOR UPDATE;

    IF NOT FOUND OR NOT identity.has_company_permission(p_company, 'company.modules.manage') THEN
        RAISE EXCEPTION 'Company administration denied' USING ERRCODE='42501';
    END IF;
    IF p_expected_revision IS NULL OR v_revision<>p_expected_revision THEN
        RAISE EXCEPTION 'Policy revision conflict' USING ERRCODE='40001';
    END IF;
    IF EXISTS(
        SELECT 1
        FROM jsonb_to_recordset(p_changes) AS x(module_code text, mode text)
        GROUP BY module_code HAVING count(*)>1
    ) THEN
        RAISE EXCEPTION 'Duplicate module in batch';
    END IF;

    v_now := clock_timestamp();
    SELECT plan_version_id INTO v_plan
    FROM licensing.license_terms
    WHERE license_id=v_license AND starts_at<=v_now
      AND (expires_at IS NULL OR v_now<expires_at);

    IF EXISTS(
        SELECT 1
        FROM jsonb_to_recordset(p_changes) AS x(module_code text, mode text)
        LEFT JOIN erp.module_definitions m ON m.code=x.module_code
        WHERE m.code IS NULL OR x.mode IS NULL
           OR x.mode NOT IN ('enabled', 'read_only', 'disabled')
           OR (
               x.mode='enabled' AND (
                   v_status IS DISTINCT FROM 'active' OR v_plan IS NULL OR (
                       m.license_feature_code IS NOT NULL AND NOT EXISTS(
                           SELECT 1 FROM licensing.plan_features f
                           WHERE f.plan_version_id=v_plan
                             AND f.feature_code=m.license_feature_code
                             AND f.is_enabled
                       )
                   )
               )
           )
    ) THEN
        RAISE EXCEPTION 'Invalid or unlicensed module change' USING ERRCODE='42501';
    END IF;

    PERFORM set_config('app.change_reason', p_reason, true);
    UPDATE erp.company_module_settings setting
    SET mode=change.mode
    FROM jsonb_to_recordset(p_changes) AS change(module_code text, mode text)
    WHERE setting.company_id=p_company AND setting.module_code=change.module_code;
    GET DIAGNOSTICS v_updated = ROW_COUNT;
    IF v_updated<>jsonb_array_length(p_changes) THEN
        RAISE EXCEPTION 'Company module settings are incomplete';
    END IF;

    IF EXISTS(
        SELECT 1
        FROM erp.company_module_settings s
        JOIN erp.module_dependencies d ON d.module_code=s.module_code
        LEFT JOIN erp.company_module_settings required
          ON required.company_id=s.company_id
         AND required.module_code=d.required_module_code
        WHERE s.company_id=p_company AND s.mode='enabled'
          AND coalesce(required.mode, 'disabled')<>'enabled'
    ) THEN
        RAISE EXCEPTION 'Module dependency conflict';
    END IF;

    SELECT revision INTO v_revision
    FROM erp.company_policy_state
    WHERE company_id=p_company;
    INSERT INTO erp.outbox_events(
        company_id,event_key,aggregate_type,aggregate_id,event_type,payload
    ) VALUES (
        p_company,'policy:'||v_revision,'company',p_company,
        'company.modules.changed',
        jsonb_build_object('policy_revision',v_revision,'changes',p_changes)
    );
    RETURN v_revision;
END
$$;
"""


class Migration(migrations.Migration):
    dependencies = [("database", "0003_fiscal_period_revisions")]
    operations = [migrations.RunSQL(FORWARD_SQL, migrations.RunSQL.noop)]
