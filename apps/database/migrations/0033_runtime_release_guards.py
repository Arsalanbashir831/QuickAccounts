from django.db import migrations

SQL = r"""
-- Preserve the existing tenant, membership, permission and entitlement checks while
-- allowing the coordinator locks without granting runtime licensing/policy DML.
ALTER FUNCTION erp.assert_company_write(uuid,uuid,text,text) SECURITY DEFINER;
ALTER FUNCTION erp.assert_company_write(uuid,uuid,text,text)
    SET search_path = pg_catalog, erp, identity, licensing, pg_temp;

CREATE FUNCTION erp.append_accounting_audit(
    p_company uuid, p_action text, p_object_type text, p_object_id uuid,
    p_data jsonb, p_request_id text
) RETURNS void LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, erp, identity, pg_temp AS $$
DECLARE
    v_permission text;
    v_state text;
BEGIN
    v_permission := CASE p_action
        WHEN 'accounting.entry.posted' THEN 'accounting.entry.post'
        WHEN 'accounting.entry.reversed' THEN 'accounting.entry.reverse'
        WHEN 'accounting.period.closed' THEN 'accounting.period.close'
        WHEN 'accounting.period.reopened' THEN 'accounting.period.reopen'
    END;
    IF v_permission IS NULL OR identity.current_user_id() IS NULL
       OR NOT identity.has_company_permission(p_company,v_permission) THEN
        RAISE EXCEPTION 'Accounting audit permission denied' USING ERRCODE='42501';
    END IF;
    IF p_action IN ('accounting.entry.posted','accounting.entry.reversed')
       AND p_object_type='journal_entry' THEN
        SELECT status INTO v_state FROM erp.journal_entries
        WHERE company_id=p_company AND id=p_object_id;
        IF v_state IS DISTINCT FROM 'posted' THEN
            RAISE EXCEPTION 'Audit requires posted entry' USING ERRCODE='42501';
        END IF;
    ELSIF p_action IN ('accounting.period.closed','accounting.period.reopened')
          AND p_object_type='fiscal_period' THEN
        SELECT state INTO v_state FROM erp.fiscal_periods
        WHERE company_id=p_company AND id=p_object_id;
        IF v_state IS DISTINCT FROM
            (CASE p_action WHEN 'accounting.period.closed' THEN 'closed' ELSE 'open' END) THEN
            RAISE EXCEPTION 'Audit requires matching period state' USING ERRCODE='42501';
        END IF;
    ELSE
        RAISE EXCEPTION 'Unsupported accounting audit object' USING ERRCODE='42501';
    END IF;
    INSERT INTO erp.company_audit_events(
        tenant_id,company_id,actor_user_id,action,object_type,object_id,new_data,request_id
    ) VALUES (
        erp.current_tenant_id(),p_company,identity.current_user_id(),p_action,
        p_object_type,p_object_id::text,p_data,p_request_id
    );
END $$;
REVOKE ALL ON FUNCTION erp.append_accounting_audit(uuid,text,text,uuid,jsonb,text)
FROM PUBLIC;
"""

REVERSE = r"""
DROP FUNCTION erp.append_accounting_audit(uuid,text,text,uuid,jsonb,text);
ALTER FUNCTION erp.assert_company_write(uuid,uuid,text,text) SECURITY INVOKER;
ALTER FUNCTION erp.assert_company_write(uuid,uuid,text,text) RESET search_path;
"""


class Migration(migrations.Migration):
    dependencies = [("database", "0032_manufacturing_execution")]
    operations = [migrations.RunSQL(SQL, REVERSE)]
