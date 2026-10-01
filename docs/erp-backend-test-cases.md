# ERP Accounting Backend Test Cases

## 1. Purpose and scope

This document is the executable test specification for the Django/PostgreSQL backend described in:

- [Backend architecture and API design](sandbox:/workspace/scratch/082cfa469e03/erp-backend-architecture-and-apis.md)
- [Full PostgreSQL schema](sandbox:/workspace/scratch/082cfa469e03/erp-accounting-backend-schema.sql)
- [Existing-installation upgrade SQL](sandbox:/workspace/scratch/082cfa469e03/erp-accounting-backend-upgrade.sql)
- [Schema installation and operational notes](sandbox:/workspace/scratch/082cfa469e03/erp-accounting-backend-schema-README.md)
- [Managed cloud deployment](sandbox:/workspace/scratch/082cfa469e03/erp-backend-managed-cloud.md)
- [Self-managed VPS deployment](sandbox:/workspace/scratch/082cfa469e03/erp-backend-vps.md)

The cases cover the shared ERP backend for wholesaler, retailer, e-commerce, and manufacturing companies:

- tenant and company isolation;
- roles, permissions, service accounts, and row-level security;
- module enable/disable/read-only policy;
- monthly and lifetime licensing;
- double-entry accounting and fiscal-period controls;
- sales, purchasing, payments, receivables, payables;
- inventory, reservations, transfer documents, lots, costing, and manufacturing;
- effective-dated taxation and tax filing workflows;
- idempotent APIs, outbox delivery, durable jobs, and integrations;
- low-latency query behavior;
- backup, restore, failover, and deployment safety.

The test cases are automation-ready specifications. They do not claim that a Django test suite already exists. The schema smoke and behavioral scenarios were validated against the generated SQL with a PostgreSQL-compatible PGlite harness, including company provisioning, module defaults, license binding, write authorization, API receipt lifecycle, and background job claiming. A release must still run the database, concurrency, load, restore, and failover cases against the actual supported PostgreSQL 18 deployment.

## 2. Test status and evidence

| Area | Current evidence | Release requirement |
|---|---|---|
| Full schema install | Parsed and executed successfully in a PostgreSQL-compatible PGlite harness; 118 tables and 16 views were created | Repeat on a clean PostgreSQL 18 database |
| Upgrade SQL | Baseline schema followed by upgrade SQL executed successfully in the same harness | Repeat from every supported production baseline |
| Company/module behavior | Required module defaults, owner membership/permission, module enablement, and company-write gate exercised | Add Django API and RLS tests |
| Licensing behavior | Lifetime plan/term, binding, and entitlement guard exercised | Add month-boundary, expiry, revocation, quota, and platform API tests |
| Jobs/API receipts | Start/complete and job claiming exercised | Add worker crash, duplicate delivery, retry, and authorization tests |
| Performance | No production benchmark has been run | Establish p95/p99 and throughput baselines |
| Recovery | No live backup restore or failover test has been run | Run scheduled restore/PITR and deployment drills |
| Country compliance | Storage and versioning are present; no statutory rate is asserted by this document | Install reviewed country packs and approve fixture evidence per jurisdiction |

Any failure in a P0 case blocks release. A P1 failure requires an explicit risk acceptance from the product and finance owners.

## 3. Test environments

Use separate databases and credentials for every test class. SQLite is not a supported substitute for PostgreSQL behavior.

| Environment | Purpose | Required services and settings |
|---|---|---|
| Unit | Pure policies, serializers, tax rounding, entitlement resolution, command validation | Django test settings; no network calls |
| PostgreSQL integration | Constraints, triggers, RLS, views, transactions, lock behavior | PostgreSQL 18, extensions used by the schema, pytest-django, transaction=True |
| API | Authentication, CSRF, permissions, status/error contracts, idempotency | Django ASGI/WSGI app, real PostgreSQL, test object storage |
| Concurrency | Two or more independent database sessions and worker processes | PostgreSQL primary, barriers/latches, controllable clock |
| Jobs/integrations | Celery execution, Redis/broker, outbox relay, webhook adapters | Real broker/cache in containers or a dedicated test stack |
| Load | Sustained and burst traffic, hot company/period, report isolation | Production-shaped PostgreSQL, PgBouncer, load generator, metrics |
| Managed cloud | Database failover, private networking, object storage, secrets, rollback | Staging cloud topology matching production |
| VPS | Host loss, restore, reverse proxy, systemd/container restart | Staging VPS with the same backup and deployment automation |

Set a deterministic application clock in tests. Store UTC timestamps in the database, and test the configured billing and fiscal time zones explicitly.

## 4. Test data fixtures

Create factories that can build a complete isolated tenant in one transaction.

| Fixture | Required data |
|---|---|
| Tenants and companies | Tenant A with companies A1 and A2; Tenant B with company B1; independent company policy and module revisions |
| Users | Owner, tenant administrator, accountant, sales clerk, warehouse clerk, tax officer, read-only analyst, revoked user, integration user |
| Memberships | Tenant and company memberships with active/inactive states; a user belonging to A1 but not A2; no membership in B1 |
| Roles and permissions | company.modules.manage, accounting.entry.post, accounting.period.close, sales.invoice.post, purchasing.bill.post, inventory.move, tax.return.submit, report.view, and a deliberately missing permission |
| Service accounts | One company-scoped account, one tenant-scoped integration account, one revoked account, and one account with a narrow module permission |
| Currencies | PKR, USD, and a zero-decimal or three-decimal fixture if the product supports it; document exchange-rate timestamps |
| Accounting | Chart of accounts, receivable/payable/tax/inventory/COGS/revenue accounts, journals, dimensions, an open period, a closed period, and adjacent periods |
| License products/plans | Monthly plan, lifetime plan, expired plan, suspended/revoked license, plan without module.inventory, plan with activation limit one, and a published plan version |
| Tax catalog | Pakistan fixture pack with sales-tax, withholding, and income-tax component types plus effective-dated versions; a second-country fixture pack; overlapping and expired versions for negative cases |
| Parties | Customer, supplier, exempt customer, registered customer, unregistered customer, customer in another jurisdiction, addresses, and partner tax registrations |
| Items and stock | Service item, stocked item, lot-controlled item, serialized item if supported, warehouses, lots, item account profiles, reorder/availability thresholds |
| Documents | Draft and posted sales invoice, credit-note candidate, purchase bill, payment, receipt, stock receipt, shipment, transfer, BOM, production order |
| Integration | Active and paused connection, signed webhook secret, duplicate event, malformed event, and provider timeout simulator |

