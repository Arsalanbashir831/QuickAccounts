-- ERP Accounting backend extension for the existing erp-accounting-schema.sql
-- PostgreSQL 18. Run once after the baseline file on a backup/clone first.
-- Review existing data before the inventory projection backfill and new CHECK constraints.
BEGIN;

DO $$
BEGIN
 IF to_regclass('erp.companies') IS NULL OR to_regclass('licensing.licenses') IS NULL THEN
   RAISE EXCEPTION 'Run erp-accounting-schema.sql before this backend upgrade';
 END IF;
END $$;

-- Backend extension, version 2. One-time migration from the supplied baseline.
-- Run the full file only on an empty database; see the companion README.
CREATE SCHEMA identity;
CREATE SCHEMA platform;

CREATE TABLE platform.schema_releases (
    version text PRIMARY KEY,
    installed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    description text NOT NULL
);
INSERT INTO platform.schema_releases VALUES
 ('erp-backend-2', clock_timestamp(), 'Django backend persistence and baseline hardening');

CREATE TABLE identity.users (
    id uuid PRIMARY KEY DEFAULT uuidv7(),
    email varchar(254) NOT NULL CHECK (email = lower(btrim(email)) AND position('@' in email) > 1),
    password varchar(128) NOT NULL DEFAULT '!', -- Django encoded password, never plaintext
    first_name varchar(150) NOT NULL DEFAULT '',
    last_name varchar(150) NOT NULL DEFAULT '',
    is_active boolean NOT NULL DEFAULT true,
    is_staff boolean NOT NULL DEFAULT false,
    is_superuser boolean NOT NULL DEFAULT false,
    last_login timestamptz,
    date_joined timestamptz NOT NULL DEFAULT clock_timestamp(),
    auth_revision bigint NOT NULL DEFAULT 1 CHECK (auth_revision > 0),
    UNIQUE (email)
);
CREATE TABLE identity.tenant_memberships (
    id uuid PRIMARY KEY DEFAULT uuidv7(),
    tenant_id uuid NOT NULL REFERENCES erp.tenants(id),
    user_id uuid NOT NULL REFERENCES identity.users(id),
    tenant_role text NOT NULL DEFAULT 'member' CHECK (tenant_role IN ('owner','admin','member')),
    is_active boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (tenant_id,user_id), UNIQUE (tenant_id,id)
);
CREATE INDEX ix_tenant_membership_user ON identity.tenant_memberships(user_id,tenant_id) WHERE is_active;
CREATE TABLE identity.company_memberships (
    id uuid PRIMARY KEY DEFAULT uuidv7(),
    tenant_id uuid NOT NULL,
    company_id uuid NOT NULL,
    user_id uuid NOT NULL,
    is_active boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (company_id,user_id), UNIQUE (company_id,id),
    FOREIGN KEY (tenant_id,company_id) REFERENCES erp.companies(tenant_id,id),
    FOREIGN KEY (tenant_id,user_id) REFERENCES identity.tenant_memberships(tenant_id,user_id)
);
CREATE INDEX ix_company_membership_user ON identity.company_memberships(user_id,company_id) WHERE is_active;
CREATE TABLE identity.permissions (
    code text PRIMARY KEY CHECK (code <> ''),
    description text NOT NULL
);
CREATE TABLE identity.roles (
    id uuid PRIMARY KEY DEFAULT uuidv7(),
    company_id uuid NOT NULL REFERENCES erp.companies(id),
    code text NOT NULL,
    name text NOT NULL,
    UNIQUE (company_id,code), UNIQUE (company_id,id)
);
CREATE TABLE identity.role_permissions (
    company_id uuid NOT NULL,
    role_id uuid NOT NULL,
    permission_code text NOT NULL REFERENCES identity.permissions(code),
    PRIMARY KEY (company_id,role_id,permission_code),
    FOREIGN KEY (company_id,role_id) REFERENCES identity.roles(company_id,id)
);
CREATE TABLE identity.company_role_assignments (
    company_id uuid NOT NULL,
    membership_id uuid NOT NULL,
    role_id uuid NOT NULL,
    PRIMARY KEY (company_id,membership_id,role_id),
    FOREIGN KEY (company_id,membership_id) REFERENCES identity.company_memberships(company_id,id),
    FOREIGN KEY (company_id,role_id) REFERENCES identity.roles(company_id,id)
);
CREATE TABLE identity.service_accounts (
    id uuid PRIMARY KEY DEFAULT uuidv7(),
    tenant_id uuid NOT NULL REFERENCES erp.tenants(id),
    company_id uuid,
    name text NOT NULL,
    credential_hash bytea NOT NULL CHECK (octet_length(credential_hash)=32),
    is_active boolean NOT NULL DEFAULT true,
    expires_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (tenant_id,id), UNIQUE (credential_hash),
    FOREIGN KEY (tenant_id,company_id) REFERENCES erp.companies(tenant_id,id)
);
CREATE TABLE identity.service_account_permissions (
    tenant_id uuid NOT NULL,
    service_account_id uuid NOT NULL,
    permission_code text NOT NULL REFERENCES identity.permissions(code),
    PRIMARY KEY (tenant_id,service_account_id,permission_code),
    FOREIGN KEY (tenant_id,service_account_id) REFERENCES identity.service_accounts(tenant_id,id)
);
CREATE OR REPLACE FUNCTION identity.current_user_id() RETURNS uuid LANGUAGE sql STABLE AS $$
 SELECT nullif(current_setting('app.user_id',true),'')::uuid
$$;
CREATE OR REPLACE FUNCTION identity.current_service_id() RETURNS uuid LANGUAGE sql STABLE AS $$
 SELECT nullif(current_setting('app.service_id',true),'')::uuid
$$;
-- Session context is trusted backend input, not a client-supplied identity.
CREATE FUNCTION identity.has_company_permission(p_company uuid,p_permission text)
RETURNS boolean LANGUAGE sql STABLE AS $$
 SELECT EXISTS (
   SELECT 1 FROM erp.companies c
   JOIN identity.company_memberships cm ON cm.company_id=c.id
   JOIN identity.tenant_memberships tm ON tm.tenant_id=c.tenant_id AND tm.user_id=cm.user_id
   JOIN identity.users u ON u.id=cm.user_id
   WHERE c.id=p_company AND c.tenant_id=erp.current_tenant_id()
     AND cm.user_id=identity.current_user_id() AND identity.current_service_id() IS NULL
     AND cm.is_active AND tm.is_active AND u.is_active
     AND (tm.tenant_role='owner' OR EXISTS (
       SELECT 1 FROM identity.company_role_assignments a
       JOIN identity.role_permissions rp ON rp.company_id=a.company_id AND rp.role_id=a.role_id
       WHERE a.company_id=c.id AND a.membership_id=cm.id AND rp.permission_code=p_permission
     ))
 ) OR EXISTS (
   SELECT 1 FROM identity.service_accounts s
   JOIN identity.service_account_permissions sp ON sp.tenant_id=s.tenant_id AND sp.service_account_id=s.id
   JOIN erp.companies c ON c.tenant_id=s.tenant_id
   WHERE c.id=p_company AND c.tenant_id=erp.current_tenant_id()
     AND s.id=identity.current_service_id() AND identity.current_user_id() IS NULL
     AND s.is_active AND (s.expires_at IS NULL OR statement_timestamp()<s.expires_at)
     AND (s.company_id IS NULL OR s.company_id=c.id) AND sp.permission_code=p_permission
 )
$$;

CREATE TABLE erp.module_definitions (
    code text PRIMARY KEY,
    name text NOT NULL,
    license_feature_code text,
    is_required boolean NOT NULL DEFAULT false,
    release_status text NOT NULL DEFAULT 'available' CHECK (release_status IN ('planned','available','retired'))
);
CREATE TABLE erp.module_dependencies (
    module_code text NOT NULL REFERENCES erp.module_definitions(code),
    required_module_code text NOT NULL REFERENCES erp.module_definitions(code),
    PRIMARY KEY (module_code,required_module_code),
    CHECK (module_code<>required_module_code)
);
INSERT INTO erp.module_definitions VALUES
 ('core','Company administration',NULL,true,'available'),
 ('accounting','Accounting',NULL,true,'available'),
 ('tax_calculation','Tax calculation',NULL,true,'available'),
 ('sales','Sales','module.sales',false,'available'),
 ('purchasing','Purchasing','module.purchasing',false,'available'),
 ('payments','Payments','module.payments',false,'available'),
 ('inventory','Inventory','module.inventory',false,'available'),
 ('manufacturing','Manufacturing','module.manufacturing',false,'available'),
 ('ecommerce','E-commerce','module.ecommerce',false,'available'),
 ('tax_filing','Tax filing','module.tax_filing',false,'available'),
 ('advanced_reporting','Advanced reporting','module.advanced_reporting',false,'available'),
 ('retail_pos','Retail POS','module.retail_pos',false,'planned');
INSERT INTO erp.module_dependencies VALUES
 ('sales','accounting'),('sales','tax_calculation'),
 ('purchasing','accounting'),('purchasing','tax_calculation'),
 ('payments','accounting'),('payments','tax_calculation'),
 ('inventory','accounting'),('manufacturing','inventory'),
 ('manufacturing','accounting'),('ecommerce','sales'),
 ('tax_filing','accounting'),('tax_filing','tax_calculation'),
 ('advanced_reporting','accounting'),('retail_pos','sales'),('retail_pos','payments');

