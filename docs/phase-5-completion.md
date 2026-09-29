# Phase 5 backend status and supported boundaries

Phase 5 covers the approved commercial-domain backend: parties and item profiles;
sales and purchase draft calculation, posting and linked corrections; payments,
withholding and open-item settlement; stocked inventory and cost history; BOMs
and production execution; and the retail/wholesale return, replacement, repair,
disposal, supplier-return, order and delivery extensions.

The final extension is implemented in migrations 0035–0040. Its commands are:

| Flow | API | Accounting and stock boundary |
|---|---|---|
| Invoice-less intake | `inventory/return-intakes` | Custody and inspection only until a verified original sale is matched. A matched return posts owned stock at the same physical warehouse. |
| Dead-stock disposition | `inventory/disposal-cases` | Explicit case approval; return-stock or ordinary-stock posting uses the existing atomic stock commands. Losses require approval; safe-sale clearance requires QC and a posted sale issue. |
| Supplier claims | `purchasing/supplier-claims` | Draft and approval have no speculative receivable or stock effect. Settlement requires a posted, matching physical supplier credit; linked disposal cases close in the same transaction. |
| Physical supplier returns | `purchasing/bills/{id}/credit-notes` | Full or cumulative partial quantities use original tax snapshots and source cost. One provable one-hop transfer to a supplier-return warehouse is supported; mixed or ambiguous provenance and cost variances fail closed. |
| Customer order | `sales/channels`, `sales/orders` | Versioned draft/confirm/cancel, then one linked invoice draft. No order-only GL or stock movement. |
| Delivery | `sales/invoices/{id}/delivery` | Immutable delivery evidence only after full stock issue; it marks a linked order fulfilled without duplicate journal or stock effects. |

All writes require company scope, module/permission checks and bounded command
transactions. New lifecycle records are audited; commands with financial or stock
effects use receipts, outbox events and source locks. Migration reversals refuse
to discard retained custody, disposal, claim, delivery or partial-credit history.

Conservative boundaries remain: tracked lot/serial supplier returns, foreign-
currency customer returns, supplier cost variances, automatic repair-cost
capitalization, warranty/no-refund returns, goodwill above original sale value,
and automatic dead-stock write-off are blocked until specific policies and
accounting tests are approved. Customer refunds require eligible cash received.
Supplier claims are not an independent supplier receivable; posted supplier
credits reduce AP. Orders require an identified customer (including a configured
walk-in customer account when applicable).

Local development verification: 431 PostgreSQL tests pass; Ruff lint, Mypy
(219 source files), Django system checks, OpenAPI validation, migration-state
checks, and the authoritative SQL source manifest pass. Migrations 0035–0040
are applied to the local development database, runtime grants have been
refreshed, and direct runtime mutation of company policy remains revoked.
This development checkpoint is distinct from staging and production
qualification: approved country tax fixtures, restore/PITR, measured
performance and RPO/RTO, and deployment smoke remain release work.
