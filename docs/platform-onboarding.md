# Platform-controlled onboarding

Only an active superuser can call `/platform-api/v1/` commands. Business owners use
the normal JWT API to access companies to which the platform has assigned them;
they cannot create tenants or bind licenses. Every platform POST requires an
`Idempotency-Key` and a reason, and records an operator audit event. The initial
owner password is write-only, validated, hashed, and must be delivered securely
out of band. Do not send it by email or include it in logs.

1. `POST /platform-api/v1/tenants` creates a tenant, initial company, owner user,
   and both memberships in one transaction. Supply `name`, `company_code`,
   `company_name`, `currency`, `timezone_name`, `business_type`, `owner_email`,
   `owner_password`, and `reason`.
2. `POST /platform-api/v1/tenants/{tenant_id}/companies` adds another company
   for an existing owner. Supply `code`, `legal_name`, `currency`,
   `timezone_name`, `business_type`, `owner_user_id`, and `reason`.
3. `POST /platform-api/v1/plan-versions` creates a draft version for an existing
   active plan, including its features. Publish the returned `id` with
   `POST /platform-api/v1/plan-versions/{plan_version_id}/publish`, then use it
   with `POST /platform-api/v1/licenses` to issue a license to the tenant.
4. `POST /platform-api/v1/licenses/{license_id}/assign` with `reason` binds the
   active, in-term license to its tenant and product. An existing assignment
   blocks replacement; an operator must explicitly resolve the old assignment
   before using this endpoint. The former tenant-facing `/license/redeem` route
   is removed.

In VPS settings, configure `DATABASE_PLATFORM_URL` for a separate privileged
database login. Platform writes fail closed without it. The ordinary
`DATABASE_URL` role must retain no access to the `platform` schema. After
creating the dedicated login, apply `deploy/vps/grant-platform.sql` as a
PostgreSQL superuser. The script grants `BYPASSRLS` to the dedicated platform
role because tenant creation begins before a tenant ID can be set for the
tenant-scoped policies. Never use this credential for tenant API requests.
For an existing installation with this error, reapply the grant script and
restart the API with `DATABASE_PLATFORM_URL` pointing to that role. The test
settings intentionally use the default test database instead.

The initial company currency must exist in `erp.currencies`. Migration
`database.0045_seed_iso4217_currencies` seeds 165 current ISO 4217 List One
codes with defined numeric minor units, including PKR. It inserts missing
codes only and leaves existing names, precision, and active flags unchanged.
Codes without numeric minor units are omitted. ISO revisions require a new
reviewed data migration; the running API does not fetch currency data online.