CREATE TABLE erp.company_policy_state (
    company_id uuid PRIMARY KEY REFERENCES erp.companies(id),
    revision bigint NOT NULL DEFAULT 1 CHECK (revision>0),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE erp.company_module_settings (
    company_id uuid NOT NULL REFERENCES erp.companies(id),
    module_code text NOT NULL REFERENCES erp.module_definitions(code),
    mode text NOT NULL DEFAULT 'disabled' CHECK (mode IN ('enabled','read_only','disabled')),
    changed_by uuid REFERENCES identity.users(id),
    changed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (company_id,module_code)
);
CREATE TABLE erp.company_audit_events (
    id uuid PRIMARY KEY DEFAULT uuidv7(),
    tenant_id uuid NOT NULL,
    company_id uuid NOT NULL,
    actor_user_id uuid REFERENCES identity.users(id),
    actor_service_id uuid REFERENCES identity.service_accounts(id),
    action text NOT NULL,
    object_type text NOT NULL,
    object_id text,
    old_data jsonb,
    new_data jsonb,
    reason text,
    request_id text,
    occurred_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK (actor_user_id IS NULL OR actor_service_id IS NULL),
    FOREIGN KEY (tenant_id,company_id) REFERENCES erp.companies(tenant_id,id),
    FOREIGN KEY (tenant_id,actor_service_id) REFERENCES identity.service_accounts(tenant_id,id)
);
CREATE INDEX ix_company_audit_timeline ON erp.company_audit_events(company_id,occurred_at DESC,id DESC);
CREATE FUNCTION erp.reject_audit_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'Audit records are append-only'; END $$;
CREATE TRIGGER trg_audit_append_only BEFORE UPDATE OR DELETE ON erp.company_audit_events
 FOR EACH ROW EXECUTE FUNCTION erp.reject_audit_mutation();

-- Revision may advance by more than one in a multi-row batch; treat it as an opaque ETag.
CREATE FUNCTION erp.guard_module_setting() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE v_company uuid; v_required boolean; v_release text;
BEGIN
 v_company := CASE WHEN TG_OP='DELETE' THEN OLD.company_id ELSE NEW.company_id END;
 PERFORM 1 FROM erp.company_policy_state WHERE company_id=v_company FOR UPDATE;
 IF NOT FOUND THEN RAISE EXCEPTION 'Company policy state is missing'; END IF;
 IF TG_OP='UPDATE' AND (NEW.company_id,NEW.module_code) IS DISTINCT FROM (OLD.company_id,OLD.module_code) THEN
   RAISE EXCEPTION 'Module setting identity is immutable';
 END IF;
 SELECT is_required,release_status INTO v_required,v_release FROM erp.module_definitions
 WHERE code=CASE WHEN TG_OP='DELETE' THEN OLD.module_code ELSE NEW.module_code END;
 IF v_required AND (TG_OP='DELETE' OR NEW.mode<>'enabled') THEN
   RAISE EXCEPTION 'Required modules must remain enabled';
 END IF;
 IF TG_OP<>'DELETE' AND NEW.mode='enabled' AND v_release<>'available' THEN
   RAISE EXCEPTION 'Module is not available in this release';
 END IF;
 UPDATE erp.company_policy_state SET revision=revision+1,updated_at=clock_timestamp() WHERE company_id=v_company;
 INSERT INTO erp.company_audit_events(tenant_id,company_id,actor_user_id,actor_service_id,action,object_type,object_id,old_data,new_data,reason,request_id)
 SELECT c.tenant_id,v_company,identity.current_user_id(),identity.current_service_id(),'module.'||lower(TG_OP),'module',
   CASE WHEN TG_OP='DELETE' THEN OLD.module_code ELSE NEW.module_code END,
   CASE WHEN TG_OP='INSERT' THEN NULL ELSE to_jsonb(OLD) END,
   CASE WHEN TG_OP='DELETE' THEN NULL ELSE to_jsonb(NEW) END,
   nullif(current_setting('app.change_reason',true),''),nullif(current_setting('app.request_id',true),'')
 FROM erp.companies c WHERE c.id=v_company;
 IF TG_OP='DELETE' THEN RETURN OLD; END IF;
 NEW.changed_at:=clock_timestamp(); NEW.changed_by:=identity.current_user_id(); RETURN NEW;
END $$;
CREATE TRIGGER trg_module_setting_guard BEFORE INSERT OR UPDATE OR DELETE ON erp.company_module_settings
 FOR EACH ROW EXECUTE FUNCTION erp.guard_module_setting();
CREATE FUNCTION erp.check_module_dependencies() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE v_company uuid;
BEGIN
 v_company:=CASE WHEN TG_OP='DELETE' THEN OLD.company_id ELSE NEW.company_id END;
 IF EXISTS (
   SELECT 1 FROM erp.company_module_settings s
   JOIN erp.module_dependencies d ON d.module_code=s.module_code
   LEFT JOIN erp.company_module_settings r ON r.company_id=s.company_id AND r.module_code=d.required_module_code
   WHERE s.company_id=v_company AND s.mode='enabled' AND coalesce(r.mode,'disabled')<>'enabled'
 ) THEN RAISE EXCEPTION 'Enabled module has a disabled/read-only dependency'; END IF;
 RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER trg_module_dependencies_valid AFTER INSERT OR UPDATE OR DELETE ON erp.company_module_settings
 DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION erp.check_module_dependencies();
CREATE FUNCTION erp.reject_module_cycle() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF EXISTS (WITH RECURSIVE reach(a,b) AS (
   SELECT module_code,required_module_code FROM erp.module_dependencies
   UNION SELECT r.a,d.required_module_code FROM reach r JOIN erp.module_dependencies d ON d.module_code=r.b
 ) SELECT 1 FROM reach WHERE a=b) THEN RAISE EXCEPTION 'Module dependencies contain a cycle'; END IF;
 RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER trg_module_catalog_acyclic AFTER INSERT OR UPDATE ON erp.module_dependencies
 DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION erp.reject_module_cycle();
CREATE FUNCTION erp.provision_company_policy() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 INSERT INTO erp.company_policy_state(company_id) VALUES(NEW.id);
 INSERT INTO erp.company_module_settings(company_id,module_code,mode)
 SELECT NEW.id,code,CASE WHEN is_required THEN 'enabled' ELSE 'disabled' END FROM erp.module_definitions;
 RETURN NEW;
END $$;
CREATE TRIGGER trg_company_policy_provision AFTER INSERT ON erp.companies
 FOR EACH ROW EXECUTE FUNCTION erp.provision_company_policy();
-- Existing companies start with required modules enabled and optional modules disabled.
INSERT INTO erp.company_policy_state(company_id) SELECT id FROM erp.companies;
INSERT INTO erp.company_module_settings(company_id,module_code,mode)
 SELECT c.id,m.code,CASE WHEN m.is_required THEN 'enabled' ELSE 'disabled' END
 FROM erp.companies c CROSS JOIN erp.module_definitions m;

CREATE FUNCTION erp.bump_company_authorization() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE v_company uuid;
BEGIN
 v_company:=CASE WHEN TG_OP='DELETE' THEN OLD.company_id ELSE NEW.company_id END;
 UPDATE erp.company_policy_state SET revision=revision+1,updated_at=clock_timestamp() WHERE company_id=v_company;
 IF TG_OP='DELETE' THEN RETURN OLD; END IF; RETURN NEW;
END $$;
CREATE TRIGGER trg_membership_policy BEFORE INSERT OR UPDATE OR DELETE ON identity.company_memberships
 FOR EACH ROW EXECUTE FUNCTION erp.bump_company_authorization();
CREATE TRIGGER trg_role_grant_policy BEFORE INSERT OR UPDATE OR DELETE ON identity.role_permissions
 FOR EACH ROW EXECUTE FUNCTION erp.bump_company_authorization();
CREATE TRIGGER trg_role_assignment_policy BEFORE INSERT OR UPDATE OR DELETE ON identity.company_role_assignments
 FOR EACH ROW EXECUTE FUNCTION erp.bump_company_authorization();
CREATE VIEW erp.v_company_module_configuration WITH (security_invoker=true) AS
 SELECT c.tenant_id,c.id AS company_id,p.revision,m.code AS module_code,m.name,m.is_required,m.release_status,
        coalesce(s.mode,'disabled') AS configured_mode,m.license_feature_code
 FROM erp.companies c JOIN erp.company_policy_state p ON p.company_id=c.id
 CROSS JOIN erp.module_definitions m LEFT JOIN erp.company_module_settings s ON s.company_id=c.id AND s.module_code=m.code;

ALTER TABLE licensing.licenses ADD CONSTRAINT uq_license_tenant_product_id UNIQUE(tenant_id,product_id,id);
ALTER TABLE licensing.licenses ADD COLUMN lock_stub boolean NOT NULL DEFAULT true CHECK(lock_stub);
ALTER TABLE licensing.plan_versions ADD CONSTRAINT ck_fixed_plan_term_complete
 CHECK(term_unit='lifetime' OR (term_count IS NOT NULL AND term_count>0));
ALTER TABLE licensing.license_terms ADD CONSTRAINT ck_fixed_license_term_complete
 CHECK(term_unit_snapshot='lifetime' OR
       (term_count_snapshot IS NOT NULL AND term_count_snapshot>0 AND expires_at IS NOT NULL AND expires_at>starts_at));
CREATE TABLE licensing.tenant_product_bindings (
    tenant_id uuid NOT NULL REFERENCES erp.tenants(id),
    product_id uuid NOT NULL REFERENCES licensing.products(id),
    license_id uuid,
    entitlement_revision bigint NOT NULL DEFAULT 1 CHECK(entitlement_revision>0),
    billing_timezone text NOT NULL DEFAULT 'UTC',
    billing_anchor_day smallint CHECK(billing_anchor_day BETWEEN 1 AND 31),
    month_end_anchor boolean NOT NULL DEFAULT false,
    lock_stub boolean NOT NULL DEFAULT true CHECK(lock_stub),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY(tenant_id,product_id),
    FOREIGN KEY(tenant_id,product_id,license_id) REFERENCES licensing.licenses(tenant_id,product_id,id)
);
-- Do not silently select among multiple licenses. Operator binds explicitly after migration.
INSERT INTO licensing.tenant_product_bindings(tenant_id,product_id)
 SELECT DISTINCT tenant_id,product_id FROM licensing.licenses;
CREATE FUNCTION licensing.ensure_binding_coordinator() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 INSERT INTO licensing.tenant_product_bindings(tenant_id,product_id) VALUES(NEW.tenant_id,NEW.product_id)
 ON CONFLICT DO NOTHING; RETURN NEW;
END $$;
CREATE TRIGGER trg_license_coordinator AFTER INSERT ON licensing.licenses
 FOR EACH ROW EXECUTE FUNCTION licensing.ensure_binding_coordinator();
CREATE FUNCTION licensing.touch_binding() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF (NEW.tenant_id,NEW.product_id) IS DISTINCT FROM (OLD.tenant_id,OLD.product_id) THEN
   RAISE EXCEPTION 'Binding scope is immutable'; END IF;
 NEW.entitlement_revision:=OLD.entitlement_revision+1; NEW.updated_at:=clock_timestamp(); RETURN NEW;
END $$;
CREATE TRIGGER trg_binding_revision BEFORE UPDATE ON licensing.tenant_product_bindings
 FOR EACH ROW EXECUTE FUNCTION licensing.touch_binding();
CREATE FUNCTION licensing.invalidate_entitlement() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE v_product uuid;
BEGIN
 IF TG_TABLE_NAME='licenses' THEN
   IF (NEW.tenant_id,NEW.product_id) IS DISTINCT FROM (OLD.tenant_id,OLD.product_id) THEN
     RAISE EXCEPTION 'License scope is immutable'; END IF;
   IF NEW.status IS NOT DISTINCT FROM OLD.status THEN RETURN NEW; END IF;
   v_product:=NEW.product_id;
 ELSE SELECT product_id INTO v_product FROM licensing.licenses WHERE id=NEW.license_id;
 END IF;
 UPDATE licensing.tenant_product_bindings SET entitlement_revision=entitlement_revision+1
 WHERE tenant_id=NEW.tenant_id AND product_id=v_product;
 RETURN NEW;
END $$;
CREATE TRIGGER trg_license_status_revision AFTER UPDATE ON licensing.licenses
 FOR EACH ROW EXECUTE FUNCTION licensing.invalidate_entitlement();
CREATE TRIGGER trg_license_term_revision AFTER INSERT ON licensing.license_terms
 FOR EACH ROW EXECUTE FUNCTION licensing.invalidate_entitlement();

CREATE OR REPLACE FUNCTION licensing.guard_license_term_insert() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE v_license licensing.licenses%ROWTYPE; v_plan licensing.plan_versions%ROWTYPE;
BEGIN
 SELECT * INTO v_license FROM licensing.licenses WHERE tenant_id=NEW.tenant_id AND id=NEW.license_id;
 IF NOT FOUND THEN RAISE EXCEPTION 'License is missing'; END IF;
 PERFORM 1 FROM licensing.tenant_product_bindings WHERE tenant_id=NEW.tenant_id AND product_id=v_license.product_id FOR UPDATE;
 IF NOT FOUND THEN RAISE EXCEPTION 'Entitlement coordinator is missing'; END IF;
 SELECT * INTO v_license FROM licensing.licenses WHERE tenant_id=NEW.tenant_id AND id=NEW.license_id FOR UPDATE;
 IF v_license.status='revoked' THEN RAISE EXCEPTION 'License is revoked'; END IF;
 SELECT * INTO v_plan FROM licensing.plan_versions WHERE id=NEW.plan_version_id FOR SHARE;
 IF NOT FOUND OR v_plan.published_at IS NULL OR v_plan.product_id<>v_license.product_id
 OR NEW.term_unit_snapshot<>v_plan.term_unit OR NEW.term_count_snapshot IS DISTINCT FROM v_plan.term_count
 OR NEW.max_activations_snapshot<>v_plan.max_activations THEN
   RAISE EXCEPTION 'Term must match a published plan for the same product'; END IF;
 IF EXISTS(SELECT 1 FROM licensing.license_terms t WHERE t.license_id=NEW.license_id
 AND tstzrange(t.starts_at,t.expires_at,'[)') && tstzrange(NEW.starts_at,NEW.expires_at,'[)')) THEN
   RAISE EXCEPTION 'License term overlaps an existing term'; END IF;
 RETURN NEW;
END $$;
CREATE OR REPLACE FUNCTION licensing.guard_license_activation() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE v_license licensing.licenses%ROWTYPE; v_limit integer; v_count bigint; v_now timestamptz;
BEGIN
 IF TG_OP='UPDATE' THEN
   IF (NEW.tenant_id,NEW.license_id,NEW.device_fingerprint_hash,NEW.device_public_key,NEW.activated_at)
   IS DISTINCT FROM (OLD.tenant_id,OLD.license_id,OLD.device_fingerprint_hash,OLD.device_public_key,OLD.activated_at) THEN
     RAISE EXCEPTION 'Activation identity is immutable'; END IF;
   -- Heartbeats do not consume another slot, and cleanup works after expiration/revocation.
   IF NEW.deactivated_at IS NOT NULL OR
      (NEW.deactivated_at IS NOT DISTINCT FROM OLD.deactivated_at) THEN RETURN NEW; END IF;
 END IF;
 SELECT * INTO v_license FROM licensing.licenses WHERE tenant_id=NEW.tenant_id AND id=NEW.license_id;
 IF NOT FOUND THEN RAISE EXCEPTION 'License is missing'; END IF;
 PERFORM 1 FROM licensing.tenant_product_bindings WHERE tenant_id=NEW.tenant_id AND product_id=v_license.product_id FOR UPDATE;
 SELECT * INTO v_license FROM licensing.licenses WHERE id=NEW.license_id AND tenant_id=NEW.tenant_id FOR UPDATE;
 v_now:=clock_timestamp();
 IF v_license.status<>'active' THEN RAISE EXCEPTION 'License is not active'; END IF;
 SELECT max_activations_snapshot INTO v_limit FROM licensing.license_terms
 WHERE license_id=NEW.license_id AND starts_at<=v_now AND (expires_at IS NULL OR v_now<expires_at);
 IF v_limit IS NULL THEN RAISE EXCEPTION 'No valid license term'; END IF;
 SELECT count(*) INTO v_count FROM licensing.license_activations
 WHERE license_id=NEW.license_id AND deactivated_at IS NULL AND id<>NEW.id;
 IF NEW.deactivated_at IS NULL AND v_count>=v_limit THEN RAISE EXCEPTION 'Activation quota exceeded'; END IF;
 RETURN NEW;
END $$;
-- Correct the draft-plan DELETE return path in the baseline trigger.
CREATE OR REPLACE FUNCTION licensing.guard_published_plan_version() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF OLD.published_at IS NOT NULL THEN RAISE EXCEPTION 'Published plans are immutable'; END IF;
 IF TG_OP='DELETE' THEN RETURN OLD; END IF; RETURN NEW;
END $$;
CREATE FUNCTION licensing.validate_signed_grant() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE t licensing.license_terms%ROWTYPE; k licensing.signing_keys%ROWTYPE; v_now timestamptz;
BEGIN
 SELECT * INTO t FROM licensing.license_terms WHERE tenant_id=NEW.tenant_id AND license_id=NEW.license_id AND id=NEW.license_term_id;
 SELECT * INTO k FROM licensing.signing_keys WHERE key_id=NEW.key_id;
 v_now:=clock_timestamp();
 IF t.id IS NULL OR k.key_id IS NULL OR k.revoked_at IS NOT NULL OR NEW.issued_at>v_now
 OR NEW.issued_at<t.starts_at OR NEW.issued_at<k.valid_from OR
 (t.expires_at IS NOT NULL AND NEW.issued_at>=t.expires_at) OR
 (k.valid_to IS NOT NULL AND NEW.issued_at>=k.valid_to) OR
 NEW.grant_valid_until IS NULL OR NEW.grant_valid_until<=NEW.issued_at OR
 (t.expires_at IS NOT NULL AND NEW.grant_valid_until>t.expires_at) OR
 (k.valid_to IS NOT NULL AND NEW.grant_valid_until>k.valid_to) THEN
   RAISE EXCEPTION 'Grant validity exceeds its term or signing key'; END IF;
 IF NOT EXISTS(SELECT 1 FROM licensing.licenses WHERE id=NEW.license_id AND tenant_id=NEW.tenant_id AND status='active') THEN
   RAISE EXCEPTION 'Cannot sign for an inactive license'; END IF;
 IF NEW.activation_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM licensing.license_activations
 WHERE id=NEW.activation_id AND license_id=NEW.license_id AND tenant_id=NEW.tenant_id AND deactivated_at IS NULL) THEN
   RAISE EXCEPTION 'Grant activation is inactive'; END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER trg_grant_validity BEFORE INSERT ON licensing.signed_grants
 FOR EACH ROW EXECUTE FUNCTION licensing.validate_signed_grant();

