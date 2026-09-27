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
- Sales-invoice draft aggregates with bounded line replacement, address snapshots, optimistic
  revisions, and deterministic effective-dated local tax calculation
- Atomic, idempotent sales-invoice posting with balanced journal and optional untracked-stock
  effects, plus source-linked full-line credit notes with reversed AR, revenue, and tax polarity
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

Accounting Phase 3 and access/licensing Phase 4 are implemented. Phase 5 is underway: partner
master data, the shared item/service catalog, item accounting profiles, deterministic
sales-invoice calculation, atomic posting, and linked credit corrections are complete; purchase
bill drafts, posting, and supplier credits are next. Phase 4 includes effective
capabilities, revision-checked and idempotent module policy changes, dependency and entitlement
validation, audit/outbox persistence, license summaries, activation quota enforcement,
explicit credential redemption and tenant/product binding, renewal-order creation, stable expiry
denials, and protected platform suspend/resume/revoke operations. The production runtime role can
read company policy but cannot mutate policy state or module settings outside the guarded database
protocol. PostgreSQL tests cover stale revisions, atomic batches, company isolation, disable/write
coordination, activation quota races, published-plan immutability, term validity, and calendar-month
boundaries across month-end and DST.

Verified payment-provider fulfillment and webhook processing remain Phase 6 integration work.
Signed offline grants remain disabled until a signing service and its subject/audience/signature
verification contract are selected. Inventory policy/costing, country tax packs, and
returns/refunds remain explicit product decisions from the supplied test plan and are not guessed.

Phase 5 is split into bounded delivery and verification checkpoints in
[`docs/phase-5-roadmap.md`](docs/phase-5-roadmap.md).
