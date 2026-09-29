from django.db import migrations

SQL = r"""
CREATE TABLE erp.internal_event_deliveries (
    id uuid PRIMARY KEY DEFAULT uuidv7(),
    company_id uuid NOT NULL REFERENCES erp.companies(id),
    outbox_event_id uuid NOT NULL UNIQUE REFERENCES erp.outbox_events(id),
    event_type text NOT NULL,
    aggregate_type text NOT NULL,
    aggregate_id uuid NOT NULL,
    payload jsonb NOT NULL,
    delivered_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE(company_id,id)
);
CREATE INDEX ix_internal_event_company ON erp.internal_event_deliveries
    (company_id,delivered_at DESC,id);

CREATE TABLE erp.job_artifacts (
    id uuid PRIMARY KEY DEFAULT uuidv7(),
    tenant_id uuid NOT NULL,
    company_id uuid NOT NULL,
    job_id uuid NOT NULL UNIQUE,
    content_type text NOT NULL CHECK(content_type='text/csv'),
    filename text NOT NULL,
    content bytea NOT NULL CHECK(octet_length(content)<=2097152),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY(tenant_id,company_id) REFERENCES erp.companies(tenant_id,id),
    FOREIGN KEY(company_id,job_id) REFERENCES erp.background_jobs(company_id,id),
    UNIQUE(company_id,id)
);
CREATE INDEX ix_job_artifact_company ON erp.job_artifacts(company_id,created_at DESC,id);

ALTER TABLE erp.internal_event_deliveries ENABLE ROW LEVEL SECURITY;
ALTER TABLE erp.internal_event_deliveries FORCE ROW LEVEL SECURITY;
CREATE POLICY internal_event_company ON erp.internal_event_deliveries
    USING (company_id IN (SELECT id FROM erp.companies WHERE tenant_id=erp.current_tenant_id()))
    WITH CHECK (company_id IN
        (SELECT id FROM erp.companies WHERE tenant_id=erp.current_tenant_id()));
ALTER TABLE erp.job_artifacts ENABLE ROW LEVEL SECURITY;
ALTER TABLE erp.job_artifacts FORCE ROW LEVEL SECURITY;
CREATE POLICY job_artifact_tenant ON erp.job_artifacts
    USING (tenant_id=erp.current_tenant_id())
    WITH CHECK (tenant_id=erp.current_tenant_id());

CREATE FUNCTION erp.guard_internal_delivery() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'Internal event delivery is immutable';
END $$;
CREATE TRIGGER trg_internal_delivery_immutable BEFORE UPDATE OR DELETE
ON erp.internal_event_deliveries FOR EACH ROW EXECUTE FUNCTION erp.guard_internal_delivery();
CREATE FUNCTION erp.guard_job_artifact() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'Job artifact is immutable';
END $$;
CREATE TRIGGER trg_job_artifact_immutable BEFORE UPDATE OR DELETE
ON erp.job_artifacts FOR EACH ROW EXECUTE FUNCTION erp.guard_job_artifact();
"""


REVERSE = r"""
DO $$ BEGIN
    IF EXISTS(SELECT 1 FROM erp.internal_event_deliveries)
       OR EXISTS(SELECT 1 FROM erp.job_artifacts) THEN
        RAISE EXCEPTION 'Cannot reverse retained internal job history';
    END IF;
END $$;
DROP TABLE erp.job_artifacts;
DROP TABLE erp.internal_event_deliveries;
DROP FUNCTION erp.guard_job_artifact();
DROP FUNCTION erp.guard_internal_delivery();
"""


class Migration(migrations.Migration):
    dependencies = [("database", "0040_supplier_transfer_provenance")]
    operations = [migrations.RunSQL(SQL, REVERSE)]
