# P5.9 inventory costing and reconciliation

P5.9 is complete for the approved conservative scope-average workflow, with explicit
checkpoint adoption rather than fabricated historical receipt attribution. Its local
INV-009, reconciliation, rebuild, isolation, rollback and concurrency gates pass.
This is not a production release or completion of all Phase 5: manufacturing and
the cross-domain release gate remain P5.10–P5.12.

The table below is current. Subsequent slice notes retain their original incremental
verification records; references there to pending later slices describe that earlier
checkpoint, not today's status.

| Slice | Deliverable | Status |
|---|---|---|
| P5.9.1 | Pure deterministic scope-average issue valuation | Implemented; 17 unit tests pass |
| P5.9.2 | Explicit durable policy/version and costing snapshots | Implemented for new typed stock issues; verified API/DB/concurrency tests |
| P5.9.3 | Receipt-layer consumption and immutable cost allocations | Complete for explicitly checkpoint-adopted scopes, including the integrations below |
| P5.9.4 | Integrate costing across sales, stock, purchase and return commands | Implemented for bounded untracked paths: typed stock, immediate sales/replacements, separate return dispositions, inline losses and matching-cost physical supplier credits; tracked supplier returns and cost-variance policy remain unsupported |
| P5.9.5 | Inventory valuation-to-GL reconciliation and bounded reports | Implemented: read-only bounded valuation/account/journal comparison; legacy attribution gaps remain explicit; durable large exports remain future report-job work |
| P5.9.6 | Rebuild/reconciliation, concurrency and INV-009 release gates | Complete locally: current cost integrity report, verified atomic projection rebuild, migration and full PostgreSQL regression evidence |

## Initial calculator contract

`apps/inventory/costing.py` accepts locked on-hand, reserved quantity, scope value,
issue quantity and functional-currency precision. All financial facts use finite
Decimal values fitting the ledger's numeric(20,6) contract. It rejects unavailable
quantity, inconsistent positions and costs requiring an unapproved GL rounding policy.

Partial issues consume proportional scope value at six-place ledger precision.
A full drain consumes the exact residual value, avoiding stranded cost from
multiplication of a rounded unit price. Results retain issued quantity/value,
informational unit cost and the remaining quantity/value. A local Decimal context
makes results independent of a caller's ambient precision.

This first slice is intentionally pure: it does not change existing posted values,
write receipt-layer allocations, or claim FIFO support. The currently approved
policy is scope-average valuation. Selecting FIFO or changing GL rounding remains
an explicit policy decision before the corresponding posting path is enabled.

Tests cover conservation, repeated issues, exact full depletion, free stock,
reserved capacity, finite/range/precision validation and ambient Decimal contexts.
The approved checkpoint adoption and subsequent layer-consumption contract below
define how existing immutable history is handled without fabricated attribution.

## P5.9.2 durable policy and cost basis

Migration 0023 adds immutable `inventory_cost_policies` and
`inventory_cost_basis_snapshots`, composite company foreign keys, tenant RLS,
audit triggers and deferred snapshot-completeness checks for new outgoing stock
documents. Policy version 1 supports only the approved scope-average method,
six-place ledger precision and rejection of fractional-GL costs. A policy is created
once, lazily inside the first successful typed outgoing stock-post transaction;
concurrent initialization cannot produce duplicate policies.

Typed shipments, transfers and adjustment-out commands now call the pure calculator
and retain an immutable basis per negative movement. Each snapshot includes policy,
functional currency/precision, quantity/value before the issue, reservations after
consuming the document's own reservation, issued quantity/value and unit cost.
Repeated outgoing lines use a sequential virtual basis derived from the locked
pre-document scope; planned inbound transfer legs are not included in that basis.
This preserves P5.8's existing batch transfer valuation behavior.

Database guards reconcile snapshot quantities, values and unit costs against the
movement and deterministic formula. Missing snapshots prevent new outgoing documents
from committing. Policy/snapshot audit, movements, journals, source state, outbox
and command receipt roll back together on any failure.

- `GET inventory/cost-policy`: current company policy, or `configured=false` before
  the first typed outgoing posting. Reads do not silently create configuration.
- `GET inventory/movements/{id}/cost-basis`: retained snapshot and policy metadata.
- Stock-document detail includes a bounded-by-document `cost_basis` list.

