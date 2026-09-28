# Phase 5 commercial-domain roadmap

Phase 5 is delivered as small, independently verifiable slices. Each slice must preserve
company scope, permissions, license/module policy, optimistic concurrency, auditability, and
the source-document boundary from the authoritative architecture.

| Task | Deliverable | Depends on | Verification checkpoint | Status |
|---|---|---|---|---|
| P5.1a | Partner revision, permission, and audit database contract | Phase 4 access foundation | Migration/schema contract tests | Complete |
| P5.1b | Company-scoped partner list/create/detail/update API | P5.1a | CRUD, pagination, permission, duplicate, and stale-revision API tests | Complete |
| P5.2 | Partner addresses and effective-dated tax registrations | P5.1 | Address ownership/default validation; registration jurisdiction/date tests | Complete |
| P5.3 | Shared item/service catalog and accounting profiles | P5.1 | Item lifecycle, service-without-inventory, mapping, and revision tests | Complete |
| P5.4 | Sales invoice draft aggregate and deterministic calculation | P5.2–P5.3 | SAL-001–SAL-003 and SAL-010 | Complete |
| P5.5 | Sales posting and linked credit corrections | P5.4 | SAL-004–SAL-009, idempotency, concurrency, and reconciliation | Complete |
| P5.6 | Purchase bill drafts, posting, and supplier credits | P5.2–P5.3 | PUR-001–PUR-004 and reconciliation | Complete |
| P5.7 | Payments, withholding, AR/AP allocation, and reversal | P5.5–P5.6 | PAY-001–PAY-006, over-allocation race, and historical aging | Complete |
| P5.8 | Warehouses, stock documents, reservations, and availability | P5.3 | INV-001–INV-008 and reservation concurrency | Complete under approved conservative rules; migrations 0020–0022 applied locally |
| P5.9 | Inventory costing, cost allocation, and rebuildable projections | P5.8 | INV-009 and valuation-to-GL reconciliation | Complete for approved scope-average/checkpoint-adopted workflows; current cost integrity and atomic verified rebuild implemented; 255 fresh PostgreSQL tests pass; migration 0030 applied locally; deferred extensions documented |
| P5.10 | Versioned BOMs and production-order drafts | P5.3, P5.8 | MFG-001, MFG-002, and MFG-007 | Not started |
| P5.11 | Material issues, outputs, completion, and cancellation | P5.9–P5.10 | MFG-003–MFG-006 and duplicate-command races | Not started |
| P5.12 | Cross-domain release gate | P5.1–P5.11 | Full API, schema, RLS, concurrency, OpenAPI, lint, type, and migration gates | Not started |

### P5.7 implementation slices

| Slice | Scope | Exit criteria | Status |
|---|---|---|---|
| P5.7.1 | Payment draft aggregate and CRUD | Company-scoped receipt/disbursement drafts with revision and idempotent creation | Complete |
| P5.7.2 | Withholding calculation and immutable snapshots | Payment withholding components reconcile exactly to `withholding_total` | Complete |
| P5.7.3 | Draft allocation proposals | Bounded AR/AP allocation replacement with currency and direction validation | Complete |
| P5.7.4 | Atomic payment posting | Cash, withholding, allocations, GL, audit, outbox, and idempotency commit together | Complete |
| P5.7.5 | Allocation race and reversal | Locked open-item settlement, compensating reversal, and no over-allocation | Complete |
| P5.7.6 | Settlement reporting | Current open items and cutoff aging reconcile before/after allocation and reversal | Complete |

P5.7 supports same-currency, matching-exchange-rate settlement; incompatible conversions
are rejected before posting. The payment contract and verification mapping are documented in
[`phase-5-p7-payment-contract.md`](phase-5-p7-payment-contract.md).

## Product-decision checkpoints

Implementation pauses at the relevant boundary rather than inventing any of these policies:

- supported-country tax packs and reviewed tax fixtures before tax-enabled posting;
- negative-stock/backorder policy before stock issue and reservation behavior;
- FIFO versus weighted-average policy before inventory costing;
- returns, refunds, scrap, and variance rules before those correction workflows are exposed.

Unrestricted journal-line, stock-movement, and allocation insertion endpoints are never part of
this roadmap. Those records are outputs of authorized source-document commands.

P5.9 completion and verification evidence are documented in
[`phase-5-p9-costing-contract.md`](phase-5-p9-costing-contract.md). FIFO, supplier-return
cost variances, partial/tracked supplier returns and large durable report/maintenance
jobs are not enabled by this completion. P5.10 is the next roadmap step; Phase 5's
overall release gate remains P5.12.
