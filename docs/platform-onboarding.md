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
3. Publish a plan version through the existing operator workflow, then
   `POST /platform-api/v1/licenses` to issue a license to the tenant.
4. `POST /platform-api/v1/licenses/{license_id}/assign` with `reason` binds the
   active, in-term license to its tenant and product. An existing assignment
   blocks replacement; an operator must explicitly resolve the old assignment
   before using this endpoint. The former tenant-facing `/license/redeem` route
   is removed.

In VPS settings, configure `DATABASE_PLATFORM_URL` for a separate privileged
database login. Platform writes fail closed without it. The ordinary
`DATABASE_URL` role must retain no access to the `platform` schema. After
creating the dedicated login, apply `deploy/vps/grant-platform.sql` as schema
owner. The test settings intentionally use the default test database instead.
