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

Bill back means customer cash back/refund (confirmed by the user); supplier
claims are outside this scope. Refunds require eligible received cash; credits use
original sale/tax values. Damaged stock retains historical cost until an explicitly
approved write-off. No-refund/warranty and excess goodwill remain unsupported.
Dead-stock reports require a caller-selected inactivity threshold and never write
off inventory automatically.

Posting currently supports functional-currency sales at exchange rate 1 and
untracked stock. Refund receipts with withholding are blocked. Historical cost
portions must fit functional-currency precision; otherwise posting fails closed
until a rounding policy is approved. Ordinary-stock impairment, repairs, tracked
lots/serials, foreign-currency return settlement and supplier claims are not included.

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
- `POST sales/returns/{id}/apply-credit`: applies remaining credit to the original sale.
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