CREATE TABLE licensing.billing_orders (
    id uuid PRIMARY KEY DEFAULT uuidv7(),
    tenant_id uuid NOT NULL REFERENCES erp.tenants(id),
    license_id uuid NOT NULL,
    plan_version_id uuid NOT NULL REFERENCES licensing.plan_versions(id),
    order_key varchar(128) NOT NULL,
    currency_code char(3) NOT NULL REFERENCES erp.currencies(code),
    amount numeric(20,6) NOT NULL CHECK(amount>=0),
    provider text NOT NULL,
    provider_order_reference text,
    status text NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','paid','fulfilled','cancelled','refunded')),
    fulfilled_term_id uuid,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    fulfilled_at timestamptz,
    UNIQUE(tenant_id,id), UNIQUE(tenant_id,order_key), UNIQUE(provider,provider_order_reference),
    FOREIGN KEY(tenant_id,license_id) REFERENCES licensing.licenses(tenant_id,id),
    FOREIGN KEY(tenant_id,license_id,fulfilled_term_id) REFERENCES licensing.license_terms(tenant_id,license_id,id),
    CHECK(status<>'fulfilled' OR (fulfilled_term_id IS NOT NULL AND fulfilled_at IS NOT NULL))
);
CREATE TABLE licensing.billing_events (
    id uuid PRIMARY KEY DEFAULT uuidv7(),
    tenant_id uuid NOT NULL REFERENCES erp.tenants(id),
    billing_order_id uuid NOT NULL,
    provider text NOT NULL,
    external_event_id text NOT NULL,
    payload_hash bytea NOT NULL CHECK(octet_length(payload_hash)=32),
    event_type text NOT NULL,
    received_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    verified_at timestamptz NOT NULL,
    event_data jsonb NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE(provider,external_event_id),
    FOREIGN KEY(tenant_id,billing_order_id) REFERENCES licensing.billing_orders(tenant_id,id)
);
CREATE TRIGGER trg_billing_event_immutable BEFORE UPDATE OR DELETE ON licensing.billing_events
 FOR EACH ROW EXECUTE FUNCTION licensing.reject_issued_record_change();
CREATE VIEW licensing.v_bound_entitlements WITH(security_invoker=true) AS
 SELECT b.tenant_id,b.product_id,b.license_id,b.entitlement_revision,l.status,
        t.id AS license_term_id,t.plan_version_id,t.starts_at,t.expires_at,
        t.support_valid_until,t.updates_valid_until
 FROM licensing.tenant_product_bindings b JOIN licensing.licenses l ON l.id=b.license_id AND l.tenant_id=b.tenant_id
 JOIN licensing.license_terms t ON t.license_id=l.id AND t.tenant_id=l.tenant_id
 WHERE l.status='active' AND t.starts_at<=statement_timestamp()
 AND (t.expires_at IS NULL OR statement_timestamp()<t.expires_at);

-- Same transaction as each business command; no remote license request is needed.
CREATE FUNCTION erp.assert_company_write(p_company uuid,p_product uuid,p_module text,p_permission text)
RETURNS uuid LANGUAGE plpgsql AS $$
DECLARE v_license uuid; v_plan uuid; v_status text; v_now timestamptz;
BEGIN
 IF NOT EXISTS(SELECT 1 FROM erp.companies WHERE id=p_company AND tenant_id=erp.current_tenant_id() AND is_active) THEN
   RAISE EXCEPTION 'Company is not accessible/active' USING ERRCODE='42501'; END IF;
 SELECT license_id INTO v_license FROM licensing.tenant_product_bindings
 WHERE tenant_id=erp.current_tenant_id() AND product_id=p_product FOR SHARE;
 IF v_license IS NULL THEN RAISE EXCEPTION 'No designated license' USING ERRCODE='42501'; END IF;
 SELECT status INTO v_status FROM licensing.licenses WHERE id=v_license AND tenant_id=erp.current_tenant_id() FOR SHARE;
 PERFORM 1 FROM erp.company_policy_state WHERE company_id=p_company FOR SHARE;
 IF NOT FOUND OR NOT identity.has_company_permission(p_company,p_permission) THEN
   RAISE EXCEPTION 'Company permission denied' USING ERRCODE='42501'; END IF;
 -- The company can have been deactivated while this transaction waited for policy.
 IF NOT EXISTS(SELECT 1 FROM erp.companies WHERE id=p_company AND is_active) THEN
   RAISE EXCEPTION 'Company inactive' USING ERRCODE='42501'; END IF;
 v_now:=clock_timestamp();
 SELECT plan_version_id INTO v_plan FROM licensing.license_terms
 WHERE license_id=v_license AND starts_at<=v_now AND (expires_at IS NULL OR v_now<expires_at);
 IF v_status IS DISTINCT FROM 'active' OR v_plan IS NULL THEN
   RAISE EXCEPTION 'License inactive or expired' USING ERRCODE='42501'; END IF;
 IF NOT EXISTS(SELECT 1 FROM erp.module_definitions WHERE code=p_module) THEN RAISE EXCEPTION 'Unknown module'; END IF;
 IF EXISTS(WITH RECURSIVE required(code) AS (
   SELECT p_module UNION SELECT d.required_module_code FROM required r JOIN erp.module_dependencies d ON d.module_code=r.code
 ) SELECT 1 FROM required r JOIN erp.module_definitions m ON m.code=r.code
 LEFT JOIN erp.company_module_settings s ON s.company_id=p_company AND s.module_code=r.code
 WHERE coalesce(s.mode,'disabled')<>'enabled' OR m.release_status<>'available' OR
 (m.license_feature_code IS NOT NULL AND NOT EXISTS(SELECT 1 FROM licensing.plan_features f
 WHERE f.plan_version_id=v_plan AND f.feature_code=m.license_feature_code AND f.is_enabled))) THEN
   RAISE EXCEPTION 'Module/dependency is unavailable or not entitled' USING ERRCODE='42501'; END IF;
 RETURN v_plan;
END $$;

CREATE FUNCTION erp.set_company_modules(p_company uuid,p_product uuid,p_expected_revision bigint,p_changes jsonb,p_reason text)
RETURNS bigint LANGUAGE plpgsql AS $$
DECLARE v_revision bigint; v_license uuid; v_plan uuid; v_status text; v_now timestamptz;
BEGIN
 IF jsonb_typeof(p_changes) IS DISTINCT FROM 'array' OR jsonb_array_length(p_changes) NOT BETWEEN 1 AND 24 THEN
   RAISE EXCEPTION 'changes must be a bounded nonempty array'; END IF;
 IF nullif(btrim(p_reason),'') IS NULL THEN RAISE EXCEPTION 'Change reason required'; END IF;
 SELECT license_id INTO v_license FROM licensing.tenant_product_bindings
 WHERE tenant_id=erp.current_tenant_id() AND product_id=p_product FOR SHARE;
 IF v_license IS NOT NULL THEN
   SELECT status INTO v_status FROM licensing.licenses WHERE id=v_license AND tenant_id=erp.current_tenant_id() FOR SHARE;
 END IF;
 SELECT revision INTO v_revision FROM erp.company_policy_state WHERE company_id=p_company FOR UPDATE;
 IF NOT FOUND OR NOT identity.has_company_permission(p_company,'company.modules.manage') THEN
   RAISE EXCEPTION 'Company administration denied' USING ERRCODE='42501'; END IF;
 IF p_expected_revision IS NULL OR v_revision<>p_expected_revision THEN RAISE EXCEPTION 'Policy revision conflict' USING ERRCODE='40001'; END IF;
 IF EXISTS(SELECT 1 FROM jsonb_to_recordset(p_changes) AS x(module_code text,mode text)
           GROUP BY module_code HAVING count(*)>1) THEN RAISE EXCEPTION 'Duplicate module in batch'; END IF;
 v_now:=clock_timestamp();
 SELECT plan_version_id INTO v_plan FROM licensing.license_terms WHERE license_id=v_license
 AND starts_at<=v_now AND (expires_at IS NULL OR v_now<expires_at);
 IF EXISTS(SELECT 1 FROM jsonb_to_recordset(p_changes) AS x(module_code text,mode text)
 LEFT JOIN erp.module_definitions m ON m.code=x.module_code
 WHERE m.code IS NULL OR x.mode IS NULL OR x.mode NOT IN ('enabled','read_only','disabled') OR
 (x.mode='enabled' AND (v_status IS DISTINCT FROM 'active' OR v_plan IS NULL OR
  (m.license_feature_code IS NOT NULL AND NOT EXISTS(SELECT 1 FROM licensing.plan_features f
  WHERE f.plan_version_id=v_plan AND f.feature_code=m.license_feature_code AND f.is_enabled))))) THEN
   RAISE EXCEPTION 'Invalid or unlicensed module change' USING ERRCODE='42501'; END IF;
 PERFORM set_config('app.change_reason',p_reason,true);
 INSERT INTO erp.company_module_settings(company_id,module_code,mode)
 SELECT p_company,x.module_code,x.mode FROM jsonb_to_recordset(p_changes) AS x(module_code text,mode text)
 ON CONFLICT(company_id,module_code) DO UPDATE SET mode=excluded.mode;
 IF EXISTS(SELECT 1 FROM erp.company_module_settings s JOIN erp.module_dependencies d ON d.module_code=s.module_code
 LEFT JOIN erp.company_module_settings r ON r.company_id=s.company_id AND r.module_code=d.required_module_code
 WHERE s.company_id=p_company AND s.mode='enabled' AND coalesce(r.mode,'disabled')<>'enabled') THEN
   RAISE EXCEPTION 'Module dependency conflict'; END IF;
 SELECT revision INTO v_revision FROM erp.company_policy_state WHERE company_id=p_company;
 INSERT INTO erp.outbox_events(company_id,event_key,aggregate_type,aggregate_id,event_type,payload)
 VALUES(p_company,'policy:'||v_revision,'company',p_company,'company.modules.changed',
 jsonb_build_object('policy_revision',v_revision,'changes',p_changes));
 RETURN v_revision;
