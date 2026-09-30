# Phase 6 internal jobs (no external API providers)

This provider-free slice runs entirely on PostgreSQL. It does not authenticate
webhooks, call payment providers, send emails, or claim external delivery.

## Supported commands

- `POST /api/v1/tenants/{tenant}/companies/{company}/jobs` with `job_type`
  `partner_import` and a unique `job_key` accepts 1–100 partner objects. It
  creates new partners only; existing codes and invalid rows receive row-level
  outcomes. It never updates existing partners.
- The same endpoint with `job_type=partner_export` queues a company-scoped
  partner CSV. The artifact endpoint becomes available after success. Exports
  are capped at 10,000 rows and 2 MiB, with spreadsheet formula cells escaped.
- `GET /jobs`, `GET /jobs/{id}`, `GET /jobs/status`, `POST /jobs/{id}/cancel`,
  and `GET /jobs/{id}/artifact` expose scoped status, cancellation of queued
  jobs, and completed artifacts. A repeated job key with the same requester
  and payload returns the original job; different payloads conflict.
- The internal outbox relay copies committed events into immutable delivery
  receipts and marks the outbox row delivered in the same transaction. This is
  **internal** delivery, not proof that an external subscriber received it.

## Worker operation

Run bounded sweeps from a trusted scheduler for each tenant and queue:

```sh
python manage.py run_internal_jobs --tenant-id TENANT_UUID --actor-user-id USER_UUID --queue imports
python manage.py run_internal_jobs --tenant-id TENANT_UUID --actor-user-id USER_UUID --queue reports --company-id COMPANY_UUID
```

The actor must be an active tenant member; the original job requester is
re-checked against current company/module/permission policy before execution.
The worker claims only supported job types with `FOR UPDATE SKIP LOCKED` and a
lease. Claim tokens fence completion, and a failed transaction leaves no import
rows or artifact. Retries use bounded delays and at most five attempts by
default; expired final claims become failed. Check `/jobs/status` and job
details for backlog and errors. A deployment scheduler must invoke both
queues and the company outbox sweeps; merely deploying the API does not process
queued jobs.

Do not use this relay as an external connector. Future provider integrations
need their own authenticated inbox, destination-specific delivery cursor,
secret handling, and provider-specific reconciliation. Bulk accounting-document
imports are not enabled: they require separate policy and posting validation.

## Local verification

The complete PostgreSQL regression suite passes (436 tests), including import
rollback/retry, job-key replay, cancellation, artifact download, outbox
deduplication, and unsupported-job isolation. Ruff, Mypy (226 source files),
OpenAPI validation, and Django checks pass. Migration 0041 is applied to the
local development database. Existing default grants cover its two tables;
the runtime role still cannot update company policy. A broad grant-script
rerun was rejected by the safety review and was not needed for these tables.