Every fixture should expose IDs without relying on insertion order. Use UUID/ULID factories and record the current expected row_version, policy revision, entitlement revision, and configuration revision.

## 5. Test conventions

- P0 means data loss, unauthorized access, incorrect financial/tax posting, license bypass, or unrecoverable migration risk.
- P1 means a major workflow is incorrect, unavailable, or materially slower than the agreed budget.
- P2 means a secondary workflow, report, or diagnostic behavior is wrong without corrupting authoritative data.
- DB means direct PostgreSQL integration; API means an HTTP contract test; CONC means multiple sessions/processes; UNIT means isolated Python logic; E2E spans API, database, and worker; PERF measures latency/throughput; DR covers recovery/deployment.
- Every mutation test asserts both the response and the durable database state.
- Every negative test asserts that no partial journal, tax, stock, allocation, outbox, or audit side effect remains.
- Use the same Idempotency-Key when retrying a command after a timeout. Never use a new key to “see whether” the first request committed.

## 6. Schema installation and migration cases

| ID | Priority / level | Preconditions and action | Expected result |
|---|---|---|---|
| SCHEMA-001 | P0 / DB | Run the full SQL on a clean PostgreSQL 18 database with the required extensions | The transaction completes; all expected schemas, 118 tables, 16 views, functions, triggers, indexes, and RLS policies exist |
| SCHEMA-002 | P0 / DB | Run the full SQL twice against the same database | The second run fails safely or is rejected as documented; it must not silently replace data or leave a half-installed catalog |
| SCHEMA-003 | P0 / DB | Apply the upgrade SQL after the supported baseline schema | Upgrade commits and all added identity, licensing, module, job, inventory, tax-revision, and security objects are present |
| SCHEMA-004 | P0 / DB | Force an error halfway through a migration in a disposable database | The migration transaction rolls back; no partially added constraint, trigger, view, or policy remains |
| SCHEMA-005 | P0 / DB | Insert rows with a company FK that belongs to another tenant | The FK/check rejects the write; no cross-tenant reference is stored |
| SCHEMA-006 | P0 / DB | Insert null, negative, invalid status, invalid enum, invalid currency, and invalid date-range values in representative tables | CHECK constraints reject each invalid value with a deterministic database error |
| SCHEMA-007 | P0 / DB | Insert duplicate natural keys, duplicate document numbers, duplicate idempotency receipts, and duplicate active bindings | The intended unique constraint rejects the second row without a silent merge |
| SCHEMA-008 | P1 / DB | Inspect indexes for all company-scoped, tenant-scoped, timestamp/queue, and effective-date lookup paths | Required access paths exist; no query-critical FK is left without a supporting index |
| SCHEMA-009 | P0 / DB | Insert a tax-rate version or filing period that overlaps an existing effective range | Tax-rate or filing-period overlap guard rejects the overlap |
| SCHEMA-010 | P0 / DB | Modify a posted journal, posted invoice, posted bill, posted payment, stock movement, audit event, or published plan | The immutability trigger rejects the mutation; the original row is unchanged |
| SCHEMA-011 | P1 / DB | Query the period activity, open AR/AP, inventory availability, tax component, and active-license views with representative data | Views return expected columns, sign/currency fields, company scope, and no duplicate rows |
| SCHEMA-012 | P0 / DB | Run the database parser/linter and verify routine bodies use a controlled search path | SQL parses without errors; privileged routines do not depend on attacker-controlled schemas |
| SCHEMA-013 | P1 / DB | Run a migration from each supported previous release with realistic rows, including posted documents and active licenses | Rows remain readable and all new defaults/constraints are valid; incompatible legacy data is reported before mutation |
| SCHEMA-014 | P0 / DB | Restore a backup taken before a migration, apply the migration, and compare schema fingerprints | Restored database reaches the same schema fingerprint and application health checks pass |

## 7. Authentication, authorization, and tenant isolation

| ID | Priority / level | Preconditions and action | Expected result |
|---|---|---|---|
| SEC-001 | P0 / API+DB | Authenticate a user in Tenant A and query tenants and companies | Only Tenant A and authorized companies are returned |
| SEC-002 | P0 / DB | Set transaction-local tenant/user/company context, query a company-scoped table, then clear the context | RLS permits only the intended scope; a missing/invalid context returns no rows or a controlled error |
| SEC-003 | P0 / DB | Reuse a pooled connection: request A sets Tenant A context, request B sets Tenant B context | Request B cannot see A data; transaction-local settings are cleared at transaction end |
| SEC-004 | P0 / API | User has membership in A1 but not A2; call A1 and A2 endpoints | A1 succeeds subject to permission; A2 returns 403 or 404 according to the documented resource-hiding policy |
| SEC-005 | P0 / API | User belongs to Tenant A; guess a B1 company ID in a path, body, filter, and bulk request | Every form of cross-tenant access is denied; no timing-sensitive existence leak is exposed beyond the chosen policy |
| SEC-006 | P0 / DB | Query and mutate through a company service account with the wrong company scope | RLS and service-account permission checks deny the operation |
| SEC-007 | P0 / API | Remove a user's company membership while an old session/token is active | The next request is denied; cached permissions do not continue to authorize writes |
| SEC-008 | P0 / API | Remove a role permission and retry a previously allowed command | The command returns 403; no financial side effect is committed |
| SEC-009 | P0 / API | Use a valid session without CSRF, an invalid CSRF token, and a valid token | Unsafe browser mutations fail with 403; safe GET requests follow the documented session policy |
| SEC-010 | P0 / API | Attempt login/session fixation, reuse a logged-out session, and send an expired/OIDC token | Session rotation/logout and issuer/audience/expiry checks work; secrets are absent from logs and responses |
| SEC-011 | P1 / API | Call platform-only plan publication, license issue, revoke, or tax-pack publication as a company administrator | Request is rejected; no platform catalog or license state changes |
| SEC-012 | P0 / DB | Insert/modify/delete audit rows as an ordinary user | Append-only audit protections reject the mutation; application audit records the authorized actor |
| SEC-013 | P1 / API | Submit a request with a hidden or unknown company ID in each supported endpoint family | It cannot be used to bypass the authorization order; error shape is consistent |
| SEC-014 | P1 / API | Request large page sizes, unbounded export filters, and deeply nested includes | Limits and export-job requirements are enforced; database is not exposed to unbounded work |