END $$;

CREATE TABLE erp.api_command_receipts (
    id uuid PRIMARY KEY DEFAULT uuidv7(),
    tenant_id uuid NOT NULL REFERENCES erp.tenants(id),
    company_id uuid,
    actor_user_id uuid REFERENCES identity.users(id),
    actor_service_id uuid,
    operation text NOT NULL,
    resource_key text NOT NULL DEFAULT '',
    idempotency_key varchar(128) NOT NULL CHECK(length(btrim(idempotency_key))>0),
    request_hash bytea NOT NULL CHECK(octet_length(request_hash)=32),
    status text NOT NULL DEFAULT 'processing' CHECK(status IN ('processing','completed')),
    response_status smallint CHECK(response_status BETWEEN 200 AND 599),
    result_type text,
    result_id uuid,
    response_body jsonb,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    completed_at timestamptz,
    retain_until timestamptz NOT NULL,
    UNIQUE(tenant_id,id),
    UNIQUE NULLS NOT DISTINCT(tenant_id,company_id,actor_user_id,actor_service_id,operation,resource_key,idempotency_key),
    FOREIGN KEY(tenant_id,company_id) REFERENCES erp.companies(tenant_id,id),
    FOREIGN KEY(tenant_id,actor_service_id) REFERENCES identity.service_accounts(tenant_id,id),
    CHECK(num_nonnulls(actor_user_id,actor_service_id)=1),
    CHECK(status<>'completed' OR (response_status IS NOT NULL AND completed_at IS NOT NULL)),
    CHECK(retain_until>created_at)
);
CREATE INDEX ix_receipts_retention ON erp.api_command_receipts(retain_until);
-- A receipt is intentionally insertable in `processing` state. The command
-- service completes it in the same business transaction, while a sweeper can
-- recover a crashed processing claim after a bounded lease/retention policy.
CREATE FUNCTION erp.guard_command_receipt() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF OLD.status='completed' THEN RAISE EXCEPTION 'Completed command outcome is immutable'; END IF;
 IF (NEW.tenant_id,NEW.company_id,NEW.actor_user_id,NEW.actor_service_id,NEW.operation,NEW.resource_key,NEW.idempotency_key,NEW.request_hash)
 IS DISTINCT FROM (OLD.tenant_id,OLD.company_id,OLD.actor_user_id,OLD.actor_service_id,OLD.operation,OLD.resource_key,OLD.idempotency_key,OLD.request_hash) THEN
   RAISE EXCEPTION 'Command identity/payload cannot change'; END IF; RETURN NEW;
END $$;
CREATE TRIGGER trg_receipt_identity BEFORE UPDATE ON erp.api_command_receipts
 FOR EACH ROW EXECUTE FUNCTION erp.guard_command_receipt();

CREATE TABLE erp.background_jobs (
    id uuid PRIMARY KEY DEFAULT uuidv7(),
    tenant_id uuid NOT NULL REFERENCES erp.tenants(id),
    company_id uuid,
    requester_user_id uuid REFERENCES identity.users(id),
    requester_service_id uuid,
    job_type text NOT NULL,
    queue_name text NOT NULL CHECK(queue_name IN ('integrations','documents','reports','imports','tax_submission','maintenance')),
    job_key varchar(200) NOT NULL,
    parameters jsonb NOT NULL DEFAULT '{}'::jsonb,
    status text NOT NULL DEFAULT 'queued' CHECK(status IN ('queued','running','succeeded','failed','cancel_requested','cancelled')),
    progress numeric(5,2) NOT NULL DEFAULT 0 CHECK(progress BETWEEN 0 AND 100),
    run_after timestamptz NOT NULL DEFAULT clock_timestamp(),
    claim_token uuid,
    claim_expires_at timestamptz,
    heartbeat_at timestamptz,
    attempt_count integer NOT NULL DEFAULT 0 CHECK(attempt_count>=0),
    max_attempts integer NOT NULL DEFAULT 5 CHECK(max_attempts BETWEEN 1 AND 100),
    result_object_key text,
    result_metadata jsonb,
    last_error_code text,
    last_error_message text,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    started_at timestamptz,
    finished_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE(tenant_id,id), UNIQUE(company_id,id),
    UNIQUE NULLS NOT DISTINCT(tenant_id,company_id,job_type,job_key),
    FOREIGN KEY(tenant_id,company_id) REFERENCES erp.companies(tenant_id,id),
    FOREIGN KEY(tenant_id,requester_service_id) REFERENCES identity.service_accounts(tenant_id,id),
    CHECK(num_nonnulls(requester_user_id,requester_service_id)<=1), -- maintenance may be system-owned
    CHECK((claim_token IS NULL)=(claim_expires_at IS NULL)),
    CHECK(status NOT IN ('running','cancel_requested') OR claim_token IS NOT NULL),
    CHECK(status NOT IN ('succeeded','failed','cancelled') OR finished_at IS NOT NULL)
);
CREATE INDEX ix_jobs_ready ON erp.background_jobs(tenant_id,queue_name,run_after,id) WHERE status='queued';
CREATE INDEX ix_jobs_expired_claim ON erp.background_jobs(tenant_id,claim_expires_at) WHERE status='running';
CREATE INDEX ix_jobs_company_history ON erp.background_jobs(company_id,created_at DESC,id DESC);
CREATE FUNCTION erp.guard_job_request() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF (NEW.tenant_id,NEW.company_id,NEW.requester_user_id,NEW.requester_service_id,NEW.job_type,NEW.queue_name,NEW.job_key,NEW.parameters)
 IS DISTINCT FROM (OLD.tenant_id,OLD.company_id,OLD.requester_user_id,OLD.requester_service_id,OLD.job_type,OLD.queue_name,OLD.job_key,OLD.parameters) THEN
   RAISE EXCEPTION 'Job request is immutable; submit a new job'; END IF;
 NEW.updated_at:=clock_timestamp(); RETURN NEW;
END $$;
CREATE TRIGGER trg_job_request_immutable BEFORE UPDATE ON erp.background_jobs
 FOR EACH ROW EXECUTE FUNCTION erp.guard_job_request();
CREATE FUNCTION erp.claim_jobs(p_queue text,p_limit integer DEFAULT 10,p_lease_seconds integer DEFAULT 120)
RETURNS SETOF erp.background_jobs LANGUAGE plpgsql AS $$
BEGIN
 IF p_limit NOT BETWEEN 1 AND 100 OR p_lease_seconds NOT BETWEEN 10 AND 900 THEN RAISE EXCEPTION 'Invalid claim bounds'; END IF;
 RETURN QUERY WITH selected AS (
   SELECT id FROM erp.background_jobs WHERE tenant_id=erp.current_tenant_id() AND queue_name=p_queue
   AND attempt_count<max_attempts AND
   ((status='queued' AND run_after<=statement_timestamp()) OR (status='running' AND claim_expires_at<statement_timestamp()))
   ORDER BY run_after,id FOR UPDATE SKIP LOCKED LIMIT p_limit
 ) UPDATE erp.background_jobs j SET status='running',claim_token=uuidv7(),
   claim_expires_at=clock_timestamp()+make_interval(secs=>p_lease_seconds),heartbeat_at=clock_timestamp(),
   attempt_count=j.attempt_count+1,started_at=coalesce(j.started_at,clock_timestamp()),finished_at=NULL
 FROM selected s WHERE j.id=s.id RETURNING j.*;
END $$;
COMMENT ON FUNCTION erp.claim_jobs(text,integer,integer) IS
 'Claim and commit before network work. Complete/heartbeat only WHERE id AND claim_token match. Sweeper handles exhausted claims and cancellations.';

CREATE TABLE erp.integration_connections (
    id uuid PRIMARY KEY DEFAULT uuidv7(),
    company_id uuid NOT NULL REFERENCES erp.companies(id),
    channel_id uuid,
    provider text NOT NULL,
    name text NOT NULL,
    public_routing_id uuid NOT NULL DEFAULT uuidv7() UNIQUE,
    secret_reference text NOT NULL, -- reference into a secret manager, not the secret itself
    mode text NOT NULL DEFAULT 'disabled' CHECK(mode IN ('enabled','paused','disabled')),
    provider_account_reference text,
    settings jsonb NOT NULL DEFAULT '{}'::jsonb,
    row_version bigint NOT NULL DEFAULT 1,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE(company_id,id), UNIQUE(company_id,provider,name),
    FOREIGN KEY(company_id,channel_id) REFERENCES erp.sales_channels(company_id,id)
);
ALTER TABLE erp.integration_event_inbox ADD COLUMN connection_id uuid;
ALTER TABLE erp.integration_event_inbox ADD COLUMN payload_hash bytea CHECK(payload_hash IS NULL OR octet_length(payload_hash)=32);
ALTER TABLE erp.integration_event_inbox ADD COLUMN claim_token uuid;
ALTER TABLE erp.integration_event_inbox ADD COLUMN claim_expires_at timestamptz;
ALTER TABLE erp.integration_event_inbox ADD COLUMN attempt_count integer NOT NULL DEFAULT 0 CHECK(attempt_count>=0);
ALTER TABLE erp.integration_event_inbox ADD COLUMN run_after timestamptz NOT NULL DEFAULT clock_timestamp();
ALTER TABLE erp.integration_event_inbox ADD CONSTRAINT fk_inbox_connection FOREIGN KEY(company_id,connection_id) REFERENCES erp.integration_connections(company_id,id);
ALTER TABLE erp.integration_event_inbox DROP CONSTRAINT integration_event_inbox_status_check;
ALTER TABLE erp.integration_event_inbox ADD CONSTRAINT ck_inbox_status CHECK(status IN ('received','processing','processed','failed','paused'));
ALTER TABLE erp.integration_event_inbox ADD CONSTRAINT ck_inbox_claim CHECK((claim_token IS NULL)=(claim_expires_at IS NULL));
CREATE UNIQUE INDEX uq_inbox_connection_event ON erp.integration_event_inbox(company_id,connection_id,external_event_id) WHERE connection_id IS NOT NULL;
CREATE INDEX ix_inbox_ready ON erp.integration_event_inbox(company_id,run_after,id) WHERE status IN ('received','failed');
-- Existing rows may have null connection/hash; new webhooks require both at the application boundary.
CREATE FUNCTION erp.guard_inbox_payload() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF (NEW.company_id,NEW.channel_id,NEW.connection_id,NEW.external_event_id,NEW.event_type,NEW.payload,NEW.payload_hash)
 IS DISTINCT FROM (OLD.company_id,OLD.channel_id,OLD.connection_id,OLD.external_event_id,OLD.event_type,OLD.payload,OLD.payload_hash) THEN
 RAISE EXCEPTION 'Inbox event identity and payload are immutable'; END IF; RETURN NEW;
END $$;
CREATE TRIGGER trg_inbox_payload_immutable BEFORE UPDATE ON erp.integration_event_inbox
 FOR EACH ROW EXECUTE FUNCTION erp.guard_inbox_payload();

