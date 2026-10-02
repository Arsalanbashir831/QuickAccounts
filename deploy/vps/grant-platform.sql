-- Run as a PostgreSQL superuser after creating the dedicated platform login.
-- This credential is used only by superadmin platform commands, never by tenant APIs.
-- Provisioning starts before a tenant ID exists, so tenant-scoped RLS policies
-- cannot authorize its first INSERT. Keep this role separate from the runtime role.
ALTER ROLE quickaccounts_platform BYPASSRLS;
GRANT USAGE ON SCHEMA erp, identity, licensing, platform TO quickaccounts_platform;
GRANT SELECT, INSERT, UPDATE ON
    erp.tenants, erp.companies, erp.company_policy_state,
    erp.company_module_settings, erp.company_audit_events,
    identity.users, identity.tenant_memberships, identity.company_memberships,
    licensing.licenses, licensing.license_terms, licensing.license_events,
    licensing.tenant_product_bindings, licensing.plan_versions, licensing.plan_features,
    platform.operator_audit_events, platform.operator_command_receipts
TO quickaccounts_platform;
GRANT SELECT ON
    erp.currencies, erp.module_definitions, erp.module_dependencies,
    licensing.products, licensing.plans
TO quickaccounts_platform;
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA erp, identity, licensing
TO quickaccounts_platform;