## 8. Company modules and policy revision

| ID | Priority / level | Preconditions and action | Expected result |
|---|---|---|---|
| MOD-001 | P0 / DB | Provision a company with an eligible license | core, accounting, and tax_calculation are enabled; optional modules are disabled; policy and module rows are created atomically |
| MOD-002 | P0 / API | Enable sales with its dependencies and a licensed feature | Change succeeds only with company.modules.manage, If-Match, idempotency key, and a current policy revision |
| MOD-003 | P0 / API | Disable accounting, tax_calculation, or another required dependency | Change is rejected with 409; all module modes remain unchanged |
| MOD-004 | P0 / API | Enable a module absent from the bound plan | Request returns 403 with a license reason; no module row changes |
| MOD-005 | P0 / API | Set a module to read_only | Read/report routes remain available; new writes/posting/stock effects are rejected with a stable module error |
| MOD-006 | P0 / API | Set a module to disabled | Ordinary module routes are rejected; permitted historical reads and audit routes follow the documented exception |
| MOD-007 | P0 / API | Omit If-Match or send a stale policy revision | Responses are 428 and 412 respectively; no partial batch applies |
| MOD-008 | P0 / CONC | Submit two module-change batches from the same revision | Exactly one succeeds; the other receives 412 and must refresh |
| MOD-009 | P0 / CONC | Race module disable with an invoice/stock/production command | Already-authorized command either completes before the disable commit or is rejected after it; no command commits under a disabled policy |
| MOD-010 | P0 / API | Apply a batch where one requested module has a dependency conflict | Entire batch rolls back; all prior modes, revision, audit, and outbox state remain unchanged |
| MOD-011 | P1 / API | Enable/disable the same module twice with the same idempotency key | Replay returns the original result; no duplicate audit or revision increment |
| MOD-012 | P1 / API | Inspect capabilities after license, permission, and module changes | Effective actions and denial reasons reflect all three gates, with current policy/entitlement revision |
| MOD-013 | P0 / API+DB | Attempt to edit module settings directly around the guarded function/endpoint | Trigger/function and application permission prevent bypass; actor, time, revision, and audit are populated |
| MOD-014 | P1 / E2E | Disable a module with queued worker jobs | Worker re-checks policy before a new business effect; job is paused/cancelled according to policy and cannot bypass the disable |
| MOD-015 | P1 / API | Configure different module modes for A1 and A2 | Settings and capabilities are independent; a change in A1 does not alter A2 |

## 9. Licensing, validity, and entitlements

| ID | Priority / level | Preconditions and action | Expected result |
|---|---|---|---|
| LIC-001 | P0 / DB | Issue a license credential through the platform flow | Only a one-way digest and permitted last-four display value are persisted; raw credential is never returned by ordinary reads/logs |
| LIC-002 | P0 / API | Bind a tenant/product to one designated license when multiple licenses exist | Binding is explicit; entitlement resolution never arbitrarily sums or selects another license |
| LIC-003 | P0 / DB | Add a monthly term on January 31, February 28/29, and across a DST boundary using the configured billing timezone | Term has the documented calendar-month end and UTC boundaries; no overlap is created |
| LIC-004 | P0 / DB | Add a lifetime term | expires_at is null/represented as lifetime according to schema; it remains valid until suspension/revocation |
| LIC-005 | P0 / API | Call a licensed write immediately before expiry, exactly at expiry, and after expiry using an authoritative acceptance clock | Boundary behavior is deterministic; expired entitlement blocks protected writes |
| LIC-006 | P0 / API | Suspend, revoke, and reactivate a license while attempting a protected command | Exclusive entitlement coordination prevents a command from committing under a suspended/revoked state |
| LIC-007 | P0 / API | Use a plan without module.manufacturing to enable manufacturing or post production | Request is denied; accounting and stock state remain unchanged |
| LIC-008 | P0 / DB | Attempt to issue a term against an unpublished plan version or the wrong product | Guard rejects the insert |
| LIC-009 | P0 / DB | Update a published plan version or published plan feature | Immutability trigger rejects the update/delete; a new version is required |
| LIC-010 | P0 / CONC | Two devices activate against a license with quota one | One activation succeeds; the other receives a quota conflict; no over-quota state is visible |
| LIC-011 | P1 / API | Heartbeat an existing activation after expiry; then deactivate it | Heartbeat cannot extend validity or consume a new quota; deactivation is allowed to release capacity |
| LIC-012 | P0 / API | Redeem the same license credential twice with the same and different idempotency keys | Same key replays the first result; a different key is rejected as already redeemed/bound |
| LIC-013 | P1 / API | Create a renewal order, retry after a timeout, and replay after fulfillment | One term and one billing event are created; order/result is discoverable by idempotency key |
| LIC-014 | P0 / DB | Insert a signed grant with invalid signature, subject, audience, time window, or entitlement revision | Grant validity guard rejects it |
| LIC-015 | P1 / API | Change binding, renew, revoke, and inspect entitlement cache/version | entitlement_revision increments; stale cache is rejected or refreshed before a protected write |
| LIC-016 | P0 / API | Present a raw license number as an API bearer token | Authentication fails; license number is not accepted as identity |
| LIC-017 | P1 / API | Query license summary as company admin and as an unauthorized user | Authorized response omits hashes, secrets, signing material, and payment payloads; unauthorized response is denied |
| LIC-018 | P0 / DB | Attempt to reduce activation quota below active installations | Operation is rejected or requires an explicit migration workflow; active installations are not silently invalidated |

## 10. General accounting and fiscal periods