CREATE TABLE platform.operator_command_receipts (
    id uuid PRIMARY KEY DEFAULT uuidv7(),
    operator_user_id uuid NOT NULL REFERENCES identity.users(id),
    operation text NOT NULL,
    resource_key text NOT NULL DEFAULT '',
    idempotency_key varchar(128) NOT NULL,
    request_hash bytea NOT NULL CHECK(octet_length(request_hash)=32),
    response_status smallint NOT NULL CHECK(response_status BETWEEN 200 AND 599),
    result_reference jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE(operator_user_id,operation,resource_key,idempotency_key)
);
CREATE TABLE platform.operator_audit_events (
    id uuid PRIMARY KEY DEFAULT uuidv7(),
    operator_user_id uuid REFERENCES identity.users(id),
    action text NOT NULL,
    object_type text NOT NULL,
    object_id text NOT NULL,
    reason text NOT NULL,
    event_data jsonb NOT NULL DEFAULT '{}'::jsonb,
    occurred_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TRIGGER trg_operator_audit_immutable BEFORE UPDATE OR DELETE ON platform.operator_audit_events
 FOR EACH ROW EXECUTE FUNCTION erp.reject_audit_mutation();
CREATE TRIGGER trg_operator_receipt_immutable BEFORE UPDATE OR DELETE ON platform.operator_command_receipts
 FOR EACH ROW EXECUTE FUNCTION erp.reject_audit_mutation();

-- A synchronized operational projection. Ledger rows remain the rebuildable truth.
CREATE TABLE erp.inventory_positions (
    id uuid PRIMARY KEY DEFAULT uuidv7(),
    company_id uuid NOT NULL REFERENCES erp.companies(id),
    warehouse_id uuid NOT NULL,
    item_id uuid NOT NULL,
    lot_id uuid,
    on_hand_quantity numeric(20,6) NOT NULL DEFAULT 0,
    reserved_quantity numeric(20,6) NOT NULL DEFAULT 0 CHECK(reserved_quantity>=0),
    value_company numeric(20,6) NOT NULL DEFAULT 0,
    lock_stub boolean NOT NULL DEFAULT true CHECK(lock_stub),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE NULLS NOT DISTINCT(company_id,warehouse_id,item_id,lot_id),
    FOREIGN KEY(company_id,warehouse_id) REFERENCES erp.warehouses(company_id,id),
    FOREIGN KEY(company_id,item_id) REFERENCES erp.items(company_id,id),
    FOREIGN KEY(company_id,item_id,lot_id) REFERENCES erp.inventory_lots(company_id,item_id,id),
    CHECK(on_hand_quantity>=reserved_quantity) -- this release disallows negative available stock
);
WITH scopes AS (
 SELECT company_id,warehouse_id,item_id,lot_id FROM erp.stock_movements
 UNION SELECT company_id,warehouse_id,item_id,lot_id FROM erp.inventory_reservations
), stock AS (
 SELECT company_id,warehouse_id,item_id,lot_id,sum(quantity_delta) AS qty,sum(value_delta_company) AS val
 FROM erp.stock_movements GROUP BY company_id,warehouse_id,item_id,lot_id
), reserved AS (
 SELECT company_id,warehouse_id,item_id,lot_id,sum(quantity) AS qty FROM erp.inventory_reservations
 WHERE status='active' GROUP BY company_id,warehouse_id,item_id,lot_id
)
INSERT INTO erp.inventory_positions(company_id,warehouse_id,item_id,lot_id,on_hand_quantity,reserved_quantity,value_company)
 SELECT s.company_id,s.warehouse_id,s.item_id,s.lot_id,coalesce(q.qty,0),coalesce(r.qty,0),coalesce(q.val,0)
 FROM scopes s LEFT JOIN stock q ON q.company_id=s.company_id AND q.warehouse_id=s.warehouse_id
 AND q.item_id=s.item_id AND q.lot_id IS NOT DISTINCT FROM s.lot_id
 LEFT JOIN reserved r ON r.company_id=s.company_id AND r.warehouse_id=s.warehouse_id
 AND r.item_id=s.item_id AND r.lot_id IS NOT DISTINCT FROM s.lot_id;
-- Narrow SECURITY DEFINER triggers permit ledger writers without granting direct projection mutation.
CREATE FUNCTION erp.project_stock_insert() RETURNS trigger LANGUAGE plpgsql
 SECURITY DEFINER SET search_path=pg_catalog AS $$
BEGIN
 IF NOT EXISTS(SELECT 1 FROM erp.companies WHERE id=NEW.company_id AND tenant_id=erp.current_tenant_id()) THEN
   RAISE EXCEPTION 'Stock projection tenant context missing/mismatched' USING ERRCODE='42501'; END IF;
 INSERT INTO erp.inventory_positions(company_id,warehouse_id,item_id,lot_id,on_hand_quantity,value_company)
 VALUES(NEW.company_id,NEW.warehouse_id,NEW.item_id,NEW.lot_id,NEW.quantity_delta,NEW.value_delta_company)
 ON CONFLICT(company_id,warehouse_id,item_id,lot_id) DO UPDATE
 SET on_hand_quantity=erp.inventory_positions.on_hand_quantity+excluded.on_hand_quantity,
 value_company=erp.inventory_positions.value_company+excluded.value_company,updated_at=clock_timestamp();
 RETURN NEW;
END $$;
CREATE TRIGGER trg_stock_projection AFTER INSERT ON erp.stock_movements
 FOR EACH ROW EXECUTE FUNCTION erp.project_stock_insert();
CREATE FUNCTION erp.project_reservation_change() RETURNS trigger LANGUAGE plpgsql
 SECURITY DEFINER SET search_path=pg_catalog AS $$
DECLARE v_delta numeric(20,6);
BEGIN
 IF TG_OP='DELETE' THEN RAISE EXCEPTION 'Release reservations; do not delete their history'; END IF;
 IF NOT EXISTS(SELECT 1 FROM erp.companies WHERE id=NEW.company_id AND tenant_id=erp.current_tenant_id()) THEN
   RAISE EXCEPTION 'Reservation tenant context missing/mismatched' USING ERRCODE='42501'; END IF;
 v_delta:=CASE WHEN NEW.status='active' THEN NEW.quantity ELSE 0 END;
 IF TG_OP='UPDATE' THEN
   IF (NEW.company_id,NEW.warehouse_id,NEW.item_id,NEW.lot_id,NEW.source_type,NEW.source_id,NEW.reservation_key)
   IS DISTINCT FROM (OLD.company_id,OLD.warehouse_id,OLD.item_id,OLD.lot_id,OLD.source_type,OLD.source_id,OLD.reservation_key) THEN
     RAISE EXCEPTION 'Reservation scope/source cannot change'; END IF;
   v_delta:=v_delta-CASE WHEN OLD.status='active' THEN OLD.quantity ELSE 0 END;
 END IF;
 IF v_delta<>0 THEN
   UPDATE erp.inventory_positions SET reserved_quantity=reserved_quantity+v_delta,updated_at=clock_timestamp()
   WHERE company_id=NEW.company_id AND warehouse_id=NEW.warehouse_id AND item_id=NEW.item_id AND lot_id IS NOT DISTINCT FROM NEW.lot_id;
   IF NOT FOUND THEN RAISE EXCEPTION 'Stock scope missing; cannot reserve unavailable stock'; END IF;
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER trg_reservation_projection AFTER INSERT OR UPDATE OR DELETE ON erp.inventory_reservations
 FOR EACH ROW EXECUTE FUNCTION erp.project_reservation_change();
CREATE VIEW erp.v_inventory_availability WITH(security_invoker=true) AS
 SELECT company_id,warehouse_id,item_id,lot_id,on_hand_quantity,reserved_quantity,
 on_hand_quantity-reserved_quantity AS available_quantity,value_company,updated_at
 FROM erp.inventory_positions;

ALTER TABLE erp.sales_order_lines ADD CONSTRAINT uq_sales_order_line_company_id UNIQUE(company_id,id);
ALTER TABLE erp.purchase_order_lines ADD CONSTRAINT uq_purchase_order_line_company_id UNIQUE(company_id,id);
CREATE TABLE erp.inventory_documents (
    id uuid PRIMARY KEY DEFAULT uuidv7(),
    company_id uuid NOT NULL REFERENCES erp.companies(id),
    document_no text NOT NULL,
    document_kind text NOT NULL CHECK(document_kind IN ('receipt','shipment','transfer','adjustment_in','adjustment_out')),
    document_date date NOT NULL,
    status text NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','posted','void')),
    journal_entry_id uuid,
    reversal_of_document_id uuid,
    row_version bigint NOT NULL DEFAULT 1 CHECK(row_version>0),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    posted_at timestamptz,
    UNIQUE(company_id,id), UNIQUE(company_id,document_no),
    FOREIGN KEY(company_id,journal_entry_id) REFERENCES erp.journal_entries(company_id,id),
    FOREIGN KEY(company_id,reversal_of_document_id) REFERENCES erp.inventory_documents(company_id,id),
    CHECK(status<>'posted' OR posted_at IS NOT NULL)
);
CREATE TABLE erp.inventory_document_lines (
    id uuid PRIMARY KEY DEFAULT uuidv7(),
    company_id uuid NOT NULL,
    inventory_document_id uuid NOT NULL,
    line_no integer NOT NULL CHECK(line_no>0),
    item_id uuid NOT NULL,
    lot_id uuid,
    from_warehouse_id uuid,
    to_warehouse_id uuid,
    quantity numeric(20,6) NOT NULL CHECK(quantity>0),
    unit_cost_company numeric(20,6) NOT NULL CHECK(unit_cost_company>=0),
    sales_order_line_id uuid,
    purchase_order_line_id uuid,
    UNIQUE(company_id,id), UNIQUE(company_id,inventory_document_id,line_no),
    FOREIGN KEY(company_id,inventory_document_id) REFERENCES erp.inventory_documents(company_id,id),
    FOREIGN KEY(company_id,item_id) REFERENCES erp.items(company_id,id),
    FOREIGN KEY(company_id,item_id,lot_id) REFERENCES erp.inventory_lots(company_id,item_id,id),
    FOREIGN KEY(company_id,from_warehouse_id) REFERENCES erp.warehouses(company_id,id),
    FOREIGN KEY(company_id,to_warehouse_id) REFERENCES erp.warehouses(company_id,id),
    FOREIGN KEY(company_id,sales_order_line_id) REFERENCES erp.sales_order_lines(company_id,id),
    FOREIGN KEY(company_id,purchase_order_line_id) REFERENCES erp.purchase_order_lines(company_id,id),
    CHECK(num_nonnulls(from_warehouse_id,to_warehouse_id)>=1),
    CHECK(from_warehouse_id IS NULL OR to_warehouse_id IS NULL OR from_warehouse_id<>to_warehouse_id),
    CHECK(num_nonnulls(sales_order_line_id,purchase_order_line_id)<=1)
);
ALTER TABLE erp.stock_movements ADD COLUMN inventory_document_line_id uuid;
ALTER TABLE erp.stock_movements ADD CONSTRAINT fk_stock_inventory_line
 FOREIGN KEY(company_id,inventory_document_line_id) REFERENCES erp.inventory_document_lines(company_id,id);
CREATE INDEX ix_inventory_document_movements ON erp.stock_movements(company_id,inventory_document_line_id) WHERE inventory_document_line_id IS NOT NULL;
CREATE INDEX ix_inventory_document_history ON erp.inventory_documents(company_id,document_date DESC,id DESC);
CREATE FUNCTION erp.guard_inventory_document() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF TG_OP='INSERT' AND NEW.status<>'draft' THEN RAISE EXCEPTION 'Create inventory document as draft'; END IF;
 IF TG_OP<>'INSERT' AND OLD.status='posted' THEN RAISE EXCEPTION 'Posted inventory documents are immutable'; END IF;
 IF TG_OP='DELETE' THEN RETURN OLD; END IF;
 IF NEW.status='posted' THEN NEW.posted_at:=coalesce(NEW.posted_at,clock_timestamp()); END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER trg_inventory_document_immutable BEFORE INSERT OR UPDATE OR DELETE ON erp.inventory_documents
 FOR EACH ROW EXECUTE FUNCTION erp.guard_inventory_document();
CREATE FUNCTION erp.guard_inventory_document_line() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE v_parent uuid; v_company uuid; v_status text;
BEGIN
 v_parent:=CASE WHEN TG_OP='DELETE' THEN OLD.inventory_document_id ELSE NEW.inventory_document_id END;
 v_company:=CASE WHEN TG_OP='DELETE' THEN OLD.company_id ELSE NEW.company_id END;
 IF TG_OP='UPDATE' AND NEW.inventory_document_id IS DISTINCT FROM OLD.inventory_document_id THEN
   RAISE EXCEPTION 'Cannot move inventory document line'; END IF;
 SELECT status INTO v_status FROM erp.inventory_documents WHERE company_id=v_company AND id=v_parent FOR UPDATE;
 IF v_status IS DISTINCT FROM 'draft' THEN RAISE EXCEPTION 'Inventory lines require a draft document'; END IF;
 IF TG_OP='DELETE' THEN RETURN OLD; END IF; RETURN NEW;
END $$;
CREATE TRIGGER trg_inventory_line_guard BEFORE INSERT OR UPDATE OR DELETE ON erp.inventory_document_lines
 FOR EACH ROW EXECUTE FUNCTION erp.guard_inventory_document_line();
CREATE FUNCTION erp.assert_inventory_document(p_company uuid,p_document uuid) RETURNS void LANGUAGE plpgsql AS $$
DECLARE d erp.inventory_documents%ROWTYPE;
BEGIN
 SELECT * INTO d FROM erp.inventory_documents WHERE company_id=p_company AND id=p_document;
 IF d.status<>'posted' THEN
   IF EXISTS(SELECT 1 FROM erp.stock_movements m JOIN erp.inventory_document_lines l
   ON l.company_id=m.company_id AND l.id=m.inventory_document_line_id
   WHERE l.company_id=p_company AND l.inventory_document_id=p_document) THEN
     RAISE EXCEPTION 'Stock effects cannot commit against an unposted inventory document'; END IF;
   RETURN;
 END IF;
 IF NOT EXISTS(SELECT 1 FROM erp.inventory_document_lines WHERE company_id=p_company AND inventory_document_id=p_document) THEN
   RAISE EXCEPTION 'Inventory document has no lines'; END IF;
 IF d.journal_entry_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM erp.journal_entries WHERE company_id=p_company AND id=d.journal_entry_id AND status='posted') THEN
   RAISE EXCEPTION 'Inventory journal must be posted'; END IF;
 IF EXISTS(SELECT 1 FROM erp.inventory_document_lines l WHERE l.company_id=p_company AND l.inventory_document_id=p_document AND (
   (d.document_kind IN ('receipt','adjustment_in') AND (l.from_warehouse_id IS NOT NULL OR l.to_warehouse_id IS NULL)) OR
   (d.document_kind IN ('shipment','adjustment_out') AND (l.from_warehouse_id IS NULL OR l.to_warehouse_id IS NOT NULL)) OR
   (d.document_kind='transfer' AND num_nonnulls(l.from_warehouse_id,l.to_warehouse_id)<>2) OR
   (l.from_warehouse_id IS NOT NULL AND coalesce((SELECT sum(m.quantity_delta) FROM erp.stock_movements m
      WHERE m.company_id=l.company_id AND m.inventory_document_line_id=l.id AND m.warehouse_id=l.from_warehouse_id),0)<>-l.quantity) OR
   (l.to_warehouse_id IS NOT NULL AND coalesce((SELECT sum(m.quantity_delta) FROM erp.stock_movements m
      WHERE m.company_id=l.company_id AND m.inventory_document_line_id=l.id AND m.warehouse_id=l.to_warehouse_id),0)<>l.quantity) OR
   EXISTS(SELECT 1 FROM erp.stock_movements m WHERE m.company_id=l.company_id AND m.inventory_document_line_id=l.id AND
      (m.item_id<>l.item_id OR m.lot_id IS DISTINCT FROM l.lot_id OR
      (m.warehouse_id IS DISTINCT FROM l.from_warehouse_id AND m.warehouse_id IS DISTINCT FROM l.to_warehouse_id)))
 )) THEN RAISE EXCEPTION 'Inventory line and movement quantities/scopes disagree'; END IF;
