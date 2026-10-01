-- Apply as the schema owner after migrations. Keep platform/operator objects inaccessible.
GRANT USAGE ON SCHEMA erp, identity, licensing TO quickaccounts_runtime;
-- Database-backed Django sessions are framework state, not ERP policy/audit data.
GRANT USAGE ON SCHEMA public TO quickaccounts_runtime;
GRANT SELECT, INSERT, UPDATE, DELETE ON public.django_session TO quickaccounts_runtime;
-- Django's startup migration check reads this catalog; the runtime must not mutate it.
GRANT SELECT ON public.django_migrations TO quickaccounts_runtime;
-- The separate Django admin UI reads its recent-action log and built-in Group
-- model. Keep these framework grants explicit rather than opening all public tables.
GRANT SELECT, INSERT ON public.django_admin_log TO quickaccounts_runtime;
GRANT SELECT ON public.django_content_type, public.auth_permission TO quickaccounts_runtime;
GRANT SELECT, INSERT, UPDATE, DELETE ON
    public.auth_group,
    public.auth_group_permissions
TO quickaccounts_runtime;
GRANT USAGE, SELECT ON SEQUENCE
    public.django_admin_log_id_seq,
    public.auth_group_id_seq
TO quickaccounts_runtime;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA erp TO quickaccounts_runtime;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA identity TO quickaccounts_runtime;
GRANT SELECT ON ALL TABLES IN SCHEMA licensing TO quickaccounts_runtime;
GRANT INSERT, UPDATE ON
    licensing.license_activations,
    licensing.billing_orders,
    licensing.billing_events
TO quickaccounts_runtime;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA erp, identity, licensing
TO quickaccounts_runtime;
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA erp, identity, licensing
TO quickaccounts_runtime;

-- Company policy mutations must pass through erp.set_company_modules() or the guarded
-- provisioning/authorization triggers. The runtime can read policy but cannot bypass the
-- permission, license, dependency, revision, audit, and outbox protocol with direct DML.
REVOKE INSERT, UPDATE, DELETE ON
    erp.company_policy_state,
    erp.company_module_settings,
    erp.company_audit_events
FROM quickaccounts_runtime;

ALTER DEFAULT PRIVILEGES IN SCHEMA erp
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO quickaccounts_runtime;
ALTER DEFAULT PRIVILEGES IN SCHEMA identity
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO quickaccounts_runtime;
ALTER DEFAULT PRIVILEGES IN SCHEMA licensing
    GRANT SELECT ON TABLES TO quickaccounts_runtime;
ALTER DEFAULT PRIVILEGES IN SCHEMA erp, identity, licensing
    GRANT EXECUTE ON FUNCTIONS TO quickaccounts_runtime;

REVOKE ALL ON SCHEMA platform FROM quickaccounts_runtime;