| ID | Priority / level | Preconditions and action | Expected result |
|---|---|---|---|
| GL-001 | P0 / DB | Insert a draft journal with balanced debit and credit lines | Journal is accepted; totals are represented in journal and functional currency rules |
| GL-002 | P0 / DB | Insert an unbalanced journal or a line with invalid account/currency | Balance/constraint validation rejects posting; no posted entry exists |
| GL-003 | P0 / API | Post a journal in an open period | Entry transitions once to posted, sequence is allocated atomically, audit/outbox are written |
| GL-004 | P0 / API | Post into a closed, locked, or future-ineligible period | Request is rejected; journal remains draft and no side effect is written |
| GL-005 | P0 / CONC | Post two independent journals in the same open period | Both can proceed under shared period locks when otherwise valid; neither sees a partial close |
| GL-006 | P0 / CONC | Race a posting with period close | Close waits for or precedes the shared lock according to arrival; final state has either a fully posted journal in an open period or a closed period with no late post |
| GL-007 | P0 / API | Close a period with unposted drafts, unreconciled allocations, or tax blockers | Close is rejected with actionable blockers; period stays open |
| GL-008 | P0 / API | Reopen a closed period without permission, reason, or audited approval | Request is denied; no period state changes |
| GL-009 | P0 / DB | Update posted journal header, line, dimension, or source link | Immutability guards reject the update/delete |
| GL-010 | P0 / API | Reverse a posted entry into an allowed open period | A new balanced opposite entry is created with a unique reversal link; source and audit links are retained |
| GL-011 | P0 / API | Reverse an invoice/payment-linked journal directly | Request is blocked or routed through source-document correction; no orphaned tax, stock, or allocation state is created |
| GL-012 | P1 / API | Post a multi-currency journal with exchange rate, rounding, and zero/three-decimal currency fixtures | Functional and transaction currency totals follow documented rounding; rate/evidence is retained |
| GL-013 | P0 / API | Retry posting with the same idempotency key after a simulated lost response | The original journal/result is returned; no duplicate sequence or GL effect occurs |
| GL-014 | P0 / DB | Supply a stale row_version or draft revision | Update returns a conflict; newer changes are not overwritten |
| GL-015 | P1 / DB | Allocate document numbers concurrently for the same company/series | Numbers are unique, gap behavior is documented, and no duplicate document number is committed |
| GL-016 | P1 / API | Post with a missing account mapping, inactive account, or wrong company account | Validation fails before the journal is posted |
| GL-017 | P0 / DB | Supply a cross-company source-to-journal link | FK/check/RLS rejects it |
| GL-018 | P1 / E2E | Generate trial balance and period activity after postings, reversal, and correction | Opening, movement, closing, debit/credit signs, and currency scope reconcile to the immutable journal |

## 11. Sales, purchasing, payments, and settlement

| ID | Priority / level | Preconditions and action | Expected result |
|---|---|---|---|
| SAL-001 | P1 / API | Create a sales invoice draft with up to 50 lines, addresses, currency, and tax code | Draft totals and revision are correct; no GL/stock effect exists |
| SAL-002 | P0 / API | Edit a draft line with the current revision, then with a stale revision | Current edit succeeds; stale edit returns conflict and preserves the latest draft |
| SAL-003 | P0 / API | Calculate a draft using a versioned local tax configuration | Tax snapshots include jurisdiction, rule/version, base, rate, rounding, recoverability, and currency |
| SAL-004 | P0 / API | Post an eligible invoice with sales and tax modules enabled | Invoice, tax snapshots, journal, audit, idempotency result, and outbox commit atomically |
| SAL-005 | P0 / API | Post an invoice while sales is disabled/read-only or tax calculation is disabled | Request is denied before financial/stock effects |
| SAL-006 | P0 / API | Post a taxable invoice with missing/expired partner registration or invalid tax code | Request fails with a tax configuration reason; draft remains recoverable |
| SAL-007 | P0 / API | Post a credit note/correction with a source invoice | Sign/polarity, tax reversal, receivable, and source link are correct; arbitrary negative invoice behavior is blocked |
| SAL-008 | P0 / CONC | Two users post the same draft with different idempotency keys | Exactly one post wins; the other sees posted/conflict state without duplicate GL, tax, or stock |
| SAL-009 | P1 / API | Post an invoice that consumes stock versus a service-only invoice | Stock module/action is required only for the stock-consuming path; service-only posting remains valid |
| SAL-010 | P1 / API | Query invoice by company and tenant filters, including a hidden ID | Only authorized invoices are returned; no cross-company result |
| PUR-001 | P1 / API | Create and revise a purchase bill draft with supplier tax components | Draft remains unposted; tax snapshots and payable totals are deterministic |
| PUR-002 | P0 / API | Post an eligible purchase bill | Payable, recoverable/input tax, inventory/expense, journal, audit, outbox, and idempotency state commit atomically |
| PUR-003 | P0 / API | Post a bill with supplier registration/jurisdiction conflict or invalid tax effective date | Request is rejected without a payable or tax liability |
| PUR-004 | P0 / API | Post the same bill twice after a timeout | One bill/journal; replay returns the original result |
| PAY-001 | P0 / API | Record a receipt/disbursement with cash/bank amount and optional withholding | Payment and withholding components are separated; cash and tax-liability mapping reconcile |
| PAY-002 | P0 / API | Allocate a payment to one or more open AR/AP items | Allocation totals, currencies, signs, and remaining balances are correct |
| PAY-003 | P0 / CONC | Two sessions allocate more than the remaining amount of one invoice | Row locking/constraint allows at most the remaining amount; one transaction receives a conflict |
| PAY-004 | P0 / API | Attempt allocation across companies, currencies, or incompatible document directions | Request is rejected; no partial allocation remains |
| PAY-005 | P1 / API | Reverse a payment with existing allocations | Source payment and allocations follow the documented correction workflow; open-item view reconciles |
| PAY-006 | P1 / E2E | Generate AR/AP open-item and aging reports before and after allocation/reversal | Current open balances are correct; cutoff aging reconstructs effective allocations rather than using today's state |

## 12. Inventory and manufacturing