Reads use company membership, `inventory.view`, inventory licensing/module gates and
tenant RLS. These are inspection APIs, not arbitrary cost-history write endpoints.
There is no historical backfill: legacy movements and other posting paths explicitly
report `available=false` until their own integration/adoption slice is delivered.
Unadopted immediate sales issues, supplier-credit effects and return stock dispositions
are not yet moved onto this snapshot path. P5.9.3 below adds receipt-layer allocation
for adopted typed stock issues; FIFO and INV-009's full allocation/GL gate remain
unavailable.

Verification covers replay, policy/snapshot immutability, arithmetic tampering,
scope lookup, legacy absence, tenant isolation, mid-post failure, missing-snapshot
commit rollback, and concurrent policy initialization with serialized cost bases.

Local verification on 2026-09-28: a fresh PostgreSQL test database passed the full
174-test backend suite. Ruff, mypy, OpenAPI, Django checks, migration-state drift
and patch checks pass. Migration 0023 is applied locally. This evidence completes
the bounded P5.9.2 slice, not full P5.9 or the cross-domain production release gate.

## P5.9.3 opening-cost checkpoint foundation

The approved adoption approach is an explicit opening checkpoint, not retrospective
receipt attribution. `POST inventory/cost-checkpoints` accepts one warehouse/item/lot
scope and a nonblank review reason, with `Idempotency-Key`. It requires
`inventory.reconcile`, inventory write capability and the existing company/license
protocol. It locks the existing position and verifies quantity and value against
the immutable movement ledger before retaining the balance and policy version.

Migration 0024 stores an immutable checkpoint plus the exact covered movement IDs.
Timestamp cutoffs are intentionally not used: a concurrent transaction can create
a movement before waiting for a position lock. Exact membership avoids classifying
that later-committed movement as historical stock already covered by the opening.
Membership is sealed in the creation transaction; a deferred constraint verifies
its scope, count, quantity and value. Both tables have tenant RLS. Checkpoint audit,
policy creation, outbox and command receipt commit or roll back together.

Only one checkpoint is permitted per stock scope, including a null lot. Drift fails
closed; a new key cannot replace a checkpoint. Scopes with more than 10,000 movements
are rejected for interactive adoption and require a future bounded maintenance job.
No checkpoint is automatically created by a migration or posting operation.

The command does not change stock quantities, journals or historical values, and
does not create synthetic receipt movements or historical cost allocations.
Checkpoint creation alone is an adoption foundation: it does not consume stock.
The subsequent P5.9.3 slice below adds layer allocation to typed outgoing posting.
Cross-domain integration and full INV-009/valuation-to-GL reconciliation remain pending.

Local verification on 2026-09-28: the fresh PostgreSQL backend suite passed
179 tests, including checkpoint replay/immutability, missing-membership rollback,
concurrent adoption, projection drift and runtime-role tenant isolation. Ruff,
mypy (176 source files), Django checks, migration-state drift, OpenAPI validation
and patch checks pass. Migration 0024 is applied to the local development database.

## P5.9.3 retained proportional layer consumption

Migration 0025 extends the existing `inventory_cost_allocations` ledger, rather than
creating a parallel issue-allocation ledger. Each new allocation has exactly one
origin: a real receipt movement or the explicit opening checkpoint. Opening stock
does not acquire a synthetic receipt movement. Immutable `inventory_cost_layers`
retain the original quantity/value and origin; residuals are derived from immutable
allocations, not updated balances.

For adopted scopes, typed shipment, transfer and adjustment-out posting locks the
position, retains the opening layer and uncovered positive movements, then locks
layers in ID order. Layer IDs are derived deterministically from company and typed
origin, so rolling back first retention cannot change attribution order on retry.
Receipt retention is lazy within outgoing posting, including
positive transfer or return movements not covered by the checkpoint. Historical
negative movements covered by the checkpoint are never allocated retrospectively.
Scope residual quantity/value must equal the locked costing basis before use.
Repeated outgoing lines use virtual sequential layer residuals, preserving the
existing pre-document transfer planning policy.

Scope-average allocation consumes each layer's exact proportional quantity. Value
is apportioned using cumulative six-place HALF_UP rounding in layer-ID order against
the already approved issue value; the last cumulative amount equals the entire
issue value. This attribution does not alter the movement, COGS or GL amount and is
not FIFO. Full depletion consumes every layer's exact residual. A proportional
quantity not representable at six places fails with `STOCK_LAYER_ROUNDING_REQUIRED`;
no stock rounding or alternative costing policy is inferred.

