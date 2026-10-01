# Phase 8 release-gate evidence

Phase 8 is a release qualification, not a new accounting workflow. Run
`DATABASE_URL=... PHASE8_DISPOSABLE_HOST=1 bash scripts/phase8_local_gate.sh`
against a verified disposable
PostgreSQL 18 test host for lint, types, Django/migration checks, OpenAPI
validation, P0 tests, and the full PostgreSQL suite. The test suite creates
and destroys its own test database; do not point the command at production.

## Local evidence, 2026-10-01

- PostgreSQL server 18.4; local `pg_dump`/`pg_restore` client 18.3.
- A custom-format logical backup of the local development database restored
  without error into a separately named disposable database. Source and restore
  both had 107 ERP tables, 12 ERP views, 140 ERP routines, 94 ERP policies,
  71 applied Django migrations, and the same current company/journal row counts
  (both zero). `manage.py check` passed on the restored copy.
- On that copy, migration 0044 was unapplied and reapplied successfully. The
  disposable database and temporary backup were removed after verification.
- The integration release-contract test checks recent RLS policies, report and
  serial indexes, the runtime role's lack of `BYPASSRLS`, and primary routing.
- The unchanged local gate runner completed: Ruff, Mypy (235 source files),
  Django checks, model/migration checks, OpenAPI validation, 334 P0 tests, and
  the full 448-test PostgreSQL suite passed.

These checks establish a **local development gate**, not production release
approval. The source database has no business rows, so the restore did not
exercise financial-data reconciliation or production-sized recovery.

## Still required for production sign-off

| Gate | Required evidence | Current blocker |
| --- | --- | --- |
| PITR/failover | Base backup plus WAL restore to a selected timestamp; idempotent posting across failover | Local PostgreSQL has `archive_mode=off`; no configured WAL archive or failover target. |
| Supported upgrade | Restore a representative pre-release backup, migrate forward, and compare schema/data fingerprints | Only the latest 0044 backward/forward drill was possible with the available local backup. |
| Recovery targets | Timed restoration of production-shaped data against approved RPO/RTO | No approved RPO/RTO targets or representative backup. |
| Tax jurisdiction | Approved country-specific fixtures and legal/version review | No approved jurisdiction fixture pack in the repository. |
| Performance | Agreed p95/p99, throughput, concurrency, and soak budgets on production-shaped data | No approved load profile or staging environment. |
| Deployment/security | Managed-cloud/VPS smoke, private network/TLS, secrets, worker scheduler, alerts, and rollback drill | Deployment was explicitly deferred; no staging target or credentials were supplied. |
| Dependencies | Vulnerability audit of the locked dependency set | No audit tool or approved advisory feed is configured locally. |

Do not mark Phase 8 or production release complete until those gates have
measured evidence and a human release approval. Preserve a regression test for
each future production incident or corrected financial contract.
