---
name: build-erp-accounting-backend
description: Build and extend the supplied multi-tenant ERP accounting backend as a production Django 5.2/DRF and PostgreSQL 18 modular monolith. Use when implementing or modifying its database schema, Django apps, APIs, accounting posting, licensing, company module controls, taxation, inventory, manufacturing, jobs, deployment, or test suite from the provided architecture, SQL, README, and test-case artifacts.
---

# Build ERP Accounting Backend

## Mission

Implement the ERP backend described by the supplied architecture, SQL, README, and test-case artifacts. Move directly from the current repository state to the smallest validated development slice. Preserve the database contract and financial invariants; do not redesign the schema from memory.

The default shape is one modular Django application deployed as separate API, worker, and scheduler processes. PostgreSQL is authoritative. Redis is replaceable cache and broker infrastructure. Celery handles durable work represented by PostgreSQL jobs and outbox rows. Keep the same application image, migrations, and API contracts for managed cloud and VPS deployments.

## Source-of-truth loading

At the start of a task, locate these artifacts recursively. Uploaded copies may contain a suffix such as (1).

- erp-backend-architecture-and-apis.md
- erp-accounting-backend-schema.sql
- erp-accounting-backend-upgrade.sql
- erp-accounting-backend-schema-README.md
- erp-backend-test-cases.md

Run the bundled indexer before reading long files:

~~~bash
python3 <skill-root>/scripts/context_index.py --root <project-root> --headings
python3 <skill-root>/scripts/context_index.py --root <project-root> --query "posting|RLS|module|license|tax"
~~~

Use the architecture document for behavior, transaction rules, endpoint contracts, and deployment decisions. Use the full schema for new installations and object names. Use the upgrade SQL for an existing baseline. Use the README for Django/RLS/migration integration. Use the test-case document for acceptance IDs and regression coverage.

Do not read all five artifacts into context. Read only the relevant heading range or SQL object definition for the current slice. Use rg for headings, table/function/view names, test IDs, and endpoint paths; then use sed for the smallest surrounding range. If an artifact is missing, do not recreate financial behavior from memory: report the missing source and continue only with a safe repository inspection or a clearly bounded scaffolding task.

Create a local source manifest with paths and SHA-256 values before changing schema or migrations. Treat a changed source hash as a contract review trigger.

## Non-negotiable architecture

- Django 5.2 LTS, DRF, psycopg 3, PostgreSQL 18, Celery, and separate Redis cache/broker roles.
- Use a custom UUID user model backed by identity.users before the first Django auth migration.
- Keep database objects schema-qualified. Preserve SQL-owned functions, triggers, views, composite foreign keys, RLS, and defaults with RunSQL or SeparateDatabaseAndState.
- Use a modular monolith first. Do not split accounting posting into internal network services.
- Keep views thin, serializers focused on shape, services focused on commands and transactions, selectors focused on bounded reads, and tasks focused on loading durable jobs/events then calling the same services.
- Do not put posting, licensing, tax, inventory, or lifecycle effects in Model.save, broad signals, or unrestricted ModelViewSet updates.

## Authorization and isolation protocol

Every request or worker command follows this order:

1. Authenticate the user or scoped service account.
2. Resolve tenant membership.
3. Resolve company membership when the command is company-scoped.
4. Set transaction-local app.tenant_id and app.user_id or app.service_id only after authorization.
5. Check current permission.
6. Check designated tenant/product entitlement and term validity.
7. Check company module mode, dependencies, and write/read capability.
8. Execute the bounded command transaction.

Use a runtime role without BYPASSRLS. Never trust a client-supplied tenant header. Clear context at transaction end, which is required for PgBouncer transaction pooling. Platform catalog and signing operations use separate operator credentials and routes.

Company module writes call the guarded database/service protocol: lock company_policy_state, validate If-Match revision, entitlement, permission, dependencies, and requested modes, apply the whole batch, increment revision, append audit events, and write an outbox event. Required modules are core, accounting, and tax_calculation. Modes are enabled, read_only, and disabled. A company administrator cannot issue licenses, edit global tax rates, or publish tax packs.

