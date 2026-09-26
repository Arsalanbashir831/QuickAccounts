from django.db import migrations

FORWARD_SQL = r"""
ALTER TABLE erp.journal_entries
    ADD COLUMN row_version bigint NOT NULL DEFAULT 1 CHECK (row_version > 0);

CREATE FUNCTION erp.bump_journal_entry_version()
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

CREATE TRIGGER trg_journal_entry_version
    BEFORE UPDATE ON erp.journal_entries
    FOR EACH ROW
    WHEN (OLD.status = 'draft')
    EXECUTE FUNCTION erp.bump_journal_entry_version();

INSERT INTO identity.permissions(code, description) VALUES
    ('accounting.view', 'View accounting configuration and journals'),
    ('accounting.setup.manage', 'Manage accounting configuration'),
    ('accounting.entry.create', 'Create and edit manual journal drafts'),
    ('accounting.ledger.view', 'View the general ledger')
ON CONFLICT(code) DO NOTHING;

CREATE FUNCTION identity.discover_user_tenants(p_user_id uuid)
RETURNS TABLE(tenant_id uuid, tenant_name text, tenant_role text)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, identity, erp
AS $$
    SELECT t.id, t.name, tm.tenant_role
    FROM identity.tenant_memberships tm
    JOIN erp.tenants t ON t.id = tm.tenant_id
    JOIN identity.users u ON u.id = tm.user_id
    WHERE tm.user_id = p_user_id AND tm.is_active AND u.is_active
    ORDER BY t.name, t.id
$$;
REVOKE ALL ON FUNCTION identity.discover_user_tenants(uuid) FROM PUBLIC;
"""

REVERSE_SQL = r"""
DROP FUNCTION IF EXISTS identity.discover_user_tenants(uuid);
DELETE FROM identity.permissions WHERE code IN (
    'accounting.view', 'accounting.setup.manage',
    'accounting.entry.create', 'accounting.ledger.view'
);
DROP TRIGGER IF EXISTS trg_journal_entry_version ON erp.journal_entries;
DROP FUNCTION IF EXISTS erp.bump_journal_entry_version();
ALTER TABLE erp.journal_entries DROP COLUMN IF EXISTS row_version;
"""


class Migration(migrations.Migration):
    dependencies = [("database", "0001_adopt_authoritative_schema")]
    operations = [migrations.RunSQL(FORWARD_SQL, REVERSE_SQL)]