Database guards validate origin/scope, original layer facts, current residual
capacity, proportional quantity/value, unit cost, ordered cumulative basis and
complete movement allocation. Deferred checks require every new negative movement
in an adopted scope to have a cost-basis snapshot and complete allocations. Issue
allocation membership is sealed in its posting transaction. Missing or invalid
allocations roll back journal, movement, retained layers, source status, audit,
outbox and command receipt together. Reversal of migration 0025 is refused once
retained costing history exists.

Stock document detail includes `cost_allocations`. Existing company/module/license
gates remain in place, and layers/allocations are tenant isolated by RLS. Commands
reject scopes requiring over 1,000 retained layers or newly discovered receipts;
a future maintenance job is required beyond the interactive bound.

Adoption is opt-in. Unadopted scopes keep the earlier stock/cost-basis behavior.
P5.9.4a below enables immediate sales/replacement fulfillment in adopted scopes;
deferred fulfillment through a linked typed shipment remains supported.
Other unintegrated negative posting paths in adopted
scopes fail the database completeness guard rather than creating incomplete cost
history. P5.9.4 must integrate those paths before they are enabled for adopted stock.
Full valuation-to-GL reconciliation, full-domain INV-009 and release gates remain
pending.

Local verification for retained layer consumption on 2026-09-28: a fresh PostgreSQL
suite passed 198 tests; a final reused-database pass also passed all 198 after the
deterministic-origin ID assertions were added. Coverage includes nine pure costing
cases and ten layer API/database cases for weighted attribution, full depletion,
replay, immutability, missing/failed allocation rollback, concurrent consumption,
fractional-quantity rejection, tenant RLS, bypass rejection, balanced deferred
shipment journals, repeated lines and protected migration reversal. The existing
empty-history warehouse migration test now uses transactionally restored costing
fixtures, preserving the production reversal safeguard. Ruff, mypy (179 source
files), Django checks, migration-state drift, OpenAPI validation, source checksums
and patch checks pass. Migration 0025 is applied locally. This verifies the adopted
typed-stock slice, not full P5.9 or cross-domain release readiness.

## P5.9.4a immediate sales and replacements

Immediate fulfillment of untracked stock in a checkpoint-adopted scope now uses the
same deterministic scope-average calculator and retained layer allocation as typed
shipments. The temporary `STOCK_LAYER_SHIPMENT_REQUIRED` restriction is removed.
All affected positions are locked in item order first. Each line reads the current
quantity/value after earlier line movements, consumes proportional layer residuals,
and retains a cost-basis snapshot and allocations with its sales movement. Full
depletion therefore takes the exact residual rather than multiplying a rounded
unit price. COGS and inventory journal lines use the same calculated movement value.

Replacement commands call this existing sales service inside their outer transaction;
replacement settlement, invoice posting, stock, costing, GL, audit/outbox and receipts
remain atomic. Unadopted scopes retain their earlier behavior, and lot/serial-tracked
immediate fulfillment still requires typed shipments.

Migration 0026 corrects allocation sealing for nested posting. PostgreSQL row `xmin`
can belong to a savepoint/subtransaction, so comparing it with the owning transaction
ID rejected valid replacements. The immutable cost-basis snapshot now retains an
`allocation_seal_xid` containing the owning transaction ID. Existing snapshots receive
an upgrade-time technical seal, not a fabricated original posting transaction;
their financial facts are unchanged. Later transactions still cannot append allocations.
This internal metadata is omitted from public cost-basis responses.
An insertion guard requires the actual owning transaction ID; callers cannot supply
an arbitrary past/future seal to leave a snapshot open for later allocations.

This slice does not change historical return costs or enable stocked supplier-credit
posting. Purchase and return receipts can already be discovered as real uncovered
positive layers by adopted outgoing costing. Their complete command integration,
supplier-cost adjustments and linked historical-cost disposal allocations remain
P5.9.4 work. Historical disposal costs must not be silently replaced with scope-average
costs when a warehouse contains returns with different original costs.

Local verification on 2026-09-28: a fresh PostgreSQL backend suite passed 205 tests.
Coverage adds missing/raised allocation rollback for immediate sales, concurrent
sale-versus-transfer costing, immediate-sale replay, three adopted replacement
price-difference scenarios and rejection of a forged transaction seal. Existing
deferred shipment, RLS, immutability and migration reverse/forward tests also pass.
Ruff, mypy (180 source files), Django checks, migration-state drift, OpenAPI validation,
source checksums and patch checks pass. Migration 0026 is applied locally. This
completes the bounded P5.9.4a sales/replacement slice, not all of P5.9.4 or P5.9.

## P5.9.4b linked return-stock dispositions

