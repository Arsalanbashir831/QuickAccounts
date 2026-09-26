# ERP accounting backend SQL package

This package extends the earlier normalized ERP accounting schema for the Django backend architecture. It supports the same online managed-cloud and self-managed-VPS deployments.

## Files

| File | Use |
|---|---|
| `erp-accounting-backend-schema.sql` | Complete install for a new PostgreSQL 18 database. It includes the existing accounting/tax/inventory/licensing baseline plus identity, company module settings, roles, licenses, jobs, integrations, inventory projections, tax revisions, RLS, views, and concurrency functions. |
| `erp-accounting-backend-upgrade.sql` | One-time extension for a database that already has `erp-accounting-schema.sql`. Review data preconditions before running it. |
| `erp-accounting-backend-schema-README.md` | Installation, Django integration, security, and migration notes. |

The complete schema contains the baseline 71 tables and 9 views plus the backend additions. The validated output currently contains 118 tables and 16 views across `erp`, `licensing`, `identity`, and `platform` schemas.

## New database installation

Create an empty database and run the complete file through one controlled migration job:

```bash
createdb erp_accounting
psql "$DATABASE_URL" \
  -v ON_ERROR_STOP=1 \
  -f erp-accounting-backend-schema.sql
```

The script does not create database roles, passwords, or a database. Create those through your cloud/VPS secret-management process. Use a schema-owner/migration role for installation and a separate runtime role for Django and Celery.

## Existing database upgrade

Take a tested backup first. Run the extension against a clone, inspect the data checks below, then run it once against production during a controlled migration window:

```bash
pg_dump "$DATABASE_URL" --format=custom --file=erp-before-backend-upgrade.dump
psql "$DATABASE_URL" \
  -v ON_ERROR_STOP=1 \
  -f erp-accounting-backend-upgrade.sql
```

The upgrade expects these baseline objects to exist: `erp.companies`, `erp.stock_movements`, `erp.tax_returns`, `licensing.licenses`, and the other objects in `erp-accounting-schema.sql`. It runs in a transaction, so a failed upgrade rolls back database changes that PostgreSQL can roll back.

Before applying to a populated system:

1. Check for any fixed-term plan version with a null `term_count`, and any fixed license term with a null `term_count_snapshot` or `expires_at`. Correct the commercial data through an approved migration.
2. Reconcile existing stock movements and active reservations. The new `erp.inventory_positions` projection rejects `reserved_quantity > on_hand_quantity` in this release. Rebuild or correct inconsistent historical data before the backfill.
3. Review existing companies. Each company receives required modules (`core`, `accounting`, `tax_calculation`) enabled and optional modules disabled. Provisioning audit rows created during migration have no actor; mark the migration actor in a separate deployment audit record if required.
4. Review plan publication order. Published plan versions and their features are immutable. Insert features before publishing a new plan version.
5. Review database size and locks. The script includes ordinary indexes and does not use `CREATE INDEX CONCURRENTLY`; for large production tables, split index operations into separate non-atomic migrations.
6. Check the document-number and revision behavior in the application. Existing rows receive `row_version = 1`; application updates must use `WHERE row_version = :expected` and increment the row version transactionally.

The upgrade is a reviewed starting migration, not a substitute for a staged expand/backfill/enforce rollout on a large live database.

## Django integration

Use a custom UUID `AbstractBaseUser` model backed by `identity.users` before the first Django auth migration. Keep business tables schema-qualified, for example:

```python
class SalesInvoice(models.Model):
    class Meta:
        db_table = '"erp"."sales_invoices"'
```

Preserve SQL functions, triggers, views, composite foreign keys, and RLS using `RunSQL` or `SeparateDatabaseAndState`. Do not run this complete installation file on every web-process startup.

For external PgBouncer transaction pooling, start with:

```python
DATABASES["default"].update({
    "CONN_MAX_AGE": 0,
    "ATOMIC_REQUESTS": False,
    "DISABLE_SERVER_SIDE_CURSORS": True,
})
```

Every request or worker command must set tenant and identity context inside its own transaction:

