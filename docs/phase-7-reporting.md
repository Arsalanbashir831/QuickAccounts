# Phase 7 reporting and observability (deployment deferred)

Financial selectors read PostgreSQL's primary `default` connection. The ORM router
pins financial apps to that alias if a replica is configured later. Trial balance,
general ledger, AR/AP aging, inventory valuation, and tax-component reads are
bounded. Trial balance and tax components use UUID cursors; general ledger uses
`cursor_date`, `cursor_entry`, and `cursor_line`, and carries the opening balance
across pages. Large trial-balance, general-ledger, tax-component, AR/AP aging,
and inventory-valuation CSV exports
use `financial_report_export` jobs on the existing `reports` queue. Exports cap
at 10,000 rows and 2 MiB and escape spreadsheet formulas. The worker rechecks
the requester's permissions before producing an immutable artifact.
For aging and inventory valuation exports, `date_to` is the as-of date;
`date_from` is accepted for the shared report-job contract but does not exclude
earlier source activity. Inventory valuation exports aggregate stock movements;
the separate interactive reconciliation still compares stock with the GL.

Every API response emits a `Server-Timing` application duration, and request
latency is logged under `quickaccounts.request_metrics`. The trusted command
`manage.py report_operational_metrics --tenant-id UUID --actor-user-id UUID`
returns a JSON snapshot of queue age, outbox age, failed jobs, database lock
waiters, connection usage, and replica lag (null on primary). A scheduler and
metrics collector may consume these signals when deployment is addressed.

No managed-cloud/VPS setup, alerts, external metrics service, or production
performance qualification is included here. Deployment and production release
gates remain explicitly deferred.
