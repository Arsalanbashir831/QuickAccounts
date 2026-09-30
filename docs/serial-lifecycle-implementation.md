# Serial lifecycle implementation

Serial tracking is an overlay on the existing quantity and valuation ledger. A
serial-tracked item still uses one inventory lot and one unit of stock movement
per physical unit. `erp.inventory_serials` is the stable identity/current-location
projection; `erp.serial_movements` is its immutable transition history. Inventory
positions, stock movements, cost layers, and journals remain authoritative for
quantities, value, and accounting.

## Posting routes

| Event | Existing command plus serial selection |
| --- | --- |
| Supplier bill receipt | Post the bill with `warehouse_id` and `serial_receipts: [{purchase_bill_line_id, serial_numbers}]`. The count must equal each tracked bill line. |
| Separate goods receipt | Register up to 2,000 labels through `POST inventory/lots/serial-bulk`, then post a typed receipt with one `lot_id` and quantity 1 per line. |
| Transfer/reservation/sale | Use typed transfer, reservation, and shipment commands with the unit's `lot_id`. Sales invoices use deferred stock fulfillment. |
| Customer return | Supply exact `serial_ids` during linked return inspection, then post the return. Each serial must have been shipped on the source invoice line. |
| Repair/QC/restock/scrap | Create a repair job with `serial_id` and quantity 1. After QC approval, use return stock disposition with that serial and repair job to reach sellable stock. Explicitly approved write-off uses the same disposition command without a destination. |
| Replacement | Post the linked replacement invoice with deferred stock fulfillment, ship its exact lot, then link the returned and replacement serials through `POST sales/returns/{return_id}/replacement-serials`. Repeat this for later generations. |
| Supplier credit | Post a linked stocked supplier credit with `serial_returns: [{purchase_bill_line_id, serial_ids}]` and the current return warehouse. Every selected unit must originate from that bill's source line; selected inventory cost must match the credit amount. Variances are blocked. |

All commands retain their existing company/module/permission, idempotency-key,
revision, fiscal-period, audit, and outbox behavior. Serial and stock effects
share the posting transaction; failed journals or reconciliation roll back
the identity and history changes.

## Lookup and scale boundaries

`GET inventory/serials?serial=...` is an indexed, normalized exact lookup.
The collection also accepts item, warehouse, or source-document filters and
returns a keyset cursor (maximum 200 rows). Detail and history endpoints are
`inventory/serials/{id}` and `inventory/serials/{id}/history`; the latter is
keyset-paginated. Replacement links are separately paginated at
`sales/returns/{return_id}/replacement-serials`. Document responses do not
embed serial arrays.

Registration uses one PostgreSQL bulk insert. Large typed receipts batch stock
movement insertion, while database triggers still validate and project every
unit. The deferred stock validator checks each movement's line and checks the
whole document once, avoiding quadratic commit work. The 1,001-unit local
regression completed in 9.44 seconds; this is not a production throughput/SLO
guarantee, so benchmark under the intended deployment load before setting
production batch-size and latency targets.