END $$;
CREATE FUNCTION erp.check_inventory_document_event() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE v_document uuid;
BEGIN
 IF TG_TABLE_NAME='inventory_documents' THEN v_document:=NEW.id;
 ELSE SELECT inventory_document_id INTO v_document FROM erp.inventory_document_lines
 WHERE company_id=NEW.company_id AND id=NEW.inventory_document_line_id; END IF;
 IF v_document IS NOT NULL THEN PERFORM erp.assert_inventory_document(NEW.company_id,v_document); END IF; RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER trg_inventory_document_consistent AFTER INSERT OR UPDATE ON erp.inventory_documents
 DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION erp.check_inventory_document_event();
CREATE CONSTRAINT TRIGGER trg_inventory_effect_consistent AFTER INSERT ON erp.stock_movements
 DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION erp.check_inventory_document_event();

-- A return is a stable period/kind container. Amendments are separate frozen revisions.
CREATE TABLE erp.tax_return_revisions (
    id uuid PRIMARY KEY DEFAULT uuidv7(),
    company_id uuid NOT NULL,
    tax_return_id uuid NOT NULL,
    revision_no integer NOT NULL CHECK(revision_no>0),
    prior_revision_id uuid,
    currency_code char(3) NOT NULL REFERENCES erp.currencies(code),
    taxable_base_total numeric(20,6) NOT NULL DEFAULT 0,
    tax_due_total numeric(20,6) NOT NULL DEFAULT 0,
    ruleset_reference text NOT NULL,
    source_cutoff timestamptz NOT NULL,
    filing_payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    frozen_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE(company_id,id), UNIQUE(company_id,tax_return_id,id), UNIQUE(company_id,tax_return_id,revision_no),
    FOREIGN KEY(company_id,tax_return_id) REFERENCES erp.tax_returns(company_id,id),
    FOREIGN KEY(company_id,tax_return_id,prior_revision_id) REFERENCES erp.tax_return_revisions(company_id,tax_return_id,id),
    CHECK((revision_no=1 AND prior_revision_id IS NULL) OR (revision_no>1 AND prior_revision_id IS NOT NULL)),
    CHECK(id IS DISTINCT FROM prior_revision_id)
);
CREATE TABLE erp.tax_return_revision_lines (
    id uuid PRIMARY KEY DEFAULT uuidv7(),
    company_id uuid NOT NULL,
    revision_id uuid NOT NULL,
    box_code text NOT NULL,
    description text NOT NULL,
    taxable_base_amount numeric(20,6) NOT NULL DEFAULT 0,
    output_tax_amount numeric(20,6) NOT NULL DEFAULT 0,
    recoverable_tax_amount numeric(20,6) NOT NULL DEFAULT 0,
    withholding_amount numeric(20,6) NOT NULL DEFAULT 0,
    adjustment_amount numeric(20,6) NOT NULL DEFAULT 0,
    UNIQUE(company_id,revision_id,box_code),
    FOREIGN KEY(company_id,revision_id) REFERENCES erp.tax_return_revisions(company_id,id)
);
CREATE TABLE erp.tax_submission_attempts (
    id uuid PRIMARY KEY DEFAULT uuidv7(),
    company_id uuid NOT NULL,
    revision_id uuid NOT NULL,
    attempt_no integer NOT NULL CHECK(attempt_no>0),
    provider text NOT NULL,
    idempotency_key text NOT NULL,
    status text NOT NULL DEFAULT 'queued' CHECK(status IN ('queued','sent','accepted','rejected','unknown')),
    request_hash bytea NOT NULL CHECK(octet_length(request_hash)=32),
    request_object_key text,
    response_object_key text,
    authority_receipt_ref text,
    last_error_code text,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    sent_at timestamptz,
    resolved_at timestamptz,
    UNIQUE(company_id,revision_id,attempt_no), UNIQUE(company_id,provider,idempotency_key),
    FOREIGN KEY(company_id,revision_id) REFERENCES erp.tax_return_revisions(company_id,id)
);
CREATE FUNCTION erp.guard_tax_revision() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE v_prior_no integer; v_prior_frozen timestamptz;
BEGIN
 IF TG_OP<>'INSERT' AND OLD.frozen_at IS NOT NULL THEN RAISE EXCEPTION 'Frozen tax revision is immutable'; END IF;
 IF TG_OP='DELETE' THEN RETURN OLD; END IF;
 IF NEW.prior_revision_id IS NOT NULL THEN
   SELECT revision_no,frozen_at INTO v_prior_no,v_prior_frozen FROM erp.tax_return_revisions
   WHERE company_id=NEW.company_id AND tax_return_id=NEW.tax_return_id AND id=NEW.prior_revision_id FOR SHARE;
   IF v_prior_no IS NULL OR v_prior_frozen IS NULL OR NEW.revision_no<>v_prior_no+1 THEN
     RAISE EXCEPTION 'Amendment must follow a frozen preceding revision'; END IF;
 END IF; RETURN NEW;
END $$;
CREATE TRIGGER trg_tax_revision_guard BEFORE INSERT OR UPDATE OR DELETE ON erp.tax_return_revisions
 FOR EACH ROW EXECUTE FUNCTION erp.guard_tax_revision();
CREATE FUNCTION erp.guard_tax_revision_line() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE v_company uuid; v_revision uuid; v_frozen timestamptz;
BEGIN
 v_company:=CASE WHEN TG_OP='DELETE' THEN OLD.company_id ELSE NEW.company_id END;
 v_revision:=CASE WHEN TG_OP='DELETE' THEN OLD.revision_id ELSE NEW.revision_id END;
 IF TG_OP='UPDATE' AND NEW.revision_id IS DISTINCT FROM OLD.revision_id THEN RAISE EXCEPTION 'Cannot move tax revision line'; END IF;
 SELECT frozen_at INTO v_frozen FROM erp.tax_return_revisions WHERE company_id=v_company AND id=v_revision FOR UPDATE;
 IF NOT FOUND OR v_frozen IS NOT NULL THEN RAISE EXCEPTION 'Tax revision missing or frozen'; END IF;
 IF TG_OP='DELETE' THEN RETURN OLD; END IF; RETURN NEW;
END $$;
CREATE TRIGGER trg_tax_revision_line_guard BEFORE INSERT OR UPDATE OR DELETE ON erp.tax_return_revision_lines
 FOR EACH ROW EXECUTE FUNCTION erp.guard_tax_revision_line();
CREATE FUNCTION erp.guard_tax_submission() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF NOT EXISTS(SELECT 1 FROM erp.tax_return_revisions WHERE company_id=NEW.company_id AND id=NEW.revision_id AND frozen_at IS NOT NULL) THEN
   RAISE EXCEPTION 'Submit only a frozen return revision'; END IF;
 IF TG_OP='UPDATE' AND (
 (NEW.company_id,NEW.revision_id,NEW.attempt_no,NEW.provider,NEW.idempotency_key,NEW.request_hash)
 IS DISTINCT FROM (OLD.company_id,OLD.revision_id,OLD.attempt_no,OLD.provider,OLD.idempotency_key,OLD.request_hash)
 OR OLD.status IN ('accepted','rejected')) THEN RAISE EXCEPTION 'Submission identity/final outcome is immutable'; END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER trg_tax_submission_guard BEFORE INSERT OR UPDATE ON erp.tax_submission_attempts
 FOR EACH ROW EXECUTE FUNCTION erp.guard_tax_submission();
CREATE TRIGGER trg_tax_submission_no_delete BEFORE DELETE ON erp.tax_submission_attempts
 FOR EACH ROW EXECUTE FUNCTION erp.reject_audit_mutation();
CREATE INDEX ix_tax_submission_pending ON erp.tax_submission_attempts(company_id,created_at,id) WHERE status IN ('queued','sent','unknown');

CREATE TABLE erp.tax_packs (
    id uuid PRIMARY KEY DEFAULT uuidv7(),
    jurisdiction_id uuid NOT NULL REFERENCES erp.tax_jurisdictions(id),
    version_code text NOT NULL,
    adapter_code text NOT NULL,
    source_reference text NOT NULL,
    content_hash bytea NOT NULL CHECK(octet_length(content_hash)=32),
    effective_from date NOT NULL,
    published_at timestamptz,
    UNIQUE(jurisdiction_id,version_code)
);
CREATE TABLE erp.tax_pack_rate_versions (
    tax_pack_id uuid NOT NULL REFERENCES erp.tax_packs(id),
    tax_rate_version_id uuid NOT NULL REFERENCES erp.tax_rate_versions(id),
    PRIMARY KEY(tax_pack_id,tax_rate_version_id)
);
COMMENT ON TABLE erp.tax_packs IS 'Reviewed country/jurisdiction release metadata; no statutory rates are pre-seeded.';

-- Baseline corrections and database security policies.
-- This section is kept separate so an existing installation can apply it in one reviewed migration.

-- Existing posting paths held the fiscal period FOR UPDATE. Shared readers allow unrelated posters to proceed;
-- period close/reopen acquires FOR UPDATE in the application/service path.
CREATE OR REPLACE FUNCTION erp.guard_posted_journal_entry()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE v_period erp.fiscal_periods%ROWTYPE;
BEGIN
    IF TG_OP='INSERT' THEN
        IF NEW.status<>'draft' THEN RAISE EXCEPTION 'Journal entries must be created as draft'; END IF;
        RETURN NEW;
    END IF;
    IF TG_OP='DELETE' THEN
        IF OLD.status='posted' THEN RAISE EXCEPTION 'Posted journal entries cannot be deleted'; END IF;
        RETURN OLD;
    END IF;
    IF OLD.status='posted' THEN RAISE EXCEPTION 'Posted journal entries cannot be changed'; END IF;
    IF NEW.status='posted' THEN
        IF OLD.status<>'draft' THEN RAISE EXCEPTION 'Only draft journal entries can be posted'; END IF;
        SELECT * INTO v_period FROM erp.fiscal_periods
        WHERE company_id=NEW.company_id AND id=NEW.fiscal_period_id FOR SHARE;
        IF NOT FOUND OR v_period.state<>'open' THEN RAISE EXCEPTION 'Fiscal period is missing or not open'; END IF;
        IF NEW.entry_date<v_period.starts_on OR NEW.entry_date>v_period.ends_on THEN
            RAISE EXCEPTION 'Entry date is outside the selected fiscal period'; END IF;
        IF NEW.entry_number IS NULL OR length(btrim(NEW.entry_number))=0 THEN
            RAISE EXCEPTION 'A journal entry number is required before posting'; END IF;
        IF NEW.posted_at IS NULL THEN NEW.posted_at:=clock_timestamp(); END IF;
    END IF;
    RETURN NEW;