| ID | Priority / level | Preconditions and action | Expected result |
|---|---|---|---|
| INV-001 | P0 / DB | Insert a valid stock receipt document and complete its event | Stock movement and inventory position projection update atomically |
| INV-002 | P0 / DB | Insert a shipment/issue that would make available quantity negative | Policy rejects it or reserves/backorders it according to company configuration; actual stock never becomes inconsistent |
| INV-003 | P0 / CONC | Two sessions reserve the same available stock | Reservations serialize; combined reserved quantity cannot exceed availability |
| INV-004 | P0 / DB | Edit/delete a posted stock movement | Append-only trigger rejects the change; a compensating document is required |
| INV-005 | P1 / API | Transfer stock between two warehouses | Source decrease, destination increase, lot/cost links, and audit are atomic |
| INV-006 | P0 / API | Receive a lot-controlled item without a lot or with a duplicate lot/warehouse key | Validation rejects it; no quantity projection is changed |
| INV-007 | P1 / API | Rebuild inventory positions from immutable movements and compare to the live projection | Rebuild matches current position and availability views |
| INV-008 | P1 / API | Post a sales shipment linked to an invoice and then retry | Fulfillment is not duplicated; invoice/stock links remain one-to-one where required |
| INV-009 | P0 / API | Cost a stock issue using FIFO/weighted-average configuration | Cost allocation is deterministic, balanced, and retained with the movement/journal |
| MFG-001 | P1 / API | Create and approve a BOM with component lines and effective dates | BOM is versioned, validated, and only active approved versions can be used |
| MFG-002 | P0 / API | Create production order with an inactive BOM, disabled manufacturing module, or unavailable materials | Request is rejected before material issue |
| MFG-003 | P0 / API | Issue materials to a production order | Material issue is append-only, consumes reservations/costs, and maps to WIP/stock accounts |
| MFG-004 | P0 / API | Record production output | Output quantity/cost and WIP transfer reconcile; duplicate output command is idempotent |
| MFG-005 | P0 / CONC | Two workers complete the same production order | One completion wins; the other sees a terminal/idempotent result; no duplicate output |
| MFG-006 | P1 / API | Cancel a production order after partial issue | Cancellation preserves immutable history and creates required reversals/returns; it does not delete issues |
| MFG-007 | P0 / API | Change BOM or production order with stale row_version | Update conflicts; an in-flight production command cannot use an unintended revision |
| MFG-008 | P1 / E2E | Post a manufactured item into inventory and query inventory valuation/COGS | Material, labor/overhead policy, output, and valuation reconcile to posted GL |

## 13. Taxation and country packs

Tax tests must use an approved fixture pack. This document does not invent current Pakistan or other-country statutory rates. A fixture should include the legal source/version, effective dates, rounding policy, registration conditions, exemptions, account mapping, and expected examples.

| ID | Priority / level | Preconditions and action | Expected result |
|---|---|---|---|
| TAX-001 | P0 / DB | Publish an approved jurisdiction/tax pack with a unique effective version | Pack is immutable after publication and has an auditable release/version identity |
| TAX-002 | P0 / DB | Insert overlapping rate versions for the same jurisdiction/type/base condition | Overlap guard rejects the second version |
| TAX-003 | P0 / API | Quote tax for a Pakistan taxable sale using a registered customer and approved sales-tax fixture | Component type, base, rate/version, rounding, tax account role, and currency are deterministic |
| TAX-004 | P0 / API | Quote a Pakistan sale with exempt customer, unregistered customer, invalid registration, and out-of-jurisdiction place of supply | Applicable exemption/registration rules are selected; invalid combinations fail clearly |
| TAX-005 | P0 / API | Calculate withholding on a payment to a supplier/customer using an approved fixture | Withholding is a distinct payment tax component and maps to the correct liability/settlement account |
| TAX-006 | P0 / API | Build an income-tax assessment workpaper from approved sources | Assessment lines reconcile to source facts and cannot be posted without validated period/journal linkage |
| TAX-007 | P0 / API | Use a second-country pack with a different tax hierarchy, rates, and filing calendar | Country rules stay isolated by jurisdiction/version; Pakistan configuration is not applied accidentally |
| TAX-008 | P0 / API | Calculate a document across a tax-rate effective-date boundary | Tax point selects the correct version; snapshot remains stable after a future rate publication |
| TAX-009 | P0 / API | Change tax code/rate after a draft calculation but before posting | Posting revalidates configuration and either recalculates explicitly or returns a stale-configuration conflict |
| TAX-010 | P0 / DB | Modify tax components on a posted invoice/bill/payment | Draft-only guard rejects mutation; posted component snapshot is immutable |
| TAX-011 | P0 / API | Create tax return revision, calculate it, approve/freeze it, and amend it | Each revision is immutable after freeze; amendment creates a new revision linked to the original |
| TAX-012 | P0 / API | Submit the same frozen return twice with same and different idempotency keys | One submission attempt/provider request is recorded; replay returns the same operation |
| TAX-013 | P1 / E2E | Simulate tax provider timeout, retry, late callback, duplicate callback, and invalid signature | Posting remains local; submission attempt/status is durable; callbacks are authenticated and deduplicated |
| TAX-014 | P0 / DB | Query the v2 tax component view for invoices, credit notes, purchases, payments, currencies, and signs | Tax report preserves currency and debit/credit polarity and reconciles to snapshots |
| TAX-015 | P1 / API | Run tax report at a historical cutoff after a tax code/rate change | Report uses effective source snapshots and returns a reproducible result |
| TAX-016 | P0 / API | Disable tax_filing while keeping tax_calculation enabled | Quotes and document snapshots work; return creation/submission is denied |
| TAX-017 | P0 / API | Disable tax_calculation while a sales/purchase posting requires tax | Posting is denied; no document/journal/stock effect is committed |
| TAX-018 | P1 / DB | Attempt to post an assessment with mismatched workpaper totals or an unrelated journal | Guard rejects it; no posted assessment or GL link exists |

## 14. API contract and idempotency cases

