# QuickAccounts ERP accounting backend

Production-oriented Django 5.2/DRF foundation for the supplied multi-tenant accounting
contract. PostgreSQL owns financial invariants, RLS, functions, triggers, and views; Django
adopts that schema through a one-time checksum-guarded migration.

## Local foundation setup

Requirements: Python 3.13, `uv`, Docker, and Docker Compose.

```bash
cp .env.example .env
docker compose up -d postgres redis-cache redis-broker
uv sync
DATABASE_URL=postgresql://quickaccounts_owner:owner-development-only@localhost:55432/quickaccounts \
  uv run python manage.py migrate
docker compose exec -T postgres psql -U quickaccounts_owner -d quickaccounts \
  < deploy/vps/grant-runtime.sql
uv run python manage.py check
uv run pytest -m unit
```

The first migration installs `erp-accounting-backend-schema.sql` only when its SHA-256
matches `docs/source-manifest.sha256`. Never point migrations at the runtime role. After
migration, grant only the required schema/table/function privileges to
`quickaccounts_runtime`; it is deliberately non-owner and has no `BYPASSRLS`. For local
development, apply `deploy/vps/grant-runtime.sql` from the repository by piping it to
the container's `psql`, as shown above.

## Processes

```bash
uv run gunicorn config.wsgi:application
uv run celery -A config worker --loglevel=INFO
uv run celery -A config beat --loglevel=INFO
# For the provider-free PostgreSQL job queue, run bounded sweeps from a trusted scheduler:
uv run python manage.py run_internal_jobs --tenant-id TENANT_UUID --actor-user-id USER_UUID --queue imports
uv run python manage.py run_internal_jobs --tenant-id TENANT_UUID --actor-user-id USER_UUID --queue reports --company-id COMPANY_UUID
```

## Implemented API surface

- `/api/v1/auth/csrf`, `/api/v1/auth/session`, and `/api/v1/auth/me`
- `/api/v1/tenants` and `/api/v1/tenants/{tenant_id}/companies`
- Company-scoped accounting accounts, journals, periods, dimensions, and posting rules
- Manual journal draft create/read/update, idempotent post, and idempotent reversal commands
- Idempotent fiscal-period close/reopen with revision checks and reconciliation blockers
- Trial balance, period activity, and general ledger with running functional-currency balances
- Company capabilities, module configuration, batch validation/change, and module audit history
- Company-scoped business-partner search/create/detail/update with revision and audit controls
- Partner address list/create/versioned-update and effective-dated tax-registration list/create
- Shared stock/non-stock/service item catalog, UOM lookup, and item accounting profiles
- Revisioned warehouse management and stock balances with sellable/quarantine/damaged/
  supplier-return segregation; non-sellable locations reject sales and reservations
- Typed stock drafts, atomic receipts/transfers/approved losses, lot/serial validation,
  revisioned reservations/release, deferred invoice shipments and projection reconciliation/rebuild
- Sales-invoice draft aggregates with bounded line replacement, address snapshots, optimistic
  revisions, and deterministic effective-dated local tax calculation
- Atomic, idempotent sales-invoice posting with balanced journal and optional untracked-stock
  effects, plus source-linked full-line credit notes with reversed AR, revenue, and tax polarity
- Purchase-bill drafts, deterministic input-tax allocation, atomic posting, and linked supplier credits
- Payment draft CRUD, withholding snapshots, locked AR/AP allocations, atomic posting and reversal
- Current open receivables/payables and cutoff aging, including signed unapplied payment balances
- Tenant license summaries, activation allocation/cleanup, and renewal-order creation
- Platform-operator license issuance, suspension/revocation, and plan-version publication
- `/api/schema` and `/api/docs` for the generated OpenAPI contract

Mutation routes enforce company membership, action permission, active designated product
license, module policy, RLS context, and database financial constraints. Posting commands
require `Idempotency-Key`; draft updates and posting require integer `If-Match` revisions.

Cache and broker use separate Redis endpoints. `ATOMIC_REQUESTS` is disabled, database
connections are not persisted across requests, and server-side cursors are disabled for
PgBouncer transaction pooling compatibility.

## Structure

- `apps/identity` contains the custom UUID user mapping and authentication ownership.
- `apps/database` installs the immutable SQL contract before Django auth migrations.
- Domain apps mirror the architecture and will contain models, APIs, services, selectors,
  tasks, migrations, and tests as each bounded slice is implemented.
- `common/api` owns request IDs and the stable error envelope.
- `common/db` owns transaction-local tenant and actor context.
- `tests/` follows the unit/integration/API/security/concurrency layout from the test plan.
- `deploy/cloud` and `deploy/vps` separate deployment concerns without changing domain code.

## Current boundary

Accounting Phase 3 and access/licensing Phase 4 are implemented. The approved
Phase 5 backend scope now includes partner and item masters, sales and purchase
posting, linked corrections, payment settlement and aging, inventory costing,
manufacturing execution, and the expanded retail/wholesale return flow.
Phase 4 includes effective
capabilities, revision-checked and idempotent module policy changes, dependency and entitlement
validation, audit/outbox persistence, license summaries, activation quota enforcement,
explicit credential redemption and tenant/product binding, renewal-order creation, stable expiry
denials, and protected platform suspend/resume/revoke operations. The production runtime role can
read company policy but cannot mutate policy state or module settings outside the guarded database
protocol. PostgreSQL tests cover stale revisions, atomic batches, company isolation, disable/write
coordination, activation quota races, published-plan immutability, term validity, and calendar-month
boundaries across month-end and DST.

Verified payment-provider fulfillment and webhook processing remain Phase 6 integration work.
The provider-free Phase 6 internal job slice supports durable partner import/export,
scoped job status, retries, and an atomic internal outbox relay. See the
[internal jobs contract](docs/phase-6-internal-jobs.md) for worker scheduling and limits.
Signed offline grants remain disabled until a signing service and its subject/audience/signature
verification contract are selected. Country tax packs and the explicitly blocked
inventory/return variants remain product decisions and are not guessed.

Linked customer returns, inspection, historical-cost stock disposition, confirmed
cash refunds and replacement invoice settlement are implemented under the approved
conservative rules. See [the return flow contract](docs/return-stock-implementation-plan.md)
for endpoints, accounting boundaries and expanded diagram-alignment tasks.
Returned-stock repair jobs, mandatory QC release, future-invoice credit
applications, independent custody intake, dead-stock cases, supplier claims,
physical returns, sales orders and explicit delivery confirmation are implemented
within the conservative boundaries in the flow contract.

The current [Phase 5 backend contract](docs/phase-5-completion.md) summarizes
the completed workflows and the separate production-release gates.

P5.8–P5.9 inventory commands and historical costing, P5.10 BOM and production
drafts, P5.11 material-only production execution, and P5.12 cross-domain
verification are implemented. Bounded valuation-to-GL reconciliation is available
at `reports/inventory-valuation-reconciliation?as_of=YYYY-MM-DD`; current
layer/stock/reservation integrity is available at `inventory/cost-reconciliation`.
Partial untracked supplier returns and provable one-hop transfers are supported;
tracked returns and supplier-return cost variances remain blocked. Labor/overhead
and automatic scrap/variance posting are not part of the approved Phase 5 policy.
See the [Phase 5 backend contract](docs/phase-5-completion.md) for the current
scope and release boundary.