Separately posted transfers and explicitly approved write-offs of segregated returned
stock now retain original return-line cost in checkpoint-adopted scopes. Warehouse
average cost does not replace that historical cost. Position locks precede layer locks;
return/source aggregate locks serialize competing dispositions. Stock, approved loss
GL, immutable historical cost basis, allocations, audit/outbox and replay receipt are
one transaction. Unadopted scopes preserve their earlier behavior.

Migration 0027 adds immutable, company-scoped `inventory_return_cost_basis` and links
historical allocations to it. Database guards validate the action, movement, current
position, return-line balance, exact allocation quantities/values and transaction seal.
Deferred completeness prevents missing snapshots or allocations from committing.
Cost-basis reads expose `linked_return_history` without internal transaction metadata.

Opening-checkpoint claims use only retained, signed line-linked checkpoint members;
they are current ownership claims, not fabricated historical receipt allocations.
Later linked return receipts retain their actual movement origin. Anonymous legacy
losses or average consumption of the opening pool make ownership ambiguous and require
reviewed provenance; posting rejects those cases rather than guessing. Whole-layer
residuals must still reconcile with the physical projection.

This slice does not integrate inline write-offs during return posting or stocked
supplier-credit adjustments. Those paths and full valuation-to-GL reconciliation
remain pending; all of P5.9.4 and P5.9 are not complete.

Local verification on 2026-09-28: the fresh PostgreSQL suite passed 214 tests.
Coverage includes adopted segregated transfer/write-off replay, mixed warehouse costs
without historical repricing, allocation-failure rollback, immutable history,
concurrent double-disposal rejection and ambiguous checkpoint provenance rejection.
Ruff, mypy (182 source files), Django checks, migration-state drift, OpenAPI validation,
source checksums and patch checks pass. Migration 0027 is applied locally.

## P5.9.4c inline return write-offs

Inspection-approved inline write-offs now receive the returned stock at its original
sale cost and consume that linked receipt in the same posting transaction. All return
positions are locked deterministically before movements or layers; posting shares the
inventory maintenance lock. Existing warehouse stock never reprices the returned item.
Adopted scopes retain a historical basis with a null stock-action reference and
allocations against the actual new return receipt. Separate action postings retain
their existing non-null action reference and checks.

Migration 0028 extends the immutable historical basis guard and routes negative
`sales_return` movements through historical allocation completeness rather than the
average-cost guard. Deferred checks validate final line cost/quantity, matching
receipt, loss disposition, warehouse and journal. Missing history or allocations
roll back source state, receipt/loss stock, GL, audit/outbox and command receipt.
Inspection still requires explicit loss approval and permission; no automatic
damaged-stock write-off is introduced. Reversal refuses retained inline history.

## P5.9.4d physical supplier credits

The approved supplier-credit policy is physical stock return, not a financial-only
price correction. A linked stocked credit requires `warehouse_id` and inventory
posting authorization. Current credit drafts reverse entire selected source lines;
partial supplier returns are not silently added. The original receipt must be a
single untracked purchase-bill movement in that warehouse, matching item, quantity
and functional inventory value (net plus nonrecoverable tax).

`apps/inventory/supplier_returns.py` locks all affected positions before layers,
rejects unavailable/reserved stock, and calculates each outgoing value with the
approved scope-average calculator. It retains policy and cost-basis snapshots for
every supplier return, plus immutable layer allocations in adopted scopes. The
existing linked credit reverses payable, input tax and the original inventory account
snapshots. All stock, GL, credit state, audit/outbox and replay effects are atomic.

Current issue cost must equal the credited inventory value. A mismatch returns
`SUPPLIER_RETURN_VARIANCE_POLICY_REQUIRED`, rather than inventing an expense account,
changing the credit/tax facts, or leaving inventory and GL different. Lot/serial
selection, returns from a different warehouse, partial-line returns, and financial-only
stocked credits remain unsupported. Reclassified received stock is rejected for review.

Migration 0029 enforces posted physical credit provenance, one matching movement per
stock line, retained cost basis and inventory-journal/value agreement. Appending extra
or unrelated movements cannot complete. Migration reversal refuses retained physical
return history. Existing source-line credit uniqueness prevents double correction;
costing/allocation history and posted source/journal/movement immutability are unchanged.

These bounded integrations do not complete P5.9: valuation-to-GL reports, full-domain
reconciliation/rebuild evidence and release gates remain in P5.9.5/P5.9.6.