| ID | Priority / level | Preconditions and action | Expected result |
|---|---|---|---|
| API-001 | P1 / API | Send valid and invalid JSON to representative endpoints | Responses use the documented error envelope with stable code, field details, request ID, and no traceback |
| API-002 | P0 / API | Omit authentication, permission, module, license, or company membership in turn | Status maps to the documented 401/403/404 policy; no side effect is written |
| API-003 | P0 / API | Post without Idempotency-Key, with an empty key, and with an overlong key | Required commands return 400/428; read-only requests follow their documented policy |
| API-004 | P0 / API | Reuse a key with the same payload and with a different payload | Same payload replays the stored result; different payload returns an idempotency conflict |
| API-005 | P0 / API | Drop the network after server commit, then retry the same command | Retry discovers the committed receipt/result; no duplicate business effect |
| API-006 | P0 / API | Replay a completed receipt after the caller loses permission to view its result | Business command is not re-run; response is denied or redacted according to current authorization |
| API-007 | P1 / API | Use cursor/keyset pagination while rows are added/updated between pages | No unexpected duplicate/skip within documented consistency guarantees; cursor cannot escape scope |
| API-008 | P1 / API | Request filters/sorts on unsupported or unindexed fields | Request is rejected or bounded; server does not construct arbitrary SQL |
| API-009 | P1 / API | Trigger an asynchronous export, tax submission, or long report | Returns 202 with job/operation URL; job state is authorized and durable |
| API-010 | P1 / API | Simulate database/broker outage and retryable provider response | 503/429 includes retry guidance; same idempotency key remains safe |
| API-011 | P0 / API | Send an oversized body, deeply nested JSON, invalid UTF-8, and repeated line arrays | Limits reject the request before expensive database work; no partial rows exist |
| API-012 | P0 / API | Try to patch a posted document status, journal totals, tax snapshot, or stock movement | API returns a controlled immutability error and leaves the posted record unchanged |
| API-013 | P1 / API | Call module admin with stale If-Match, then repeat after refresh | 412 response contains current revision/capabilities where allowed; refreshed request can proceed |
| API-014 | P1 / API | Verify OpenAPI schemas for all error, pagination, command receipt, job, and capability responses | Contract tests detect undocumented field/status changes |

## 15. Jobs, outbox, and integrations

| ID | Priority / level | Preconditions and action | Expected result |
|---|---|---|---|
| JOB-001 | P0 / DB | Commit an invoice/tax/license command with an outbox event | Business rows and outbox intent commit in the same transaction; rollback removes both |
| JOB-002 | P0 / DB | Claim outbox rows from two relay workers | claim_outbox_events leases each row to one worker at a time |
| JOB-003 | P1 / E2E | Crash a relay after publish and before completion | Duplicate delivery is possible but consumer dedupe prevents duplicate business effects |
| JOB-004 | P1 / E2E | Complete an outbox event with the wrong worker/lease or twice | Completion is rejected/ignored safely; event remains auditable |
| JOB-005 | P0 / DB | Create a background job and start it twice | Job uniqueness returns the same durable operation; no duplicate execution |
| JOB-006 | P0 / E2E | Kill a worker holding a job lease, wait for timeout, then run the claimant | Job is reclaimed exactly according to lease policy; attempts and errors are recorded |
| JOB-007 | P1 / E2E | Retry a transient provider error and then receive success | Retry/backoff follows limits; final state and provider reference are idempotent |
| JOB-008 | P0 / E2E | Deliver a webhook with valid signature, duplicate event ID, bad signature, changed body, and stale timestamp | Only the valid first delivery is processed; duplicates are acknowledged/deduped; invalid messages are rejected/quarantined |
| JOB-009 | P1 / E2E | Disable a module while a job is queued | Worker rechecks current module/license/permission before a new business transaction |
| JOB-010 | P1 / API | Request a job result as an unauthorized company user or another tenant | Result is denied; payload/object storage URL is not leaked |
| JOB-011 | P1 / API | Cancel a cancellable job while it is queued and while it is running | State transition is race-safe; a running financial command cannot be falsely reported cancelled |
| JOB-012 | P1 / OPS | Fill the dead-letter queue and poison-message threshold | Alerting fires, queue is inspectable, and replay requires an explicit authorized action |
| JOB-013 | P1 / E2E | Run scheduled report/export jobs while a read replica is lagging | Strongly consistent reports route to primary or expose freshness; stale output is labeled |
| JOB-014 | P1 / E2E | Rotate integration secret and deliver old/new signatures during the overlap window | Documented key rotation policy works; old key expires at the expected time |

## 16. Reporting and read consistency

| ID | Priority / level | Preconditions and action | Expected result |
|---|---|---|---|
| REP-001 | P0 / DB | Compare trial balance closing totals to journal debit/credit totals for a period | Trial balance balances and agrees with authoritative posted journals |
| REP-002 | P0 / DB | Compare GL activity to source invoice, bill, payment, stock, and production documents | Every business posting has expected source links and mapped accounts; exceptions are visible |
| REP-003 | P0 / DB | Generate AR/AP current open items after partial, full, overpayment, reversal, and credit-note cases | Open balances and signs are correct; over-allocation is rejected |
| REP-004 | P1 / DB | Generate aging at two historical cutoff dates after later allocations | Each cutoff reflects allocations effective by that date, not only current open balance |
| REP-005 | P1 / DB | Compare inventory availability, on-hand, reservation, movement, and valuation views | Projections and authoritative movements reconcile within documented cost/rounding rules |
| REP-006 | P0 / DB | Generate tax-component report across sales, purchases, withholding, credits, and currencies | Report sums to immutable tax snapshots and preserves country/jurisdiction/version |
| REP-007 | P1 / API | Immediately read a newly posted document using primary and replica routing | Post-then-read uses primary or an explicit read-your-write mechanism |
| REP-008 | P1 / API | Run a large report concurrently with invoice posting | Report does not block short writes beyond the budget; snapshot consistency is documented |
| REP-009 | P1 / API | Export a report asynchronously and download it after completion | Export is scoped, signed/expiring, complete, and auditable |
| REP-010 | P1 / DB | Run the same report twice with unchanged authoritative data | Result is reproducible; cache invalidation occurs after posting/correction |

## 17. Performance and scalability cases

The architecture's initial targets are planning budgets, not measured results:

| Operation | Initial p95 target | Initial p99 target |
|---|---:|---:|
| Small authorized read | 75 ms | 200 ms |
| Company list/filter | 125 ms | 350 ms |
| Invoice posting up to 50 lines | 300 ms | 900 ms |
| Long report/export | Asynchronous | Asynchronous |

Measure on a production-shaped dataset and record PostgreSQL version, instance size, pool size, replica lag, cache state, and concurrency. Fail a performance gate only against an approved baseline, not a developer laptop.

