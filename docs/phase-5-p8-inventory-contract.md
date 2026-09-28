# P5.8 inventory documents, reservations and availability

## Scope and approved policy

P5.8 supplies warehouse setup, typed stock drafts/posting, invoice fulfillment,
reservations, lot/serial lookup and creation, movement queries, current availability,
and projection reconciliation/rebuild. Stock movements are append-only outputs of
authorized commands, not independently writable API resources.

The user approved rejecting negative stock/backorders, using the existing
warehouse/item/lot scope-average value for issues and transfers, explicit approval
for losses, and immediate versus deferred invoice fulfillment. FIFO, receipt-layer
allocations and broader costing/revaluation remain P5.9 work.

## API

All routes below use the existing tenant/company API prefix.

| Route | Operation |
|---|---|
| `inventory/documents` | Bounded list; create draft |
| `inventory/documents/{id}` | Detail; revisioned draft patch |
| `inventory/documents/{id}/post` | Atomic stock/accounting posting |
| `inventory/documents/{id}/void` | Void draft; release active reservations |
| `inventory/reservations` | Bounded list; reserve a typed outgoing draft line |
| `inventory/reservations/{id}/release` | Revisioned idempotent release |
| `inventory/lots` | Bounded lookup; immutable lot/serial identity creation |
| `inventory/movements` | Bounded immutable movement history |
| `inventory/availability` | Current physical/reserved/sellable availability |
| `inventory/reconciliation` | Compare movement/reservation aggregates to positions |
| `inventory/rebuild` | Authorized idempotent reconstruction from authoritative history |

Writes require `Idempotency-Key`; draft patch, post, void and reservation release
require `If-Match`. Reservation creation supplies `document_revision`, `line_id`,
`quantity` and `reservation_key`. It cannot reserve more than the source line or
current physical availability, and non-sellable locations cannot be reserved.

Documents contain at most 100 lines. Receipt/adjustment-in lines have a destination;
shipment/adjustment-out lines have a source; transfer lines have distinct source
and destination locations. Lot/serial selection must match item tracking. Serial
lines represent one unit, with company/item serial uniqueness and a global
per-serial on-hand cap. Existing lots can be split across locations; creating a
duplicate lot identity is rejected.

Reads require `inventory.view`. Drafts/lots require `inventory.manage`, posting
requires `inventory.post`, reservation commands require `inventory.reserve`, and
reconciliation/rebuild require `inventory.reconcile`. Losses additionally require
`inventory.write_off`, `approve_loss=true`, an expense offset account and a reason.
All commands retain company membership, designated license, module-mode and RLS gates.

## Posting and accounting

Posting locks linked invoices, the stock aggregate, open fiscal period, affected
warehouse/item/lot positions in deterministic order and its active reservations.
Reservations are consumed in the same transaction as movement, journal, source
status, audit, outbox and command receipt. Any error rolls back every effect.

Receipt/adjustment-in uses explicitly supplied company-currency unit cost and debits
inventory against the selected offset account. These are standalone stock documents,
not a duplicate purchase-bill goods-receipt workflow. Adjustments out credit inventory
against an approved expense. Transfers preserve quantity/value and create no GL
entry, since inventory accounts belong to the item rather than the warehouse.
Cost/account snapshots reconcile against stock movements and journal amounts.
Costs that cannot fit functional-currency minor units fail closed pending a reviewed
rounding policy. Posted documents, lines, movements and final reservations are immutable.
Corrections use new adjustment/transfer documents; there is no destructive history API.

Invoice posting retains `stock_fulfillment=immediate` by default. Explicit
`stock_fulfillment=deferred` posts revenue/tax/AR without issuing stock or recognizing
COGS. A later shipment line references `sales_invoice_line_id` and posts COGS/inventory
at fulfillment. Immediate invoices cannot be shipped again. Multiple partial
shipments are permitted up to the original invoice-line quantity; invoice locks and
database caps prevent duplicate/concurrent over-fulfillment. Existing return commands
still fail closed for tracked inventory and incomplete original fulfillment.

## Queries and rebuilding

Lists use bounded ID cursors. `as_of` must be timezone-aware. Historical availability
responses intentionally report effective-dated on-hand/value only, marked
`historical_on_hand_only=true`; they do not invent historical reservation balances.
Current availability excludes inactive/non-sellable locations while retaining their
physical stock and value. Reconciliation returns at most 200 differences with a
truncation flag. Rebuild preserves position IDs, recomputes all company scopes from
immutable movements and active reservations, and requires nonblocking maintenance
locks. Busy inventory returns a conflict rather than forcing unsafe concurrent repair.

## Verification mapping

- INV-001: receipt quantity/value and GL, replay and rollback.
- INV-002: insufficient-stock rejection, approved losses and reserved availability.
- INV-003: competing reservations serialize with one loser when capacity is exhausted.
- INV-004: direct ledger edits rejected; posted aggregate immutability.
- INV-005: two-legged transfers preserve cost and consume reservations atomically.
- INV-006: required lots, duplicate identities and serial duplicate-unit rejection.
- INV-007: movement/reservation reconciliation and idempotent position rebuild.
- INV-008: deferred invoice shipment, retry and over-fulfillment rejection.

Additional tests cover stale draft revisions, void/release, module read-only mode,
denied company permissions, missing-source scope, current/historical query behavior,
and complete backend regression/migration reverse-forward gates.

## Local verification evidence

On 2026-09-28, the complete backend regression suite passed 169 tests. The final
P5.8/P5.9 focused suite passed 29 tests after explicit incoming-cost validation was
tightened. Ruff, mypy, OpenAPI validation, Django checks, migration-state drift and
patch whitespace checks pass. Inventory audit/rebuild tests use the restricted
runtime role without granting audit-table DML; test-only grants roll back.
Migrations 0020–0022 were applied successfully to the local development database.
This is local verification, not staging/performance/restore evidence or a P5.12 release.