## Financial and licensing invariants

- A posting command atomically commits its source document, immutable tax snapshots, journal, inventory/allocation effects, audit, outbox intent, and idempotency result.
- Posted journal entries, posted source documents, tax components, stock movements, audit events, billing events, signed grants, and published plan definitions are immutable. Corrections use explicit reversal/amendment workflows.
- Use API receipts for command idempotency. A retry with the same key returns the stored result; a changed payload returns a conflict. Unknown commit is resolved by replay, never by issuing a new key.
- Lock in the documented order: idempotency receipt, entitlement coordinator/license, company policy, source aggregate, fiscal period, then dependent rows in deterministic order. Posting takes a shared fiscal-period lock; close takes an exclusive lock.
- License numbers are never stored in plaintext. Resolve one designated tenant/product binding. Monthly/lifetime terms, expiry, suspension, revocation, activation quota, renewal, and entitlement_revision are authoritative on the primary database.
- Calculate taxes locally from versioned effective-dated country packs. Store jurisdiction, rule version, base, rate, rounding, recoverability, account role, sign, and currency snapshots with the source document. Keep Pakistan sales tax, withholding, and income-tax workflows as separate reviewed components; never collapse them into one company percentage.
- Keep government, payment, ecommerce, and object-storage calls outside posting transactions. Persist intent in outbox/job tables, then deliver with bounded retries and idempotent consumers.
- Inventory positions are rebuildable projections. Stock movements remain immutable quantity/value history. Reservations must lock deterministically and cannot exceed the selected availability policy.

## Development workflow

Work one slice at a time. Begin each turn with a short status statement containing current phase, files inspected, the exact change, and the focused validation command. Do not restate the entire architecture.

### Phase 0: inspect and baseline

1. Run context_index.py and contract_check.py.
2. Inspect the existing repository, settings, migration graph, dependency lockfile, and test configuration.
3. Decide whether this is a new install or an existing baseline upgrade.
4. Record missing decisions as explicit blockers; do not silently invent tax rates, costing rules, negative-stock policy, billing timezone, or unsupported returns/refunds.
5. Establish a clean PostgreSQL 18 test database and a repeatable environment command.

### Phase 1: foundation

Create settings, custom identity model, tenant/company scope middleware or service, transaction-local PostgreSQL context helper, error envelope, request ID, permission primitives, and schema-qualified model/repository mappings.

Add integration tests for login, tenant/company membership, pooled-connection context cleanup, RLS, and service-account scope before business writes.

### Phase 2: SQL and migration adoption

Convert the supplied full schema or upgrade file into ordered, reviewable Django migrations. Do not run the complete SQL on every web startup and do not blindly nest its transaction inside an atomic migration.

Preserve SQL functions, triggers, views, policies, composite relationships, UUID defaults, and indexes. Use explicit migration state where the ORM cannot represent a database object. Preflight existing fixed-term licenses, reservations, posted data, and row-version defaults before enforcing new constraints.

### Phase 3: accounting core

Implement accounts, journals, fiscal periods, sequences, dimensions, posting, reversal, and reports. Use command services with explicit locks and idempotency. Test balanced journals, closed periods, shared versus exclusive period locks, posted immutability, multi-currency rounding, and trial-balance reconciliation before adding sales or inventory.

### Phase 4: module access and licensing

Implement capabilities, company module settings, dependencies, policy revisions, audit, license binding, term validity, activation quota, renewal orders, suspension, revocation, and cache invalidation. Guard every protected write in the service and database protocol. Test stale If-Match, concurrent module changes, expiry boundaries, quota races, and plan immutability.

### Phase 5: commercial domains

Implement in this order:

1. Parties and tax registrations.
2. Sales invoice drafts, tax calculation, posting, and credit corrections.
3. Purchase bill drafts, tax calculation, posting, and supplier credits.
4. Payments, withholding, AR/AP allocation, and reversal.
5. Warehouses, stock documents, reservations, movements, costing, and projections.
6. BOMs, production orders, material issues, outputs, completion, and cancellation.

Use source-document commands. Do not expose unrestricted journal-line, stock-movement, or allocation insertion endpoints.

### Phase 6: taxation, jobs, and integrations

Implement effective-dated tax configuration, country-pack adapters, tax snapshots, returns, frozen revisions, assessments, submission attempts, outbox relay, inbox deduplication, durable job leases, webhook authentication, and provider retry handling. Add an approved fixture pack before enabling a country filing workflow. A tax provider outage must not corrupt or partially post a document.

### Phase 7: reporting and deployment

Implement bounded selectors and report jobs for trial balance, GL, AR/AP, historical aging, inventory valuation, tax components, and exports. Route read-your-write and financial gates to primary. Add managed-cloud and VPS settings without changing domain services. Add metrics for latency, lock waits, connection-pool saturation, queue age, outbox lag, replica lag, and failed jobs.


## Command implementation template

For every state-changing API or worker command:

1. Validate request shape and expected revision in the serializer/command input.
2. Begin a short database transaction.
3. Set and verify tenant/user/company context.
4. Claim or replay the API receipt using the idempotency key.
5. Lock entitlement coordinator/license and company policy in global order.
6. Check permission, license, module mode, dependencies, lifecycle, period, and source revision.
7. Lock the source aggregate and dependent rows in deterministic order.
8. Calculate and validate domain facts locally, including tax and currency rounding.
9. Write immutable snapshots and authoritative rows.
10. Create audit and outbox/job intent in the same transaction.
11. Complete the receipt with the committed result.
12. Commit, then return the result or enqueue external work.

On deadlock or serialization failure, roll back the whole transaction and retry a bounded number of times with jitter and the same key. On a network timeout after commit, replay the same key.

## API conventions

Use the versioned namespaces already specified by the architecture:

- /api/v1/auth
- /api/v1/tenants/{tenant_id}
- /api/v1/tenants/{tenant_id}/companies/{company_id}
- /platform-api/v1

Require Idempotency-Key for posting, payments, stock, production, module changes, renewals, and durable jobs. Require If-Match for draft aggregate updates and module policy changes. Use stable JSON errors with code, message, field details, request ID, and retryability. Use cursor/keyset pagination and bounded filters. Return 202 for long exports, tax submissions, and other durable jobs.

## Token-efficient interaction protocol

- Load only one source section and one code area per slice.
- Prefer rg, sed, and the bundled scripts over whole-file reads.
- Summarize discoveries in five bullets or fewer.
- Make one coherent code change per turn; do not scaffold every domain before validating the foundation.
- After each change, report files changed, tests run, failures, and the next smallest slice.
- Avoid speculative abstractions, premature microservices, and duplicate implementations.
- Never invent a successful test or benchmark. Mark planned, local smoke, and live staging evidence separately.
- Ask at most one focused question only after completing all non-blocked work.

## Definition of done

A slice is complete when:

- code and migrations are type/lint clean;
- the relevant schema objects and SQL invariants are preserved;
- focused unit, PostgreSQL integration, API, and concurrency tests pass;
- tenant/company/license/module authorization is enforced at the command boundary;
- retries are idempotent and no partial financial effect remains;
- metrics and structured errors make failures diagnosable;
- the change is small enough to review and the next slice is explicit.

A release is complete only when all P0 tests in the supplied test-case document pass against real PostgreSQL, restore/failover evidence is current, country fixtures are approved, and measured performance/RPO/RTO budgets are recorded.

## Bundled resources

- references/architecture-contract.md: compact contract extracted from the supplied artifacts.
- references/source-map.md: low-token source-loading map and search recipes.
- scripts/context_index.py: print headings or targeted excerpts without loading entire documents.
- scripts/contract_check.py: verify that the supplied artifact set and required schema/API markers are present.