```sql
SELECT set_config('app.tenant_id', :tenant_uuid, true);
SELECT set_config('app.user_id', :user_uuid, true);
-- or app.service_id for a scoped service account
```

The third parameter is `true`, so the setting is transaction-local. Set it only after the application has authenticated and authorized the tenant/company route. Do not trust a client-supplied tenant header. A worker sets the same context after loading its durable job.

Use a non-owner runtime role without `BYPASSRLS`. Keep migrations, schema ownership, platform catalog publication, and signing-key operations on separate protected credentials. The `platform` schema is operator-only; the customer company admin can change a company module mode but cannot issue a license or publish a global tax pack.

## Company modules and permissions

The following tables implement the admin enable/disable behavior:

- `erp.module_definitions` and `erp.module_dependencies` define the release catalog.
- `erp.company_policy_state` is the revision/lock row.
- `erp.company_module_settings` stores `enabled`, `read_only`, or `disabled` per company.
- `erp.company_audit_events` records each policy change.
- `identity.users`, memberships, roles, and permission bridges provide scoped authorization.

Use `erp.set_company_modules(company_id, product_id, expected_revision, changes_json, reason)` from the Django service after setting tenant/user context. It locks the policy row, validates the license and dependencies, applies a batch, increments the policy revision, records audit rows, and writes an outbox event. The API still needs to enforce the same permission and idempotency receipt before calling it.

Use `erp.assert_company_write(company_id, product_id, module_code, permission_code)` at the beginning of a protected business transaction. It checks the active company, designated license term, licensed feature, module dependency chain, and action permission. Cached capabilities are for UI acceleration; this function and the surrounding service transaction remain authoritative.

## Licensing and taxation

A license number is never stored in plaintext. Store its normalized cryptographic digest in `licensing.licenses.license_number_hash`; use `license_number_last4` for ordinary display. `licensing.tenant_product_bindings` chooses the designated license for a tenant/product and carries an `entitlement_revision` for cache invalidation.

A lifetime term uses `term_unit_snapshot = 'lifetime'`, `term_count_snapshot IS NULL`, and `expires_at IS NULL`. A monthly or yearly term requires a positive count and an exclusive non-null expiry. Calendar-month boundary calculation, payment verification, renewal, and license status changes belong in the Django licensing service; the database blocks inconsistent snapshots and overlapping terms.

Tax country packs are deliberately metadata-driven. `erp.tax_packs` records the adapter/version/source hash; it does not invent statutory rates. Pakistan sales tax, withholding/income-tax assessment, exemptions, registrations, filing boxes, rounding, and effective dates must be supplied as a reviewed country pack and tested with official requirements before enabling `tax_filing` for a company.

## Concurrency and durability functions

- `erp.post_journal_entry` and the posted-entry trigger use a shared fiscal-period lock. Period close uses `erp.close_fiscal_period`, which takes the exclusive period lock and must run reconciliation checks in the service.
- `erp.claim_outbox_events` and `erp.complete_outbox_event` implement leased, at-least-once relay delivery.
- `erp.claim_jobs` implements bounded Celery job leases. A worker completes a job only when its claim token still matches.
- `erp.inventory_positions` is a rebuildable availability projection. Stock movements remain the immutable quantity/value history; reservations and stock commands need deterministic row locks.
- Tax returns use frozen `erp.tax_return_revisions` and `erp.tax_submission_attempts` so amendments and provider retries remain auditable.

Keep external tax, payment, ecommerce, and object-storage calls outside financial posting transactions. Persist intent in the outbox/job tables first, then deliver with bounded retries and idempotent consumers.

## Validation performed for this package

The files were parsed with a PostgreSQL SQL parser and executed in a PostgreSQL 18-compatible PGlite validation database. The checks covered the complete install, the baseline-plus-upgrade path, company module provisioning, license binding and lifetime term validation, the module write gate, API receipt completion, and job claiming.

No production database, provider account, load test, restore drill, or country tax filing was executed. Run those tests in your CI/staging PostgreSQL environment before launch.
