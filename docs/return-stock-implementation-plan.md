# Returns, replacements, refunds, and stock disposition

## Implementation slices

| Slice | Scope | Status |
|---|---|---|
| R1 | Pure quantity/value/cost eligibility and replacement calculations | Implemented; unit tests verified |
| R2 | Warehouse CRUD and sellable/quarantine/damaged/supplier-return segregation | Implemented; PostgreSQL/API tests verified |
| R3 | Linked return draft aggregates, inspection, partial credit/tax snapshots | Implemented; API tests verified |
| R4 | Atomic historical-cost receipt, segregated transfers and approved write-offs | Implemented; API tests verified |
| R5 | Customer refund documents and credit applications | Implemented; refund concurrency verified |
| R6 | Linked replacement sale, issue, credit and difference settlement | Implemented; three price-difference paths verified |
| R8 | Current dead/slow-moving report; explicit returned-stock write-offs | Implemented; ordinary-stock impairment remains outside this flow |
| R9 | Regression, rollback, immutability and migration gates | Implemented; PostgreSQL regression gates verified |
| R10 | Returned-stock repair jobs and QC-gated release | Implemented; 411 fresh PostgreSQL tests pass; migration 0034 applied locally |
| R11 | Credit applications to future customer invoices | Implemented; replay and concurrent consumption verified |
| R12 | Consolidated invoice settlement/fulfillment/return summary | Implemented; stock issue is explicitly not delivery confirmation |
| R13 | Independent linked/unmatched custody intake and matching | Implemented; focused PostgreSQL tests verified |
| R14 | Dead-stock disposal cases, warehouse roles and clearance orchestration | Implemented; focused PostgreSQL tests verified |
| R15 | Supplier claims and broader physical supplier-return provenance | Implemented within conservative full/partial, one-hop scope; focused PostgreSQL tests verified |
| R16 | Sales-order lifecycle and explicit delivery confirmation | Implemented; focused PostgreSQL tests verified |

R1 has no database writes or public posting endpoints. Posting services must supply
locked source facts and separately validate original tax components, prior price
credits, UOM, fulfillment, currency and exchange-rate eligibility.

## Invariants

- Original posted invoices/payments remain immutable; corrections are linked documents.
- Physical receipt, inspection/disposition, commercial credit and cash settlement are distinct events.
- Historical stock issue cost, not current valuation, determines return cost.
- Unpaid sales may receive credits but do not create cash refund capacity.
- Quarantined/damaged/supplier-return stock must not be available for ordinary sale.
- Dead stock is an aging/turnover classification, not automatic destruction or expense.
- Quantity and amount eligibility must include all prior committed adjustments and serialize concurrent commands.
- Atomic commands include GL, movements, projections, audit, outbox and idempotency receipt.
- External payment/provider calls execute after commit through durable intent.

## Approved policies and supported boundaries

Bill back means customer cash back/refund (confirmed by the user). Supplier
claims are a separate, explicitly approved physical-return workflow. Refunds require eligible received cash; credits use
original sale/tax values. Damaged stock retains historical cost until an explicitly
approved write-off. No-refund/warranty and excess goodwill remain unsupported.
Dead-stock reports require a caller-selected inactivity threshold and never write
off inventory automatically.

Posting currently supports functional-currency sales at exchange rate 1 and
untracked stock. Refund receipts with withholding are blocked. Historical cost
portions must fit functional-currency precision; otherwise posting fails closed
until a rounding policy is approved. Ordinary-stock impairment, repair-cost capitalization, tracked
lots/serials and foreign-currency customer return settlement are not included.

## R2 API and persistence contract

- `GET/POST inventory/warehouses`: bounded, keyset-paginated company warehouse catalog.
- `GET/PATCH inventory/warehouses/{id}`: revisioned metadata; `If-Match` required on updates.
- `GET inventory/warehouses/{id}/balances`: physical quantity, reservations, sale availability and company-currency value, including lot scopes.
- Categories: `sellable`, `quarantine`, `damaged`, `supplier_return`. Existing warehouses default to `sellable`; operators must explicitly classify existing non-sellable locations.
- Reads require `inventory.view`; changes require `inventory.manage` and the enabled/licensed inventory module.
- Deactivation replaces deletion. Stock/reservations/value must be cleared before deactivation.
- A warehouse with stock history cannot be relabeled; use a future typed transfer/disposition command instead.
- Database guards block sales issues/shipments and active reservations in non-sellable locations. Other typed disposition movements remain separate workflows, not generic sales.
- Warehouse share locks serialize movement/reservation checks against warehouse edits. Availability projections exclude non-sellable/inactive locations without hiding physical stock.
- Warehouse changes append database audit events. Warehouse CRUD does not create inventory or accounting effects.

Verification includes CRUD, duplicate codes, revision races, company scope, denied
permissions, disabled/read-only modules, non-sellable balances, direct database
sale/reservation rejection, rollback and migration reverse/forward checks.
## Return command contract