Local verification on 2026-09-28: a fresh PostgreSQL suite passed 223 tests.
New coverage includes inline historical receipt allocation in a mixed-cost warehouse,
rollback/replay, adopted and unadopted physical supplier returns, insufficient stock,
cost-variance rejection, missing/failed allocations and missing physical effects,
concurrent credit posting, and protected migration reversal. The empty-history
migration test clears stock/cost fixtures only inside its rollback-only transaction,
restoring all fixtures afterwards without weakening production reversal guards.
Ruff, mypy (185 source files), Django checks, migration-state drift, OpenAPI validation,
source checksums and patch checks pass. Migrations 0028 and 0029 are applied locally.

## P5.9.5 bounded inventory valuation and GL reconciliation

`GET reports/inventory-valuation-reconciliation?as_of=YYYY-MM-DD&limit=200`
requires company membership, inventory read access, `inventory.reconcile` and
`accounting.ledger.view`. Inventory read-only mode permits the report; disabled
inventory and unauthorized companies do not. The report uses the primary/default
database and one SQL statement snapshot, without creating layers, checkpoints,
accounting entries, audit or outbox rows.

Valuation comes from immutable stock movements, not the rebuildable position cache.
Accounts come from retained typed-document, invoice, bill or original return-line
snapshots. Current item profiles discover additional inventory control accounts for
GL comparison but never reattribute historical movement values. Transfers retain both
warehouse legs; their account/journal net effect remains zero. Posted GL lines only
are included; drafts and other tenants/companies are excluded.

`as_of` is mandatory and cannot be in the future. Stock uses `occurred_at` before the
next UTC midnight; GL uses posted `entry_date` through that date. Backdated journals
whose stock has a different effective date are reported as differences, not silently
retimed. Current account names/codes are display metadata, not historical snapshots.
Historical reservations and current projection verification are not claimed;
`projection_checked=false`, with the separate inventory reconciliation/rebuild
commands retained for P5.9.6 verification.

The response contains warehouse/item/lot/account valuation, per-account stock versus
GL totals, and movement/journal/costing diagnostics. All money and quantities are
decimal strings. `financial_matches` requires attributable history with no account
or linked-journal differences and no negative attributed scope. Journal-level checks
prevent offsetting manual entries from hiding behind equal company/account totals.
`costing_coverage_complete` checks outgoing basis presence and reconciliation, plus
allocation quantities/values where retained allocations or post-checkpoint adoption
require them. Pre-checkpoint issues are not assigned invented receipt allocations.
`matches` requires both financial agreement and complete outgoing costing coverage.
Legacy unlinked movements are explicitly `UNATTRIBUTED_MOVEMENT`, even when raw stock
and GL grand totals happen to agree. Missing old cost history stays visible.

Each input ledger/configuration set is bounded to 10,000 rows (all company movement
history for account discovery, inventory profiles, relevant posted GL lines, and
allocations). Larger input returns `INVENTORY_REPORT_JOB_REQUIRED`; it never returns
partial totals marked reconciled. Durable report-job/export execution is not added
by this slice. `limit` is 1–200 for each detail section. Counts and summary checks cover
all bounded input; `truncated=true` explicitly marks clipped detail sections.

No migration is required: this slice reads existing immutable records and projections
without changing the accounting schema. P5.9.6 and full release evidence remain pending.

Local verification on 2026-09-28: a fresh PostgreSQL suite passed 237 tests,
including 14 valuation-report cases covering supplier return agreement, neutral
transfers, read-only behavior, historical cutoff/date differences, retained account
attribution, legacy gaps, draft exclusion, offsetting journal errors, detail/input
bounds, tenant RLS, company/module gates and concurrent posting visibility.
Ruff, mypy (187 source files), Django checks, migration-state drift, OpenAPI validation,
source checksums and patch checks pass. No migration or historical backfill was needed.

## P5.9.6 current cost integrity and verified rebuild

`GET inventory/cost-reconciliation?limit=200` requires company membership,
`inventory.reconcile` and licensed inventory read access. Read-only inventory permits
inspection; disabled inventory does not. This is a current-state report, not an
as-of report or a GL report. It uses one primary-database SQL statement snapshot and
never retains layers, creates checkpoints or changes accounting/stock history.

The report compares exact checkpoint membership, retained layer origins and residual
capacity, allocation origins, complete post-checkpoint outgoing basis/allocation
totals, and adopted-scope layer quantity/value against immutable movement totals.
It also compares every current position's quantity, value and reservations against
movement and active-reservation ledgers. Invalid negative/stranded-cost balances and
over-reservation are diagnostics. Ordinary lazy opening/receipt retention is included
as pending real origins, not reported as missing allocations or inserted on read.

