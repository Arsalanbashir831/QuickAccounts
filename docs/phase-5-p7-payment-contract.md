# P5.7 payment contract

P5.7 begins with payment drafts. A payment is a source document; journal lines and
allocation rows are outputs of its posting command and are never directly writable by
the public API.

## Slice order

1. P5.7.1 adds company-scoped receipt/disbursement draft CRUD over the existing
   `erp.payments` contract. Draft edits require `If-Match`; posted and void records are
   immutable.
2. P5.7.2 adds payment-tax component snapshots. The component sum must equal
   `withholding_total`, and tax direction/account polarity remains explicit.
3. P5.7.3 adds bounded draft allocation replacement. Receipt drafts may target sales
   invoices; disbursement drafts may target purchase bills. Company, currency, direction,
   and remaining-balance checks happen before any replacement rows are written.
4. P5.7.4 posts the payment atomically with locked targets, withholding, allocations,
   cash/withholding GL, audit, outbox, and idempotency result.
5. P5.7.5 adds compensating payment reversal and concurrency tests proving that two
   allocators cannot consume the same remaining balance.
6. P5.7.6 verifies current open-item and historical cutoff aging from immutable posted
   allocations rather than today's balance.

## Invariants for P5.7.1

- Every query and write is company- and tenant-scoped.
- Payment amount is positive cash; withholding is separate and non-negative.
- Direction is exactly `receipt` or `disbursement`.
- Only drafts can be created or edited; posting requires the payment-post capability,
  an idempotency key, and the current revision.
- No draft endpoint inserts journal, allocation, or tax-component output rows directly.
- Payment creation and updates emit the standard audit/request-id metadata through the
  existing database trigger protocol.

## Implemented commands

All paths are under the existing tenant/company namespace.

| Path | Behavior |
|---|---|
| `GET/POST /payments` | Bounded keyset list and idempotent creation |
| `GET/PATCH /payments/{id}` | Company-scoped detail and revision-checked draft editing |
| `GET/PUT /payments/{id}/allocations` | Inspect or replace up to 50 distinct draft allocation proposals |
| `PUT /payments/{id}/withholding` | Calculate effective configured percentage/fixed withholding and snapshots |
| `POST /payments/{id}/post` | Atomically post cash, withholding, settlement GL, allocations, audit, outbox and receipt |
| `POST /payments/{id}/reverse` | Opposite-direction compensating payment, journal, tax snapshots and allocation reversals |
| `GET /reports/open-receivables` | Signed current or cutoff receivable items |
| `GET /reports/open-payables` | Signed current or cutoff payable items |
| `GET /reports/receivable-aging` | Receivable amounts with current/1–30/31–60/61–90/90+ buckets |
| `GET /reports/payable-aging` | Payable amounts with the same cutoff and bucket rules |

Creation, posting and reversal require `Idempotency-Key`. Draft edits, allocation replacement,
withholding calculation, posting and reversal require `If-Match`. Financial header changes
clear stale allocation proposals and withholding calculations.

Posting receives `fiscal_period_id` and `journal_id`; the cash account comes from the draft.
The settlement control account comes from the existing sales/purchase posting rule and must
match each allocated document's historical control account. Cash is positive; gross settlement
capacity equals cash plus withholding. Unallocated capacity remains a partner credit balance.
Receipt withholding debits its configured receivable/asset account; disbursement withholding
credits its configured payable/liability account. Effective company and verified partner tax
registrations are required before taxable posting.

Allocations must match company, partner, source direction, currency and exchange rate.
Cross-currency settlement and realized FX conversions are rejected in this slice; they require
a separate FX conversion/account-mapping contract. Posted credit notes remain signed open
credits; applying credits and issuing refunds are separate source-document workflows.

Posted payments, tax components, allocation facts and compensation rows are immutable.
Reversal leaves the original facts intact, copies and reverses the original journal and tax
polarity, and writes one compensation per original allocation. It preserves historical tax
versions even if they expire before the reversal date.

Historical reports require both the effective document/payment date and `posted_at` to be
on or before the requested `cutoff`; the posting-time cutoff is the end of that UTC date.
Later allocations and reversals cannot rewrite an earlier report. Current reporting defaults
to today's cutoff. Signed credit documents and unallocated payment balances are included.
Balances retain currency identity; reports never sum different currencies together.

## Verification

- PAY-001: receipt/disbursement cash and withholding journal reconciliation; immutable tax snapshots.
- PAY-002: allocation revisions, capacities, partial settlement and unallocated overpayment balances.
- PAY-003: two competing payments against one AR/AP document; exactly one settlement wins.
- PAY-004: partner, company, currency, conversion and direction rejection without partial replacement.
- PAY-005: idempotent compensating reversal; original header/allocations/tax components remain immutable.
- PAY-006: current open items and historical aging before allocation/reversal reconcile.
- Additional gates: competing posting keys, journal-failure rollback, permission/module denials,
  database-level over-allocation checks, non-owner RLS, schema contracts and migration round trips.

The fixture tax rates in the tests are synthetic. This implementation does not install statutory
country rates or enable tax filing; reviewed country packs remain the documented product checkpoint.

Final local verification (2026-09-27): the complete PostgreSQL suite passed **103 tests**.
Ruff, mypy (151 source files), Django system checks, migration drift, OpenAPI validation,
source checksums and diff validation passed. Migrations 0014 and 0015 were applied successfully
to the local development database. The settlement migration also passed a backward/forward
round trip in the test database. Runtime-role RLS checks use temporary read grants inside an
explicitly rolled-back transaction.
ok 