- `GET/POST sales/invoices/{id}/returns`: linked drafts; original posted invoice is unchanged.
- `GET sales/invoices/{id}/return-eligibility`: remaining quantities, credits and cash capacity.
- `GET/PATCH sales/returns/{id}`: draft editing with revision checks.
- `POST sales/returns/{id}/inspect`, `/post`, `/void`: explicit inspection and atomic posting.
- `POST sales/returns/{id}/refund`: records a confirmed cash refund, not a gateway request.
- `POST sales/returns/{id}/replace`: posts an existing calculated replacement draft,
  links it and applies available return credit. Collect positive differences through
  normal receipts; refund eligible negative differences through the refund command.
- `POST sales/returns/{id}/apply-credit`: applies remaining credit to the original sale
  or an eligible future customer invoice.
- `POST sales/returns/{id}/stock-dispositions`: transfers segregated returned stock
  or writes it off with explicit approval and `inventory.write_off` permission.
- `GET reports/dead-stock?inactive_days=N`: current positive stock by location with
  sales-inactivity classification, not FIFO age, impairment or automatic disposal.

Commands require `If-Match` and `Idempotency-Key`. Posted documents, refund records,
credit applications and stock actions are immutable. Source locks serialize
quantity/value consumption. Atomic transactions include ledger, stock, audit,
outbox and command receipts; failed commands roll back all effects.

Return accounting reverses original revenue/output tax against AR. Physical
receipts debit inventory and credit original COGS at historical cost. Cash refunds
debit AR and credit cash. Approved losses debit the selected expense account and
credit inventory. Same-account location transfers have no GL effect.

Verification includes partial refund/replay/over-refund, concurrent refund races,
unpaid-sale credit and receipt over-allocation, all inspection dispositions,
historical cost, later segregated release/write-off, repeat-consumption rejection,
replacement differences and subsequent collection/refund, direct posted-document
immutability, journal failure rollback, quantity caps, and dead-stock filtering.

The complete backend suite covers migration reverse/forward restoration and
existing company/RLS/module/permission gates alongside these flows. Lint, type
checks, OpenAPI validation, Django checks and migration-state drift checks pass.
Migrations 0017–0019 have been applied to the local development database; deployment
to other environments still follows their normal migration and runtime-grant process.
Detail responses include at most 200 recent settlement/disposition history records;
remaining-credit totals are independently aggregated over the complete history.

## Expanded diagram alignment: approved policies

The expanded retail/wholesale diagram now has explicit commands for R13–R16. The user
approved custody-only handling for unmatched invoice-less returns until verified
sale matching, repair estimates without automatic capitalization, and no supplier
claim receivable until explicit approval. Supplier cost variances remain blocked.
Supplier claims settle only against posted physical supplier credits; no claim
receivable is created. Full/partial untracked returns and one provable one-hop
transfer are supported; mixed provenance and cost variances remain blocked.
See [the Phase 5 completion contract](phase-5-completion.md) for commands and
supported boundaries.

R10 APIs: `GET/POST inventory/repairs` and `GET/POST inventory/repairs/{id}`.
Creation requires `If-Match` of the posted return; transitions require the repair
revision. All commands require `Idempotency-Key`. Jobs track diagnosis,
company-currency estimated cost, waiting-parts/on-hold/in-progress/repaired states,
QC notes/actor/time and not-repairable decisions. No repair estimate changes GL
or inventory cost. QC requires `inventory.quality.approve`; repair management
requires `inventory.repair.manage`. Final passed-QC/not-repairable decisions and
source identities cannot be changed or deleted.

Transfers from segregated returned stock to sellable stock now require
`repair_job_id` referring to matching passed QC. Damaged items must first complete
repair; originally resellable items can receive a direct QC decision. Released
quantity cannot exceed approved quantity or retained returned stock. Ordinary
inventory transfers from non-sellable to sellable locations are blocked rather
than allowed to bypass this protocol. Existing historical transfers are preserved;
new releases must comply. Repair jobs currently require a posted linked return,
not unmatched custody stock or general inbound-stock QC.

R11 extends `sales/returns/{id}/apply-credit` with optional `target_invoice_id`
and `effective_date`. Omitting both preserves the original behavior and receipt
payload hash. A future target must be a posted original invoice for the same
identified customer, currency, rate and receivable account. Application is capped
at remaining credit and invoice balance, with source/target invoice locks ordered
by ID. The existing database settlement guard enforces chronology and capacity;
an additional guard protects customer/currency/control-account consistency.
Applications move existing AR credit between documents, not cash or GL balances.

R12 exposes `open_amount`, `settlement_status`, `fulfillment_status` and
`return_summary` on posted original invoice detail. Settlement includes valid
credit applications, so it is not mislabeled as cash payment. Stock fulfillment
is derived from issued quantities; `delivery_confirmed` changes only when the
separate delivery-confirmation command records evidence.

Verification for R10–R12: all **411 tests passed on fresh PostgreSQL in 215.99s**,
including real runtime-role repair/QC, forbidden QC bypass through return
disposition, repair transition/revision guards, immutable final QC decisions,
quantity caps, replay, concurrent future-credit consumption, and migration
reverse/forward restoration. Ruff lint/changed-file formatting, mypy (205 source
files), Django checks, warning-free OpenAPI validation, migration drift/pending
checks, source checksums and diff whitespace checks pass. Migration 0034 and
refreshed runtime grants were applied locally at that checkpoint. R13–R16 were
subsequently implemented; staging/production qualification remains separate.