END $$;
CREATE OR REPLACE FUNCTION erp.post_journal_entry(p_company_id uuid,p_entry_id uuid)
RETURNS void LANGUAGE plpgsql AS $$
DECLARE v_entry erp.journal_entries%ROWTYPE; v_period erp.fiscal_periods%ROWTYPE;
BEGIN
    SELECT * INTO v_entry FROM erp.journal_entries
    WHERE company_id=p_company_id AND id=p_entry_id FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'Journal entry not found'; END IF;
    IF v_entry.status<>'draft' THEN RAISE EXCEPTION 'Only draft journal entries can be posted'; END IF;
    SELECT * INTO v_period FROM erp.fiscal_periods
    WHERE company_id=p_company_id AND id=v_entry.fiscal_period_id FOR SHARE;
    IF NOT FOUND THEN RAISE EXCEPTION 'Fiscal period not found'; END IF;
    IF v_period.state<>'open' THEN RAISE EXCEPTION 'Fiscal period is not open'; END IF;
    IF v_entry.entry_date<v_period.starts_on OR v_entry.entry_date>v_period.ends_on THEN
        RAISE EXCEPTION 'Entry date is outside the selected fiscal period'; END IF;
    IF v_entry.entry_number IS NULL OR length(btrim(v_entry.entry_number))=0 THEN
        RAISE EXCEPTION 'A journal entry number is required before posting'; END IF;
    PERFORM erp.assert_journal_entry_balanced(p_company_id,p_entry_id);
    UPDATE erp.journal_entries SET status='posted',posted_at=clock_timestamp()
    WHERE company_id=p_company_id AND id=p_entry_id;
END $$;

-- A period close must use this lock order and then recheck all reconciliation rules in the same transaction.
CREATE FUNCTION erp.close_fiscal_period(p_company_id uuid,p_period_id uuid)
RETURNS void LANGUAGE plpgsql AS $$
DECLARE v_period erp.fiscal_periods%ROWTYPE;
BEGIN
 SELECT * INTO v_period FROM erp.fiscal_periods
 WHERE company_id=p_company_id AND id=p_period_id FOR UPDATE;
 IF NOT FOUND THEN RAISE EXCEPTION 'Fiscal period not found'; END IF;
 IF v_period.state<>'open' THEN RAISE EXCEPTION 'Only an open period can be closed'; END IF;
 -- Domain services perform tax, inventory, AR/AP, and unposted-draft checks before calling this function.
 UPDATE erp.fiscal_periods SET state='closed' WHERE company_id=p_company_id AND id=p_period_id;
END $$;

-- Optimistic concurrency counters for mutable aggregates. Application updates use WHERE row_version=:expected.
DO $$
DECLARE t text;
BEGIN
 FOREACH t IN ARRAY ARRAY['sales_orders','purchase_orders','sales_invoices','purchase_bills','payments','production_orders','boms'] LOOP
   EXECUTE format('ALTER TABLE erp.%I ADD COLUMN IF NOT EXISTS row_version bigint NOT NULL DEFAULT 1',t);
   EXECUTE format('ALTER TABLE erp.%I ADD CONSTRAINT %I CHECK(row_version>0)',t,'ck_'||t||'_row_version');
 END LOOP;
END $$;
CREATE FUNCTION erp.bump_row_version() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF NEW.row_version<>OLD.row_version+1 THEN NEW.row_version:=OLD.row_version+1; END IF;
 NEW.updated_at:=clock_timestamp(); RETURN NEW;
END $$;
CREATE TRIGGER trg_sales_order_version BEFORE UPDATE ON erp.sales_orders FOR EACH ROW EXECUTE FUNCTION erp.bump_row_version();
CREATE TRIGGER trg_purchase_order_version BEFORE UPDATE ON erp.purchase_orders FOR EACH ROW EXECUTE FUNCTION erp.bump_row_version();
CREATE TRIGGER trg_sales_invoice_version BEFORE UPDATE ON erp.sales_invoices FOR EACH ROW EXECUTE FUNCTION erp.bump_row_version();
CREATE TRIGGER trg_purchase_bill_version BEFORE UPDATE ON erp.purchase_bills FOR EACH ROW EXECUTE FUNCTION erp.bump_row_version();
CREATE TRIGGER trg_payment_version BEFORE UPDATE ON erp.payments FOR EACH ROW EXECUTE FUNCTION erp.bump_row_version();
CREATE TRIGGER trg_production_order_version BEFORE UPDATE ON erp.production_orders FOR EACH ROW EXECUTE FUNCTION erp.bump_row_version();
CREATE TRIGGER trg_bom_version BEFORE UPDATE ON erp.boms FOR EACH ROW EXECUTE FUNCTION erp.bump_row_version();

-- Typed credit links. Posted originals remain immutable; credit notes point to the original document.
ALTER TABLE erp.sales_invoices ADD COLUMN IF NOT EXISTS credit_of_invoice_id uuid;
ALTER TABLE erp.sales_invoices ADD CONSTRAINT fk_sales_credit_of_invoice
 FOREIGN KEY(company_id,credit_of_invoice_id) REFERENCES erp.sales_invoices(company_id,id);
ALTER TABLE erp.sales_invoices ADD CONSTRAINT ck_sales_credit_link
 CHECK(document_kind='invoice' OR credit_of_invoice_id IS NOT NULL);
ALTER TABLE erp.purchase_bills ADD COLUMN IF NOT EXISTS credit_of_bill_id uuid;
ALTER TABLE erp.purchase_bills ADD CONSTRAINT fk_purchase_credit_of_bill
 FOREIGN KEY(company_id,credit_of_bill_id) REFERENCES erp.purchase_bills(company_id,id);
ALTER TABLE erp.purchase_bills ADD CONSTRAINT ck_purchase_credit_link
 CHECK(document_kind='bill' OR credit_of_bill_id IS NOT NULL);
CREATE INDEX ix_sales_credit_link ON erp.sales_invoices(company_id,credit_of_invoice_id) WHERE credit_of_invoice_id IS NOT NULL;
CREATE INDEX ix_purchase_credit_link ON erp.purchase_bills(company_id,credit_of_bill_id) WHERE credit_of_bill_id IS NOT NULL;

-- Durable outbox claiming; a claim lease is not a business transaction.
CREATE FUNCTION erp.claim_outbox_events(p_limit integer DEFAULT 50,p_lease_seconds integer DEFAULT 120)
RETURNS SETOF erp.outbox_events LANGUAGE plpgsql AS $$
BEGIN
 IF p_limit NOT BETWEEN 1 AND 500 OR p_lease_seconds NOT BETWEEN 10 AND 900 THEN RAISE EXCEPTION 'Invalid claim bounds'; END IF;
 RETURN QUERY WITH selected AS (
   SELECT id FROM erp.outbox_events
   WHERE company_id IN (SELECT id FROM erp.companies WHERE tenant_id=erp.current_tenant_id())
   AND delivered_at IS NULL AND (claim_expires_at IS NULL OR claim_expires_at<statement_timestamp())
   ORDER BY occurred_at,id FOR UPDATE SKIP LOCKED LIMIT p_limit
 ) UPDATE erp.outbox_events o SET claim_token=uuidv7(),
    claim_expires_at=clock_timestamp()+make_interval(secs=>p_lease_seconds),attempt_count=o.attempt_count+1
 FROM selected s WHERE o.id=s.id RETURNING o.*;
END $$;
CREATE FUNCTION erp.complete_outbox_event(p_id uuid,p_claim uuid,p_error text DEFAULT NULL)
RETURNS boolean LANGUAGE plpgsql AS $$
BEGIN
 UPDATE erp.outbox_events SET delivered_at=CASE WHEN p_error IS NULL THEN clock_timestamp() ELSE NULL END,
 claim_token=NULL,claim_expires_at=NULL,last_error=p_error WHERE id=p_id AND claim_token=p_claim;
 RETURN FOUND;
END $$;

-- Corrected tax component view with currency and signed document polarity. The original view is retained for compatibility.
CREATE VIEW erp.v_tax_transaction_components_v2 WITH(security_invoker=true) AS
SELECT 'sales_invoice'::text source_kind,i.company_id,i.id source_document_id,i.invoice_no source_document_number,
 i.tax_point_date tax_date,i.partner_id,l.line_no,c.tax_jurisdiction_id,j.country_code,j.jurisdiction_code,j.name jurisdiction_name,
 tt.tax_family,tt.code tax_type_code,rs.schedule_code,i.currency_code,i.document_kind,
 CASE WHEN i.document_kind='credit_note' THEN -1 ELSE 1 END polarity,
 c.taxable_base_amount*CASE WHEN i.document_kind='credit_note' THEN -1 ELSE 1 END taxable_base_amount,
 c.tax_amount*CASE WHEN i.document_kind='credit_note' THEN -1 ELSE 1 END tax_amount,
 0::numeric(20,6)*CASE WHEN i.document_kind='credit_note' THEN -1 ELSE 1 END recoverable_amount,
 NULL::text withholding_treatment,c.rate_snapshot
FROM erp.sales_invoice_tax_components c JOIN erp.sales_invoices i ON i.company_id=c.company_id AND i.id=c.sales_invoice_id
JOIN erp.sales_invoice_lines l ON l.company_id=c.company_id AND l.sales_invoice_id=c.sales_invoice_id AND l.id=c.sales_invoice_line_id
JOIN erp.tax_jurisdictions j ON j.id=c.tax_jurisdiction_id JOIN erp.tax_rate_schedules rs ON rs.jurisdiction_id=c.tax_jurisdiction_id AND rs.id=c.rate_schedule_id
JOIN erp.tax_types tt ON tt.jurisdiction_id=rs.jurisdiction_id AND tt.id=rs.tax_type_id WHERE i.status='posted'
UNION ALL
SELECT 'purchase_bill',b.company_id,b.id,b.bill_no,b.tax_point_date,b.supplier_id,l.line_no,c.tax_jurisdiction_id,j.country_code,j.jurisdiction_code,j.name,
 tt.tax_family,tt.code,rs.schedule_code,b.currency_code,b.document_kind,CASE WHEN b.document_kind='supplier_credit' THEN -1 ELSE 1 END polarity,
 c.taxable_base_amount*CASE WHEN b.document_kind='supplier_credit' THEN -1 ELSE 1 END,
 c.tax_amount*CASE WHEN b.document_kind='supplier_credit' THEN -1 ELSE 1 END,c.recoverable_amount*CASE WHEN b.document_kind='supplier_credit' THEN -1 ELSE 1 END,
 NULL::text,c.rate_snapshot
FROM erp.purchase_bill_tax_components c JOIN erp.purchase_bills b ON b.company_id=c.company_id AND b.id=c.purchase_bill_id
JOIN erp.purchase_bill_lines l ON l.company_id=c.company_id AND l.purchase_bill_id=c.purchase_bill_id AND l.id=c.purchase_bill_line_id
JOIN erp.tax_jurisdictions j ON j.id=c.tax_jurisdiction_id JOIN erp.tax_rate_schedules rs ON rs.jurisdiction_id=c.tax_jurisdiction_id AND rs.id=c.rate_schedule_id
JOIN erp.tax_types tt ON tt.jurisdiction_id=rs.jurisdiction_id AND tt.id=rs.tax_type_id WHERE b.status='posted'
UNION ALL
SELECT 'payment_withholding',p.company_id,p.id,p.payment_no,p.payment_date,p.partner_id,NULL::integer,c.tax_jurisdiction_id,j.country_code,j.jurisdiction_code,j.name,
 tt.tax_family,tt.code,rs.schedule_code,p.currency_code,'payment',CASE WHEN p.direction='disbursement' THEN 1 ELSE -1 END,
 c.taxable_base_amount*CASE WHEN p.direction='disbursement' THEN 1 ELSE -1 END,
 c.tax_amount*CASE WHEN p.direction='disbursement' THEN 1 ELSE -1 END,0::numeric(20,6),c.tax_treatment,c.rate_snapshot
