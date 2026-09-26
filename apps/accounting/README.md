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

