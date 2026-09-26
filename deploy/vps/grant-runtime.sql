-- Apply as the schema owner after migrations. Keep platform/operator objects inaccessible.
GRANT USAGE ON SCHEMA erp, identity, licensing TO quickaccounts_runtime;
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

ALTER DEFAULT PRIVILEGES IN SCHEMA erp
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO quickaccounts_runtime;
ALTER DEFAULT PRIVILEGES IN SCHEMA identity
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO quickaccounts_runtime;
ALTER DEFAULT PRIVILEGES IN SCHEMA licensing
    GRANT SELECT ON TABLES TO quickaccounts_runtime;
ALTER DEFAULT PRIVILEGES IN SCHEMA erp, identity, licensing
    GRANT EXECUTE ON FUNCTIONS TO quickaccounts_runtime;

REVOKE ALL ON SCHEMA platform FROM quickaccounts_runtime;