| ID | Priority / level | Workload and action | Expected result |
|---|---|---|---|
| PERF-001 | P1 / PERF | Warm/cold authorized company reads at 1, 10, 50, and 100 concurrent users | p95/p99 are recorded and remain within approved budget; no tenant-scope query scans another company |
| PERF-002 | P0 / PERF | Post invoices with 1, 10, 50, and maximum supported lines under low contention | Latency, rows/locks, and journal/tax correctness meet the posting SLO |
| PERF-003 | P0 / PERF | Post in a hot fiscal period with concurrent readers/posters | Shared-lock design prevents unnecessary serialization; deadlocks/serialization retries stay within policy |
| PERF-004 | P0 / PERF | Reserve/ship the same hot SKU from many workers | No over-reservation; lock wait and retry rates are visible |
| PERF-005 | P1 / PERF | Run AR/AP aging and tax reports on a large company while posting | Reporting is bounded/asynchronous or isolated; posting latency remains within budget |
| PERF-006 | P1 / PERF | Test with connection-pool saturation and slow/failed cache | Requests fail gracefully or fall back to primary; no connection leak |
| PERF-007 | P1 / PERF | Run 30-minute soak with mixed reads, posts, jobs, and webhooks | Error rate, latency, queue depth, bloat, and memory remain stable |
| PERF-008 | P1 / PERF | Run a high-cardinality tenant/company distribution, not only one tenant | Hot-company behavior and cross-tenant indexes remain acceptable |
| PERF-009 | P1 / PERF | Trigger background job surge and relay backlog | Queue backpressure, rate limits, and worker autoscaling keep financial command latency predictable |
| PERF-010 | P1 / PERF | Use EXPLAIN (ANALYZE, BUFFERS) for authorization, open items, availability, tax, and posting queries | Plans use intended indexes/partitions; no accidental sequential scan on high-volume tables |
| PERF-011 | P1 / PERF | Insert historical journal/movement data to the retention horizon and run vacuum/analyze | Table/index growth, autovacuum, and query plans remain within operating thresholds |
| PERF-012 | P1 / PERF | Measure replica lag during report/export load | Lag thresholds trigger routing/alerts; strong-consistency reads do not silently use stale data |

## 18. Deployment, backup, and disaster recovery

| ID | Priority / level | Workload and action | Expected result |
|---|---|---|---|
| DEP-001 | P0 / DR | Take a logical backup and restore it into a clean PostgreSQL 18 instance | Schema, data, views, routines, permissions, and application health checks pass |
| DEP-002 | P0 / DR | Restore a base backup plus WAL to a point before an accidental document mutation | PITR stops at the requested time; financial/audit evidence is intact |
| DEP-003 | P0 / DR | Restore the latest production-like backup on the scheduled cadence | RPO/RTO are measured and meet approved targets; restore is not only a backup-file existence check |
| DEP-004 | P0 / DR | Simulate managed database failover during an idempotent posting | Client retry with the same key resolves the result; no duplicate GL or stock effect |
| DEP-005 | P0 / DR | Simulate VPS host loss and restore database, broker, object store, and secrets on a replacement host | Services restart in documented order; private network/TLS/health checks pass |
| DEP-006 | P0 / DEP | Apply a backward-compatible migration while old application workers remain online | No incompatible write is accepted; rolling deploy and rollback are safe |
| DEP-007 | P0 / DEP | Apply a migration with a deliberate preflight failure on legacy data | Deployment stops before destructive changes; operator receives the offending rows and remediation |
| DEP-008 | P1 / DEP | Rotate database, broker, signing, webhook, and object-storage secrets | Dual-key overlap/rotation works; old secrets expire; no secret appears in logs |
| DEP-009 | P0 / DEP | Verify database is private, TLS is required, backups are encrypted, and least-privilege roles are used | Network and privilege checks pass; public database access is impossible |
| DEP-010 | P1 / DR | Lose cache, object storage, or broker while PostgreSQL remains available | Protected writes use safe fallback or return retryable errors; committed business records remain authoritative |
| DEP-011 | P1 / DR | Rebuild read projections and replay outbox events after restore | Inventory/report projections and integrations reach a documented checkpoint without duplicate effects |
| DEP-012 | P1 / DEP | Roll back application code after a migration and then forward deploy | Compatibility contract and migration policy prevent old code from corrupting new rows |

## 19. Critical concurrency test procedures

Run these with two independent PostgreSQL connections. Django TestCase is insufficient because it wraps one transaction; use pytest.mark.django_db(transaction=True) and separate connections/processes. Use barriers so the race is deterministic.

### 19.1 Posting versus period close

1. Session A starts invoice posting, acquires the idempotency row, entitlement/policy rows, and the fiscal period FOR SHARE lock.
2. Pause A before commit.
3. Session B attempts to close the same period and must request the period FOR UPDATE lock.
4. Assert B waits rather than observes a half-posted journal.
5. Commit A, then let B continue. B must either close after a complete post or report a blocker according to policy.
6. Repeat with B acquiring the exclusive lock first. A must reject after the period becomes closed.
7. Verify journal, source document, tax, outbox, audit, and period state for both outcomes.

### 19.2 Module disable versus posting

1. Session A begins a posting command and locks the company policy row plus selected module state.
2. Session B submits a module-disable batch with the current If-Match revision.
3. Assert the two commands serialize on the policy row.
4. If A commits first, the posting is complete and B applies the disable with an incremented revision.
5. If B commits first, A rechecks the policy and returns a module denial with no partial financial effect.
6. Verify queued workers perform the same recheck.

### 19.3 Concurrent license activation

1. Configure one active license and activation quota one.
2. Two sessions submit different activation IDs at the same time.
3. Both must use the entitlement/license lock order.
4. Assert exactly one active activation, one quota conflict, and no duplicate license events.
5. Repeat with a heartbeat on an existing activation to prove a heartbeat does not consume a new quota slot.

### 19.4 Concurrent allocation or stock reservation

1. Create an invoice/open item or SKU with a remaining quantity/amount of 100.
2. Two sessions request 80 each.
3. Assert only 100 can be allocated/reserved in total; the losing transaction receives a conflict or is reduced according to the documented policy.
4. Roll back one session and prove the other can complete safely.
5. Verify AR/AP open views or inventory availability match the committed rows.

### 19.5 Unknown commit and replay

1. Send a command with an idempotency key.
2. Allow the server to commit, then drop the response connection.
3. Retry with the same key.
4. Assert the response is the original receipt/result and no duplicate journal, document number, stock movement, tax snapshot, outbox event, or job exists.

## 20. Automation layout

Use a test tree that mirrors the domain and allows selective gates:

