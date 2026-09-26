# Accounting module boundary

This app owns accounts, journals, fiscal periods, document sequences, dimensions,
posting rules, journal entries, journal lines, and reversals. The underlying objects are
already installed from the authoritative SQL contract.

- `api/`: input/output serializers, thin views, and versioned company-scoped routes.
- `services/`: transactional commands, deterministic locks, posting, and reversal.
- `selectors/`: bounded company-scoped reads and accounting reports.
- `models/`: schema-qualified ORM mappings only where the ORM preserves the contract.
- `tests/`: accounting-specific unit and integration coverage.

No unrestricted CRUD endpoint may create or update journal lines. Posting services must
claim an idempotency receipt, acquire locks in the documented global order, validate the
fiscal period, commit immutable effects plus audit/outbox rows atomically, and replay the
same receipt after an unknown commit.

## Business-specific charts of accounts

The release-managed template catalog has three versioned profiles:

- `retail-wholesale-v1` for both retail and wholesale companies;
- `ecommerce-v1` for webstores and marketplace sellers;
- `manufacturing-v1` for raw materials, WIP, finished goods, applied costs, and variances.

`GET /accounting/chart-templates` lists the catalog and the company's current selection.
`GET /accounting/chart-templates/{code}` returns the template account hierarchy. An
accounting administrator applies a compatible template with
`POST /accounting/chart-templates/{code}/apply` and a `business_type` body value.

Application is additive and idempotent. It records template provenance on created
accounts, preserves later company-specific edits, and refuses to reinterpret a colliding
account code or replace an already-applied profile automatically. Retail and wholesale can
switch labels because they intentionally use the same chart. Template definitions are
release-managed rather than tenant-editable; company accounts remain normal CRUD resources.

Deleting an unused account is supported. Accounts referenced by children, journal lines, or
business configuration must be deactivated instead so historical meaning is preserved.