FROM erp.payment_tax_components c JOIN erp.payments p ON p.company_id=c.company_id AND p.id=c.payment_id
JOIN erp.tax_jurisdictions j ON j.id=c.tax_jurisdiction_id JOIN erp.tax_rate_schedules rs ON rs.jurisdiction_id=c.tax_jurisdiction_id AND rs.id=c.rate_schedule_id
JOIN erp.tax_types tt ON tt.jurisdiction_id=rs.jurisdiction_id AND tt.id=rs.tax_type_id WHERE p.status='posted';
COMMENT ON VIEW erp.v_tax_transaction_components_v2 IS 'Signed, currency-aware tax facts; country adapters define statutory aggregation.';

-- New signed open-item views preserve credit balances for the settlement service.
CREATE VIEW erp.v_open_ar_items_v2 WITH(security_invoker=true) AS
WITH totals AS (SELECT company_id,sales_invoice_id,sum(gross_amount) total_amount FROM erp.sales_invoice_lines GROUP BY company_id,sales_invoice_id),
apps AS (SELECT a.company_id,a.sales_invoice_id,sum(a.applied_document_amount) applied_amount FROM erp.ar_receipt_allocations a JOIN erp.payments p ON p.company_id=a.company_id AND p.id=a.payment_id WHERE p.status='posted' GROUP BY a.company_id,a.sales_invoice_id)
SELECT i.company_id,i.id sales_invoice_id,i.invoice_no,i.document_kind,i.partner_id,i.issue_date,i.due_date,i.currency_code,
 (CASE WHEN i.document_kind='credit_note' THEN -1 ELSE 1 END)*coalesce(t.total_amount,0) signed_document_total,
 coalesce(a.applied_amount,0) applied_amount,
 (CASE WHEN i.document_kind='credit_note' THEN -1 ELSE 1 END)*coalesce(t.total_amount,0)-coalesce(a.applied_amount,0) signed_open_amount
FROM erp.sales_invoices i LEFT JOIN totals t ON t.company_id=i.company_id AND t.sales_invoice_id=i.id LEFT JOIN apps a ON a.company_id=i.company_id AND a.sales_invoice_id=i.id WHERE i.status='posted';
CREATE VIEW erp.v_open_ap_items_v2 WITH(security_invoker=true) AS
WITH totals AS (SELECT company_id,purchase_bill_id,sum(gross_amount) total_amount FROM erp.purchase_bill_lines GROUP BY company_id,purchase_bill_id),
apps AS (SELECT a.company_id,a.purchase_bill_id,sum(a.applied_document_amount) applied_amount FROM erp.ap_disbursement_allocations a JOIN erp.payments p ON p.company_id=a.company_id AND p.id=a.payment_id WHERE p.status='posted' GROUP BY a.company_id,a.purchase_bill_id)
SELECT b.company_id,b.id purchase_bill_id,b.bill_no,b.document_kind,b.supplier_id,b.bill_date,b.due_date,b.currency_code,
 (CASE WHEN b.document_kind='supplier_credit' THEN -1 ELSE 1 END)*coalesce(t.total_amount,0) signed_document_total,
 coalesce(a.applied_amount,0) applied_amount,
 (CASE WHEN b.document_kind='supplier_credit' THEN -1 ELSE 1 END)*coalesce(t.total_amount,0)-coalesce(a.applied_amount,0) signed_open_amount
FROM erp.purchase_bills b LEFT JOIN totals t ON t.company_id=b.company_id AND t.purchase_bill_id=b.id LEFT JOIN apps a ON a.company_id=b.company_id AND a.purchase_bill_id=b.id WHERE b.status='posted';

-- Tax assessments must point to a posted journal when marked posted.
CREATE FUNCTION erp.guard_tax_assessment_posted() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF TG_OP='DELETE' THEN
   IF OLD.status='posted' THEN RAISE EXCEPTION 'Posted tax assessment is immutable'; END IF;
   RETURN OLD;
 END IF;
 IF NEW.status='posted' THEN
   IF NEW.journal_entry_id IS NULL OR NOT EXISTS(SELECT 1 FROM erp.journal_entries j WHERE j.company_id=NEW.company_id AND j.id=NEW.journal_entry_id AND j.status='posted') THEN
      RAISE EXCEPTION 'Posted tax assessment needs a posted journal'; END IF;
   IF NEW.calculated_at IS NULL THEN NEW.calculated_at:=clock_timestamp(); END IF;
 END IF;
 IF TG_OP='UPDATE' AND OLD.status='posted' THEN RAISE EXCEPTION 'Posted tax assessment is immutable'; END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER trg_tax_assessment_posted BEFORE INSERT OR UPDATE OR DELETE ON erp.tax_assessments
 FOR EACH ROW EXECUTE FUNCTION erp.guard_tax_assessment_posted();

-- Explicitly expose the baseline license view with the binding revision.
CREATE VIEW licensing.v_active_license_features_v2 WITH(security_invoker=true) AS
SELECT b.tenant_id,b.product_id,b.license_id,b.entitlement_revision,t.id license_term_id,
 f.feature_code,f.is_enabled,f.limit_value,t.starts_at,t.expires_at
FROM licensing.tenant_product_bindings b JOIN licensing.licenses l ON l.tenant_id=b.tenant_id AND l.id=b.license_id
JOIN licensing.license_terms t ON t.tenant_id=l.tenant_id AND t.license_id=l.id
JOIN licensing.plan_features f ON f.plan_version_id=t.plan_version_id
WHERE l.status='active' AND t.starts_at<=statement_timestamp() AND (t.expires_at IS NULL OR statement_timestamp()<t.expires_at);

-- Runtime tenant RLS for the new persistence. Global catalogs remain operator-controlled.
ALTER TABLE identity.tenant_memberships ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_membership_scope ON identity.tenant_memberships
 USING(tenant_id=erp.current_tenant_id() AND (user_id=identity.current_user_id() OR identity.current_service_id() IS NOT NULL))
 WITH CHECK(tenant_id=erp.current_tenant_id());
ALTER TABLE identity.company_memberships ENABLE ROW LEVEL SECURITY;
CREATE POLICY company_membership_scope ON identity.company_memberships
 USING(tenant_id=erp.current_tenant_id() AND (user_id=identity.current_user_id() OR identity.current_service_id() IS NOT NULL))
 WITH CHECK(tenant_id=erp.current_tenant_id());
ALTER TABLE identity.roles ENABLE ROW LEVEL SECURITY;
CREATE POLICY role_company_scope ON identity.roles
 USING(EXISTS(SELECT 1 FROM erp.companies c WHERE c.id=company_id AND c.tenant_id=erp.current_tenant_id()))
 WITH CHECK(EXISTS(SELECT 1 FROM erp.companies c WHERE c.id=company_id AND c.tenant_id=erp.current_tenant_id()));
ALTER TABLE identity.role_permissions ENABLE ROW LEVEL SECURITY;
CREATE POLICY role_permission_company_scope ON identity.role_permissions
 USING(EXISTS(SELECT 1 FROM erp.companies c WHERE c.id=company_id AND c.tenant_id=erp.current_tenant_id()))
 WITH CHECK(EXISTS(SELECT 1 FROM erp.companies c WHERE c.id=company_id AND c.tenant_id=erp.current_tenant_id()));
ALTER TABLE identity.company_role_assignments ENABLE ROW LEVEL SECURITY;
CREATE POLICY role_assignment_company_scope ON identity.company_role_assignments
 USING(EXISTS(SELECT 1 FROM erp.companies c WHERE c.id=company_id AND c.tenant_id=erp.current_tenant_id()))
 WITH CHECK(EXISTS(SELECT 1 FROM erp.companies c WHERE c.id=company_id AND c.tenant_id=erp.current_tenant_id()));
ALTER TABLE identity.service_accounts ENABLE ROW LEVEL SECURITY;
CREATE POLICY service_account_tenant_scope ON identity.service_accounts
 USING(tenant_id=erp.current_tenant_id()) WITH CHECK(tenant_id=erp.current_tenant_id());
ALTER TABLE identity.service_account_permissions ENABLE ROW LEVEL SECURITY;
CREATE POLICY service_account_permission_scope ON identity.service_account_permissions
 USING(tenant_id=erp.current_tenant_id()) WITH CHECK(tenant_id=erp.current_tenant_id());

DO $$
DECLARE t text;
BEGIN
 FOREACH t IN ARRAY ARRAY[
  'company_policy_state','company_module_settings','company_audit_events',
  'integration_connections','inventory_positions','inventory_documents','inventory_document_lines','tax_return_revisions',
  'tax_return_revision_lines','tax_submission_attempts'
 ] LOOP
  EXECUTE format('ALTER TABLE erp.%I ENABLE ROW LEVEL SECURITY',t);
  EXECUTE format('CREATE POLICY %I ON erp.%I USING (EXISTS(SELECT 1 FROM erp.companies c WHERE c.id=company_id AND c.tenant_id=erp.current_tenant_id())) WITH CHECK (EXISTS(SELECT 1 FROM erp.companies c WHERE c.id=company_id AND c.tenant_id=erp.current_tenant_id()))', 'tenant_'||t,t);
 END LOOP;
END $$;
ALTER TABLE erp.api_command_receipts ENABLE ROW LEVEL SECURITY;
CREATE POLICY command_receipt_tenant_scope ON erp.api_command_receipts
 USING(tenant_id=erp.current_tenant_id() AND
       (company_id IS NULL OR EXISTS(SELECT 1 FROM erp.companies c WHERE c.id=company_id AND c.tenant_id=erp.current_tenant_id())))
 WITH CHECK(tenant_id=erp.current_tenant_id() AND
       (company_id IS NULL OR EXISTS(SELECT 1 FROM erp.companies c WHERE c.id=company_id AND c.tenant_id=erp.current_tenant_id())));
ALTER TABLE erp.background_jobs ENABLE ROW LEVEL SECURITY;
CREATE POLICY background_job_tenant_scope ON erp.background_jobs
 USING(tenant_id=erp.current_tenant_id() AND
       (company_id IS NULL OR EXISTS(SELECT 1 FROM erp.companies c WHERE c.id=company_id AND c.tenant_id=erp.current_tenant_id())))
 WITH CHECK(tenant_id=erp.current_tenant_id() AND
       (company_id IS NULL OR EXISTS(SELECT 1 FROM erp.companies c WHERE c.id=company_id AND c.tenant_id=erp.current_tenant_id())));
ALTER TABLE licensing.tenant_product_bindings ENABLE ROW LEVEL SECURITY;
CREATE POLICY binding_tenant_scope ON licensing.tenant_product_bindings USING(tenant_id=erp.current_tenant_id()) WITH CHECK(tenant_id=erp.current_tenant_id());
ALTER TABLE licensing.billing_orders ENABLE ROW LEVEL SECURITY;
CREATE POLICY billing_order_tenant_scope ON licensing.billing_orders USING(tenant_id=erp.current_tenant_id()) WITH CHECK(tenant_id=erp.current_tenant_id());
ALTER TABLE licensing.billing_events ENABLE ROW LEVEL SECURITY;
CREATE POLICY billing_event_tenant_scope ON licensing.billing_events USING(tenant_id=erp.current_tenant_id()) WITH CHECK(tenant_id=erp.current_tenant_id());
ALTER TABLE platform.schema_releases ENABLE ROW LEVEL SECURITY;
CREATE POLICY platform_release_operator_scope ON platform.schema_releases USING(false) WITH CHECK(false);

-- Seed common action codes. Product-specific country packs add their own reviewed codes.
INSERT INTO identity.permissions(code,description) VALUES
 ('company.modules.manage','Change company module mode'),('company.settings.manage','Change typed company settings'),
 ('sales.invoice.view','Read sales invoices'),('sales.invoice.edit_draft','Edit invoice drafts'),('sales.invoice.post','Post sales invoices'),
 ('purchasing.bill.post','Post supplier bills'),('payments.post','Post payments'),('inventory.post','Post stock documents'),
 ('manufacturing.order.post','Post production effects'),('accounting.entry.post','Post manual journals'),('accounting.entry.reverse','Reverse journals'),
 ('accounting.period.close','Close fiscal periods'),('accounting.period.reopen','Reopen fiscal periods'),
 ('tax.return.submit','Submit tax returns'),('tax.configuration.manage','Manage tax setup'),('reports.view','View reports')
ON CONFLICT(code) DO NOTHING;

-- Keep direct runtime privileges minimal; deployment migrations grant ownership separately.
REVOKE ALL ON SCHEMA platform FROM PUBLIC;
REVOKE ALL ON ALL TABLES IN SCHEMA platform FROM PUBLIC;

COMMIT;