~~~text
tests/
  conftest.py
  factories/
    identity.py
    companies.py
    accounting.py
    licensing.py
    tax.py
    inventory.py
  unit/
    test_entitlement_resolver.py
    test_tax_rounding.py
    test_policy_dependencies.py
    test_api_error_mapping.py
  integration/
    test_schema_and_constraints.py
    test_posting.py
    test_periods.py
    test_sales_purchasing.py
    test_payments.py
    test_inventory.py
    test_manufacturing.py
    test_views.py
  api/
    test_auth_and_csrf.py
    test_modules.py
    test_licenses.py
    test_accounting.py
    test_taxation.py
    test_jobs.py
  security/
    test_rls.py
    test_service_accounts.py
    test_audit_immutability.py
  concurrency/
    test_period_close_race.py
    test_module_disable_race.py
    test_license_quota_race.py
    test_allocation_race.py
  taxation/
    test_pakistan_pack.py
    test_country_pack_contract.py
    test_returns_and_submission.py
  performance/
    test_hot_period.py
    test_hot_sku.py
    test_reports.py
  recovery/
    test_backup_restore.py
    test_outbox_replay.py
    test_migration_rollback.py
~~~

Recommended markers:

~~~text
unit, integration, api, security, concurrency, taxation, jobs, performance, recovery, p0, p1, slow
~~~

Illustrative Django/pytest shape (adapt paths and service names to the implementation):

~~~python
import pytest
from django.urls import reverse

@pytest.mark.django_db(transaction=True)
@pytest.mark.p0
def test_post_invoice_is_idempotent(api_client, invoice_factory, owner_user, company):
    invoice = invoice_factory(company=company, status="draft")
    api_client.force_login(owner_user)
    url = reverse(
        "sales-invoice-post",
        kwargs={"tenant_id": company.tenant_id, "company_id": company.id,
                "invoice_id": invoice.id},
    )
    headers = {"HTTP_IDEMPOTENCY_KEY": "post-invoice-0001"}
    first = api_client.post(url, {"fiscal_period_id": str(company.open_period_id)},
                            content_type="application/json", **headers)
    second = api_client.post(url, {"fiscal_period_id": str(company.open_period_id)},
                             content_type="application/json", **headers)

    assert first.status_code in {200, 202}
    assert second.status_code == first.status_code
    assert second.json()["operation_id"] == first.json()["operation_id"]
    assert invoice.__class__.objects.filter(id=invoice.id, status="posted").count() == 1
~~~

~~~python
@pytest.mark.django_db(transaction=True)
@pytest.mark.concurrency
def test_period_close_waits_for_posting(two_db_sessions, invoice_factory, company):
    # Pseudocode: use two real connections, a barrier, and a server-side lock
    # rather than two cursors on one Django transaction.
    poster, closer = two_db_sessions
    barrier = poster.pause_after_period_share_lock()
    post = poster.start_posting(company=company)
    barrier.wait_until_reached()

    close = closer.start_close(company.open_period_id)
    assert close.is_waiting_on_period_lock()

    post.commit()
    close.finish()
    company.refresh_from_db()
    assert company.period_is_closed_or_blocked_by_reconciliation()
~~~

Do not copy these snippets into production unchanged. They demonstrate the required transaction boundary and assertions; actual route names, factories, and service APIs depend on the Django implementation.

## 21. CI and release gates

Run in this order so failures are cheap and diagnosable:

1. Format, lint, type checks, dependency/audit checks, and migration graph validation.
2. Unit tests and serializer/error contract tests.
3. Clean-database full schema install and supported upgrade smoke.
4. PostgreSQL integration tests for constraints, triggers, views, RLS, posting, licensing, tax, stock, and module policy.
5. API tests including CSRF, permissions, idempotency, pagination, and OpenAPI compatibility.
6. Concurrency tests with real independent sessions.
7. Worker/outbox/integration tests with broker and cache.
8. P0 performance smoke; full workload and soak tests nightly or before a major release.
9. Backup restore/PITR and migration rollback on a scheduled staging job.
10. Managed-cloud and VPS deployment smoke after infrastructure changes.

A pull request cannot merge if any P0 case fails, if schema install/upgrade is not reproducible, if an RLS test leaks a tenant/company row, or if an idempotent command produces a duplicate financial effect.

## 22. P0 release gate matrix

| Gate | Minimum evidence |
|---|---|
| Isolation | SEC-001 through SEC-008, SEC-012, and direct RLS tests pass |
| Module authorization | MOD-001 through MOD-010 and race test pass |
| License enforcement | LIC-002 through LIC-010, LIC-012, and expiry/revocation cases pass |
| Ledger integrity | GL-001 through GL-014, including close/post race, pass |
| Source-document integrity | Sales, purchasing, payments, inventory, and manufacturing P0 cases pass |
| Tax correctness | Approved jurisdiction fixtures, effective dates, snapshots, returns, and assessment guards pass |
| Idempotency | API-003 through API-006, duplicate worker/event cases, and unknown-commit replay pass |
| Recovery | Backup restore, PITR, failover, and migration rollback evidence is current |
| Operations | Metrics, alerts, queue depth, lock waits, audit trails, and safe retry behavior are verified |

## 23. Open implementation decisions to resolve before coding

1. Final Django app names, route names, and serializer schemas must be frozen so API contract tests can be generated.
2. Select and document the supported PostgreSQL extension set and exact major/minor version.
3. Define the fiscal/billing timezone and month-end rule for monthly license terms.
4. Choose the inventory negative-stock policy and costing method per company.
5. Publish reviewed country packs, including Pakistan sales tax, withholding, and income-tax workflows, with legal source/version and effective dates.
6. Decide whether returns, refunds, serialized stock, payroll withholding, and e-commerce settlement fees are in the first release.
7. Set measured p95/p99, throughput, RPO, and RTO budgets after a production-shaped benchmark.
8. Decide whether the first deployment uses PgBouncer, read replicas, partitioning, and a managed queue, then add the corresponding deployment assertions.
9. Define data retention, audit retention, export/redaction, and tenant deletion policies.
10. Keep the authoritative journal, tax snapshots, license history, audit log, and outbox durable even when read projections and caches are rebuilt.

The suite should grow by adding a regression case for every production incident, migration correction, country-pack change, and newly supported business workflow.