- `cost_history_matches` means the inspected current/adopted history has no integrity
  diagnostics. It does not claim pre-checkpoint outgoing costing coverage.
- `projection_matches` means current positions match movement/reservation ledgers.
- `adoption_complete` excludes stock-history scopes without an explicit checkpoint.
- `matches` requires all three. Legacy adoption and historical GL attribution are
  separate: an adopted current checkpoint can reconcile even when the valuation-to-GL
  report still correctly flags missing historical provenance or cost basis.

Each input set is capped at 10,000 rows: movements, checkpoints, checkpoint members,
layers, allocations, positions, active reservations, average bases and historical
return bases. Excess input returns `INVENTORY_REPORT_JOB_REQUIRED`, never partially
reconciled totals. Detail limits are 1–200; counts/checks cover all bounded input and
`truncated` reports clipped detail. Quantity and value JSON fields are decimal strings.

`POST inventory/rebuild` now verifies cost history and the repaired projection before
completing its receipt/outbox. The existing exclusive company maintenance lock,
conservative NOWAIT table locks and deterministic position locks remain held through
verification and commit. Any integrity/verification failure or input-bound rejection
rolls back positions, rebuild audit, outbox and receipt. Busy posting returns a
conflict for retry; posting started during rebuild waits and then costs the restored
position. Successful replay returns the retained verification result.

Rebuild changes only the position projection, never movements, policies, checkpoints,
layers, allocations, cost bases or journals. Missing immutable allocations are diagnosed
and require reviewed recovery; they are not recreated. The response exposes verified
projection/history flags and adopted/unadopted scope counts; unadopted legacy history
is not silently promoted to fully costed history.

Migration 0030 extends SQL-owned rebuild scope discovery to checkpoints/layers so a
zero-balance adopted scope with a missing position is restored too. It preserves the
existing authorization, SECURITY DEFINER search path, grants, audit and lock contract.
Both migration directions change only the function, not financial facts.

## Completed scope and deferred extensions

The approved method is scope-average, not FIFO. Adoption remains explicit, historical
values remain immutable, negative stock/backorders remain rejected, losses require
approval, and unrepresentable layer quantities or fractional-GL values fail closed.
Historical linked return losses retain their original cost rather than warehouse average.

Physical stocked supplier credits remain bounded to whole selected source lines,
one original untracked receipt in the original warehouse, and matching current issue
cost/credited inventory value. Partial/tracked or different-warehouse supplier returns,
financial-only stocked credits and cost-variance accounting are not implemented.
These extensions require additional workflow/accounting policy; completion does not
silently enable them. Large durable report/adoption/rebuild jobs remain future Phase
6/7 infrastructure. Manufacturing costing integration belongs to P5.11.

## Final local verification — 2026-09-28

The full backend suite passed **255 tests on a freshly created PostgreSQL test
database** (`pytest --create-db -q`), including 17 new cost-reconciliation/rebuild
cases and a new rebuild-function reverse/forward security-contract test. Existing
return/replacement tests now also verify cost reconciliation after rebuilding.

| Gate | Evidence |
|---|---|
| INV-009 deterministic costing | Pure average/layer tests; API mixed-cost allocations, replay, balanced GL and exact full depletion |
| INV-007 projection rebuild | Drift repair, missing/empty-checkpoint positions, active reservations; unchanged immutable costing/stock/GL |
| Atomicity | Allocation/basis/posting failures plus rebuild-history/projection/verification failures roll back all effects |
| Concurrency | Competing issues/disposals/credits, snapshot visibility, posting-versus-rebuild in both lock directions |
| Supported integrations | Typed stock/deferred shipments, immediate sales/replacements, separate historical return dispositions, inline losses, physical supplier credits |
| Authorization and isolation | Reconcile permission, membership, inventory module modes, runtime-role tenant RLS |
| Migration contract | Fresh install, existing empty-history reverse/forward suite, 0030 reverse/forward preserving function security/grants |
| Static/API/source checks | Ruff lint, mypy (189 source files), Django checks, no migration-state drift, OpenAPI validation, source checksums and patch checks |

All P5.9 files pass formatting checks. Repository-wide formatting still reports seven
unchanged pre-existing files outside this slice; they were not reformatted. Migration
0030 is applied to the local development database, with no pending migrations.
No staging deployment, restore/PITR drill, production performance certification or
country-fixture approval is claimed by this local phase completion.
