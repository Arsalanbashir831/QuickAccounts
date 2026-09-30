"""Typed stock documents and reservations; no generic movement write API."""

import uuid
from decimal import Decimal as D
from typing import Any, cast

from django.db import transaction

from apps.inventory.cost_basis import locked_cost_policy, record_cost_basis
from apps.inventory.cost_layers import locked_layers, record_layer_uses, remaining_layers
from apps.inventory.cost_reconciliation import cost_reconciliation
from apps.inventory.costing import CostingError, scope_average_issue
from apps.inventory.layer_costing import LayerBalance, LayerUse, allocate_average_issue
from apps.payments.services import (
    _account,
    _claim,
    _context,
    _json,
    _line,
    _money,
    _posting_period,
    _rows,
    _run,
)
from apps.sales.return_services import _effect, _finish, _journal, _one
from common.access.scopes import (
    CompanyScope,
    assert_company_write,
    bind_and_verify_company,
    resolve_product_id,
)
from common.api.errors import Conflict, PreconditionFailed


def document_detail(company: uuid.UUID, document_id: uuid.UUID) -> dict[str, Any]:
    result = _one(
        "SELECT * FROM erp.inventory_documents WHERE company_id=%s AND id=%s",
        [company, document_id],
    )
    result["lines"] = _rows(
        "SELECT * FROM erp.inventory_document_lines WHERE company_id=%s "
        "AND inventory_document_id=%s ORDER BY line_no",
        [company, document_id],
    )
    result["cost_basis"] = _rows(
        "SELECT b.* FROM erp.inventory_cost_basis_snapshots b "
        "JOIN erp.stock_movements m ON m.company_id=b.company_id AND m.id=b.movement_id "
        "JOIN erp.inventory_document_lines l ON l.company_id=m.company_id "
        "AND l.id=m.inventory_document_line_id WHERE l.company_id=%s "
        "AND l.inventory_document_id=%s ORDER BY l.line_no,b.id",
        [company, document_id],
    )
    for snapshot in result["cost_basis"]:
        snapshot.pop("allocation_seal_xid", None)
    result["cost_allocations"] = _rows(
        "SELECT a.* FROM erp.inventory_cost_allocations a JOIN erp.stock_movements m "
        "ON m.company_id=a.company_id AND m.id=a.issue_movement_id "
        "JOIN erp.inventory_document_lines l ON l.company_id=m.company_id "
        "AND l.id=m.inventory_document_line_id WHERE l.company_id=%s "
        "AND l.inventory_document_id=%s ORDER BY l.line_no,a.cost_layer_id",
        [company, document_id],
    )
    return cast(dict[str, Any], _json(result))


def rebuild_positions(
    scope: CompanyScope, *, key: str, request_id: str | None = None
) -> dict[str, Any]:
    with transaction.atomic(durable=True):
        bind_and_verify_company(scope)
        receipt, replay = _claim(scope, "inventory.rebuild", str(scope.company_id), key, {})
        assert_company_write(scope.company_id, "inventory", "inventory.reconcile")
        if replay is not None:
            return replay
        _context(request_id)
        count = _one(
            "SELECT erp.rebuild_inventory_positions(%s,%s) n",
            [scope.company_id, resolve_product_id()],
        )["n"]
        # The rebuild's exclusive maintenance/table/position locks last until commit.
        # Validate under those locks; never "repair" immutable layers or allocations.
        verified = cost_reconciliation(scope.company_id)
        if not verified["cost_history_matches"] or not verified["projection_matches"]:
            raise Conflict(
                "INVENTORY_REBUILD_INTEGRITY_FAILED",
                "Rebuilt positions or immutable cost history do not reconcile.",
            )
        result = {
            "id": str(receipt),
            "scope_count": count,
            "projection_verified": True,
            "cost_history_verified": True,
            "adopted_scope_count": verified["adopted_scope_count"],
            "unadopted_scope_count": verified["unadopted_scope_count"],
        }
        _effect(scope, receipt, "inventory.rebuilt", aggregate_type="inventory_rebuild")
        _finish(receipt, result, result_type="inventory_rebuild")
        return result


def void_document(
    scope: CompanyScope,
    document_id: uuid.UUID,
    *,
    revision: int,
    key: str,
    request_id: str | None = None,
) -> dict[str, Any]:
    with transaction.atomic(durable=True):
        bind_and_verify_company(scope)
        receipt, replay = _claim(
            scope, "inventory.document.void", str(document_id), key, {"revision": revision}
        )
        assert_company_write(scope.company_id, "inventory", "inventory.manage")
        if replay is not None:
            return replay
        _context(request_id)
        _draft(scope.company_id, document_id, revision)
        _run(
            "UPDATE erp.inventory_reservations r SET status='released' "
            "FROM erp.inventory_document_lines l WHERE l.company_id=%s "
            "AND l.inventory_document_id=%s AND r.company_id=l.company_id "
            "AND r.source_type='inventory_document_line' AND r.source_id=l.id "
            "AND r.status='active'",
            [scope.company_id, document_id],
        )
        _run(
            "UPDATE erp.inventory_documents SET status='void',row_version=row_version+1 "
            "WHERE company_id=%s AND id=%s",
            [scope.company_id, document_id],
        )
        _effect(
            scope, document_id, "inventory.document.voided", aggregate_type="inventory_document"
        )
        result = document_detail(scope.company_id, document_id)
        _finish(receipt, result, result_type="inventory_document")
        return result


def create_lot(
    scope: CompanyScope, data: dict[str, Any], *, key: str, request_id: str | None = None
) -> dict[str, Any]:
    with transaction.atomic(durable=True):
        bind_and_verify_company(scope)
        receipt, replay = _claim(scope, "inventory.lot.create", "new", key, data)
        assert_company_write(scope.company_id, "inventory", "inventory.manage")
        if replay is not None:
            return replay
        _context(request_id)
        item = _one(
            "SELECT * FROM erp.items WHERE company_id=%s AND id=%s FOR SHARE",
            [scope.company_id, data["item_id"]],
        )
        if (
            not item["is_active"]
            or item["item_kind"] != "stock"
            or not (item["track_lots"] or item["track_serials"])
        ):
            raise Conflict(
                "STOCK_TRACKED_ITEM_REQUIRED", "Lots require an active tracked stock item."
            )
        if bool(data.get("serial_code")) != item["track_serials"]:
            raise Conflict("STOCK_SERIAL_REQUIRED", "Serial selection must match item tracking.")
        if (
            data.get("expires_on")
            and data.get("received_on")
            and data["expires_on"] < data["received_on"]
        ):
            raise Conflict("STOCK_LOT_DATES_INVALID", "Expiry precedes receipt.")
        result = _one(
            "INSERT INTO erp.inventory_lots(company_id,item_id,lot_code,serial_code,"
            "received_on,expires_on) VALUES (%s,%s,%s,%s,%s,%s) RETURNING *",
            [
                scope.company_id,
                data["item_id"],
                data["lot_code"],
                data.get("serial_code"),
                data.get("received_on"),
                data.get("expires_on"),
            ],
        )
        _effect(scope, result["id"], "inventory.lot.created", aggregate_type="inventory_lot")
        result = _json(result)
        _finish(receipt, result, result_type="inventory_lot")
    return cast(dict[str, Any], result)


def create_serial_lots_bulk(
    scope: CompanyScope,
    item_id: uuid.UUID,
    serial_numbers: list[str],
    *,
    key: str,
    request_id: str | None = None,
) -> dict[str, Any]:
    if not 1 <= len(serial_numbers) <= 2000:
        raise Conflict("SERIAL_BATCH_SIZE", "Supply between 1 and 2,000 serials.")
    cleaned = [number.strip() for number in serial_numbers]
    if any(not 1 <= len(number) <= 100 for number in cleaned):
        raise Conflict("INVALID_SERIAL", "Each serial must contain 1–100 characters.")
    if len({number.upper() for number in cleaned}) != len(cleaned):
        raise Conflict("DUPLICATE_SERIAL", "Serials must be unique within the batch.")
    payload = {"item_id": str(item_id), "serial_numbers": cleaned}
    with transaction.atomic(durable=True):
        bind_and_verify_company(scope)
        receipt, replay = _claim(scope, "inventory.serial_lots.bulk", str(item_id), key, payload)
        assert_company_write(scope.company_id, "inventory", "inventory.manage")
        if replay is not None:
            return replay
        _context(request_id)
        item = _one(
            "SELECT id,track_serials,item_kind,is_active FROM erp.items "
            "WHERE company_id=%s AND id=%s FOR SHARE",
            [scope.company_id, item_id],
        )
        if not item["track_serials"] or item["item_kind"] != "stock" or not item["is_active"]:
            raise Conflict(
                "SERIAL_ITEM_REQUIRED", "An active serial-tracked stock item is required."
            )
        rows = _rows(
            "INSERT INTO erp.inventory_lots(company_id,item_id,lot_code,serial_code) "
            "SELECT %s,%s,x.serial_number,x.serial_number "
            "FROM unnest(%s::text[]) WITH ORDINALITY x(serial_number,ordinal) "
            "ORDER BY x.ordinal RETURNING id,serial_code",
            [scope.company_id, item_id, cleaned],
        )
        result = {
            "id": str(item_id),
            "item_id": str(item_id),
            "count": len(rows),
            "lots": _json(rows),
        }
        _effect(
            scope, item_id, f"inventory.serial_lots.created.{receipt}", aggregate_type="item"
        )
        _finish(receipt, result, result_type="inventory_serial_lots")
        return result


def _draft(company: uuid.UUID, document_id: uuid.UUID, revision: int) -> dict[str, Any]:
    result = _one(
        "SELECT * FROM erp.inventory_documents WHERE company_id=%s AND id=%s FOR UPDATE",
        [company, document_id],
    )
    if result["row_version"] != revision:
        raise PreconditionFailed(result["row_version"])
    if result["status"] != "draft":
        raise Conflict("STOCK_DOCUMENT_IMMUTABLE", "Only draft stock documents can change.")
    return result


def _item(
    company: uuid.UUID, item_id: uuid.UUID, lot_id: uuid.UUID | None, quantity: D
) -> dict[str, Any]:
    item = _one(
        "SELECT * FROM erp.items WHERE company_id=%s AND id=%s FOR SHARE", [company, item_id]
    )
    if not item["is_active"] or item["item_kind"] != "stock":
        raise Conflict("STOCK_ITEM_REQUIRED", "An active stock item is required.")
    if bool(lot_id) != bool(item["track_lots"] or item["track_serials"]):
        raise Conflict("STOCK_LOT_REQUIRED", "Lot selection must match item tracking.")
    if lot_id:
        lot = _one(
            "SELECT * FROM erp.inventory_lots WHERE company_id=%s AND item_id=%s AND id=%s",
            [company, item_id, lot_id],
        )
        if item["track_serials"] and (quantity != 1 or not lot["serial_code"]):
            raise Conflict("STOCK_SERIAL_REQUIRED", "Serial-tracked lines require one serial unit.")
    return item


def save_document(
    scope: CompanyScope,
    data: dict[str, Any],
    *,
    key: str,
    document_id: uuid.UUID | None = None,
    revision: int | None = None,
    request_id: str | None = None,
    _nested: bool = False,
) -> dict[str, Any]:
    with transaction.atomic(durable=not _nested):
        bind_and_verify_company(scope)
        receipt, replay = _claim(
            scope,
            "inventory.document.save",
            str(document_id or "new"),
            key,
            data | {"revision": revision},
        )
        assert_company_write(scope.company_id, "inventory", "inventory.manage")
        if replay is not None:
            return replay
        _context(request_id)
        if document_id is None:
            document_id = uuid.uuid4()
            _run(
                "INSERT INTO erp.inventory_documents(id,company_id,document_no,document_kind,"
                "document_date,reason) VALUES (%s,%s,%s,%s,%s,%s)",
                [
                    document_id,
                    scope.company_id,
                    data["document_no"],
                    data["document_kind"],
                    data["document_date"],
                    data.get("reason", ""),
                ],
            )
        else:
            assert revision is not None
            _draft(scope.company_id, document_id, revision)
            if _rows(
                "SELECT 1 FROM erp.inventory_reservations r "
                "JOIN erp.inventory_document_lines l ON l.company_id=r.company_id "
                "AND l.id=r.source_id WHERE l.company_id=%s AND l.inventory_document_id=%s "
                "AND r.source_type='inventory_document_line' LIMIT 1",
                [scope.company_id, document_id],
            ):
                raise Conflict("STOCK_RESERVATION_HISTORY", "Reserved lines cannot be replaced.")
            for field in ("document_no", "document_date", "reason"):
                if field in data:
                    _run(
                        f"UPDATE erp.inventory_documents SET {field}=%s "
                        "WHERE company_id=%s AND id=%s",
                        [data[field], scope.company_id, document_id],
                    )
            _run(
                "UPDATE erp.inventory_documents SET row_version=row_version+1 "
                "WHERE company_id=%s AND id=%s",
                [scope.company_id, document_id],
            )
        if "lines" in data:
            _run(
                "DELETE FROM erp.inventory_document_lines WHERE company_id=%s "
                "AND inventory_document_id=%s",
                [scope.company_id, document_id],
            )
            kind = _one(
                "SELECT document_kind FROM erp.inventory_documents WHERE company_id=%s AND id=%s",
                [scope.company_id, document_id],
            )["document_kind"]
            for number, line in enumerate(data["lines"], 1):
                _item(scope.company_id, line["item_id"], line.get("lot_id"), line["quantity"])
                src, dst = line.get("from_warehouse_id"), line.get("to_warehouse_id")
                valid = (
                    bool(dst) and not src
                    if kind in {"receipt", "adjustment_in"}
                    else bool(src) and not dst
                    if kind in {"shipment", "adjustment_out"}
                    else bool(src) and bool(dst) and src != dst
                )
                if not valid:
                    raise Conflict(
                        "STOCK_LINE_LOCATION_INVALID", "Locations do not match document kind."
                    )
                if kind in {"receipt", "adjustment_in"} and "unit_cost_company" not in line:
                    raise Conflict(
                        "STOCK_RECEIPT_COST_REQUIRED", "Supply an explicit incoming unit cost."
                    )
                if bool(line.get("sales_invoice_line_id")) != (kind == "shipment"):
                    raise Conflict(
                        "STOCK_SHIPMENT_SOURCE_REQUIRED", "Shipments require invoice lines only."
                    )
                for warehouse in sorted({w for w in (src, dst) if w}, key=str):
                    _one(
                        "SELECT id FROM erp.warehouses WHERE company_id=%s AND id=%s "
                        "AND is_active FOR SHARE",
                        [scope.company_id, warehouse],
                    )
                _run(
                    "INSERT INTO erp.inventory_document_lines(company_id,inventory_document_id,"
                    "line_no,item_id,lot_id,from_warehouse_id,to_warehouse_id,quantity,"
                    "unit_cost_company,sales_invoice_line_id) VALUES "
                    "(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    [
                        scope.company_id,
                        document_id,
                        number,
                        line["item_id"],
                        line.get("lot_id"),
                        src,
                        dst,
                        line["quantity"],
                        line.get("unit_cost_company", D(0)),
                        line.get("sales_invoice_line_id"),
                    ],
                )
        _run(
            "INSERT INTO erp.outbox_events(company_id,event_key,aggregate_type,aggregate_id,"
            "event_type,payload) VALUES (%s,%s,'inventory_document',%s,"
            "'inventory.document.saved',jsonb_build_object('id',%s::text))",
            [
                scope.company_id,
                f"inventory.document.saved:{document_id}:{receipt}",
                document_id,
                document_id,
            ],
        )
        result = document_detail(scope.company_id, document_id)
        _finish(receipt, result, result_type="inventory_document")
        return result


def _serial_reservation_event(
    company_id: uuid.UUID, reservation: dict[str, Any], action: str
) -> None:
    if reservation["lot_id"] is None:
        return
    _run(
        "INSERT INTO erp.serial_movements(company_id,serial_id,movement_kind,"
        "warehouse_id,quantity_delta,source_type,source_id,source_line_id,occurred_at,"
        "actor_user_id) SELECT %s,s.id,%s,%s,0,'inventory_reservation',%s,%s,"
        "clock_timestamp(),identity.current_user_id() FROM erp.inventory_serials s "
        "WHERE s.company_id=%s AND s.lot_id=%s",
        [
            company_id,
            action,
            reservation["warehouse_id"],
            reservation["id"],
            reservation["source_id"],
            company_id,
            reservation["lot_id"],
        ],
    )


def reserve_stock(
    scope: CompanyScope, data: dict[str, Any], *, key: str, request_id: str | None = None
) -> dict[str, Any]:
    with transaction.atomic(durable=True):
        bind_and_verify_company(scope)
        receipt, replay = _claim(scope, "inventory.reserve", str(data["line_id"]), key, data)
        assert_company_write(scope.company_id, "inventory", "inventory.reserve")
        if replay is not None:
            return replay
        _context(request_id)
        line = _one(
            "SELECT * FROM erp.inventory_document_lines WHERE company_id=%s AND id=%s",
            [scope.company_id, data["line_id"]],
        )
        _draft(scope.company_id, line["inventory_document_id"], data["document_revision"])
        line = _one(
            "SELECT * FROM erp.inventory_document_lines WHERE company_id=%s AND id=%s",
            [scope.company_id, data["line_id"]],
        )
        if not line["from_warehouse_id"]:
            raise Conflict(
                "STOCK_RESERVATION_SOURCE_INVALID", "Inbound lines cannot reserve stock."
            )
        _item(scope.company_id, line["item_id"], line["lot_id"], data["quantity"])
        _position(scope.company_id, line["from_warehouse_id"], line["item_id"], line["lot_id"])
        reservation = _one(
            "INSERT INTO erp.inventory_reservations(company_id,warehouse_id,item_id,lot_id,"
            "reservation_key,source_type,source_id,quantity) VALUES (%s,%s,%s,%s,%s,"
            "'inventory_document_line',%s,%s) RETURNING *",
            [
                scope.company_id,
                line["from_warehouse_id"],
                line["item_id"],
                line["lot_id"],
                data["reservation_key"],
                line["id"],
                data["quantity"],
            ],
        )
        _serial_reservation_event(scope.company_id, reservation, "reserved")
        _effect(
            scope, reservation["id"], "inventory.reserved", aggregate_type="inventory_reservation"
        )
        result = cast(dict[str, Any], _json(reservation))
        _finish(receipt, result, result_type="inventory_reservation")
        return result


def release_stock(
    scope: CompanyScope,
    reservation_id: uuid.UUID,
    *,
    revision: int,
    key: str,
    request_id: str | None = None,
) -> dict[str, Any]:
    with transaction.atomic(durable=True):
        bind_and_verify_company(scope)
        receipt, replay = _claim(
            scope, "inventory.release", str(reservation_id), key, {"revision": revision}
        )
        assert_company_write(scope.company_id, "inventory", "inventory.reserve")
        if replay is not None:
            return replay
        _context(request_id)
        reservation = _one(
            "SELECT * FROM erp.inventory_reservations WHERE company_id=%s AND id=%s",
            [scope.company_id, reservation_id],
        )
        if reservation["source_type"] != "inventory_document_line":
            raise Conflict("STOCK_RESERVATION_SOURCE_INVALID", "Unsupported reservation source.")
        line = _one(
            "SELECT inventory_document_id FROM erp.inventory_document_lines "
            "WHERE company_id=%s AND id=%s",
            [scope.company_id, reservation["source_id"]],
        )
        _one(
            "SELECT id FROM erp.inventory_documents WHERE company_id=%s AND id=%s FOR UPDATE",
            [scope.company_id, line["inventory_document_id"]],
        )
        reservation = _one(
            "SELECT * FROM erp.inventory_reservations WHERE company_id=%s AND id=%s FOR UPDATE",
            [scope.company_id, reservation_id],
        )
        if reservation["row_version"] != revision:
            raise PreconditionFailed(reservation["row_version"])
        if reservation["status"] != "active":
            raise Conflict("STOCK_RESERVATION_IMMUTABLE", "Reservation is already final.")
        _position(
            scope.company_id,
            reservation["warehouse_id"],
            reservation["item_id"],
            reservation["lot_id"],
        )
        result = _one(
            "UPDATE erp.inventory_reservations SET status='released' "
            "WHERE company_id=%s AND id=%s RETURNING *",
            [scope.company_id, reservation_id],
        )
        _serial_reservation_event(scope.company_id, reservation, "reservation_released")
        _effect(scope, reservation_id, "inventory.released", aggregate_type="inventory_reservation")
        result = cast(dict[str, Any], _json(result))
        _finish(receipt, result, result_type="inventory_reservation")
        return result


def _position(
    company: uuid.UUID, warehouse: uuid.UUID, item: uuid.UUID, lot: uuid.UUID | None
) -> dict[str, Any]:
    return _one(
        "SELECT * FROM erp.inventory_positions WHERE company_id=%s AND warehouse_id=%s "
        "AND item_id=%s AND lot_id IS NOT DISTINCT FROM %s::uuid FOR UPDATE",
        [company, warehouse, item, lot],
    )


def post_document(
    scope: CompanyScope,
    document_id: uuid.UUID,
    data: dict[str, Any],
    *,
    revision: int,
    key: str,
    request_id: str | None = None,
    _nested: bool = False,
) -> dict[str, Any]:
    with transaction.atomic(durable=not _nested):
        bind_and_verify_company(scope)
        receipt, replay = _claim(
            scope, "inventory.document.post", str(document_id), key, data | {"revision": revision}
        )
        assert_company_write(scope.company_id, "inventory", "inventory.post")
        if replay is not None:
            return replay
        _context(request_id)
        # Invoice locks precede stock-document and position locks for linked shipments.
        invoice_ids = _rows(
            "SELECT DISTINCT s.sales_invoice_id FROM erp.inventory_document_lines l "
            "JOIN erp.sales_invoice_lines s ON s.company_id=l.company_id "
            "AND s.id=l.sales_invoice_line_id WHERE l.company_id=%s "
            "AND l.inventory_document_id=%s ORDER BY s.sales_invoice_id",
            [scope.company_id, document_id],
        )
        for invoice in invoice_ids:
            _one(
                "SELECT id FROM erp.sales_invoices WHERE company_id=%s AND id=%s FOR UPDATE",
                [scope.company_id, invoice["sales_invoice_id"]],
            )
        document = _draft(scope.company_id, document_id, revision)
        lines = _rows(
            "SELECT * FROM erp.inventory_document_lines WHERE company_id=%s "
            "AND inventory_document_id=%s ORDER BY line_no FOR UPDATE",
            [scope.company_id, document_id],
        )
        if not lines:
            raise Conflict("STOCK_DOCUMENT_EMPTY", "Stock documents require lines.")
        kind = document["document_kind"]
        policy = (
            locked_cost_policy(scope)
            if kind in {"shipment", "transfer", "adjustment_out"}
            else None
        )
        _posting_period(
            scope.company_id,
            data["fiscal_period_id"],
            data["journal_id"],
            document["document_date"],
        )
        facts = _one(
            "SELECT c.functional_currency AS currency_code,fc.minor_units AS precision "
            "FROM erp.companies c JOIN erp.currencies fc ON fc.code=c.functional_currency "
            "WHERE c.id=%s",
            [scope.company_id],
        ) | {"exchange_rate": D(1), "partner_id": None}
        if kind == "adjustment_out":
            assert_company_write(scope.company_id, "inventory", "inventory.write_off")
            if not data.get("approve_loss") or not document["reason"].strip():
                raise Conflict(
                    "STOCK_LOSS_APPROVAL_REQUIRED", "Losses require approval and a reason."
                )
        if kind in {"receipt", "adjustment_in", "adjustment_out"}:
            if not data.get("offset_account_id"):
                raise Conflict("STOCK_OFFSET_ACCOUNT_REQUIRED", "An offset account is required.")
            _account(
                scope.company_id,
                data["offset_account_id"],
                {"expense", "cost_of_sales"}
                if kind == "adjustment_out"
                else {"asset", "liability", "equity", "expense", "cost_of_sales"},
            )
        warehouses = sorted(
            {w for row in lines for w in (row["from_warehouse_id"], row["to_warehouse_id"]) if w},
            key=str,
        )
        for warehouse in warehouses:
            w = _one(
                "SELECT * FROM erp.warehouses WHERE company_id=%s AND id=%s "
                "AND is_active FOR SHARE",
                [scope.company_id, warehouse],
            )
            if kind == "shipment" and w["stock_category"] != "sellable":
                raise Conflict("STOCK_NOT_SELLABLE", "Shipments require sellable stock.")
        scopes = sorted(
            {
                (w, row["item_id"], row["lot_id"])
                for row in lines
                for w in (row["from_warehouse_id"], row["to_warehouse_id"])
                if w
            },
            key=lambda s: tuple(str(v or "") for v in s),
        )
        # Share a maintenance lock with projection reconciliation; never truncate live positions.
        _run(
            "SELECT pg_advisory_xact_lock_shared(hashtextextended(%s,0))",
            [f"inventory-rebuild:{scope.company_id}"],
        )
        locked_missing_items: set[uuid.UUID] = set()
        for warehouse, item, lot in scopes:
            if not _rows(
                "SELECT id FROM erp.inventory_positions WHERE company_id=%s "
                "AND warehouse_id=%s AND item_id=%s AND lot_id IS NOT DISTINCT FROM %s::uuid",
                [scope.company_id, warehouse, item, lot],
            ):
                # Projection creation happens only through the ledger's definer trigger.
                # Inbound-only missing scopes are serialized by the item row.
                if item not in locked_missing_items:
                    _one(
                        "SELECT id FROM erp.items WHERE company_id=%s AND id=%s FOR UPDATE",
                        [scope.company_id, item],
                    )
                    locked_missing_items.add(item)
            else:
                _position(scope.company_id, warehouse, item, lot)
        costs: list[tuple[dict[str, Any], D, uuid.UUID, uuid.UUID | None]] = []
        running: dict[tuple[Any, ...], tuple[D, D]] = {}
        shipment_quantities: dict[uuid.UUID, D] = {}
        shipment_invoices: dict[uuid.UUID, uuid.UUID] = {}
        cost_basis: dict[uuid.UUID, dict[str, D]] = {}
        layer_balances: dict[tuple[Any, ...], list[LayerBalance] | None] = {}
        layer_uses: dict[uuid.UUID, list[LayerUse]] = {}
        profiles: dict[uuid.UUID, dict[str, Any]] = {}
        validated_assets: set[uuid.UUID] = set()
        for line in lines:
            _item(scope.company_id, line["item_id"], line["lot_id"], line["quantity"])
            if line["item_id"] not in profiles:
                profiles[line["item_id"]] = _one(
                    "SELECT * FROM erp.item_accounting_profiles WHERE company_id=%s "
                    "AND item_id=%s FOR SHARE",
                    [scope.company_id, line["item_id"]],
                )
            profile = profiles[line["item_id"]]
            inventory = profile["inventory_account_id"]
            if inventory not in validated_assets:
                _account(scope.company_id, inventory, {"asset"})
                validated_assets.add(inventory)
            offset = data.get("offset_account_id")
            if kind == "shipment":
                source = _one(
                    "SELECT s.*,i.status,i.document_kind,i.stock_fulfillment,i.issue_date "
                    "FROM erp.sales_invoice_lines s JOIN erp.sales_invoices i "
                    "ON i.company_id=s.company_id AND i.id=s.sales_invoice_id "
                    "WHERE s.company_id=%s AND s.id=%s",
                    [scope.company_id, line["sales_invoice_line_id"]],
                )
                if source["sales_invoice_id"] not in {
                    invoice["sales_invoice_id"] for invoice in invoice_ids
                }:
                    raise Conflict(
                        "STOCK_SOURCE_CHANGED", "Shipment source changed; retry the command."
                    )
                if (
                    source["status"] != "posted"
                    or source["document_kind"] != "invoice"
                    or source["stock_fulfillment"] != "deferred"
                    or source["item_id"] != line["item_id"]
                    or document["document_date"] < source["issue_date"]
                ):
                    raise Conflict(
                        "STOCK_SHIPMENT_SOURCE_INVALID", "Invoice is not eligible for shipment."
                    )
                shipped = _one(
                    "SELECT coalesce(sum(-quantity_delta),0) quantity "
                    "FROM erp.stock_movements WHERE company_id=%s "
                    "AND source_type='sales_invoice' AND source_line_id=%s",
                    [scope.company_id, source["id"]],
                )["quantity"]
                shipment_invoices[line["id"]] = source["sales_invoice_id"]
                shipment_quantities[source["id"]] = (
                    shipment_quantities.get(source["id"], D(0)) + line["quantity"]
                )
                if shipped + shipment_quantities[source["id"]] > source["quantity"]:
                    raise Conflict(
                        "STOCK_ALREADY_FULFILLED", "Shipment exceeds remaining invoice quantity."
                    )
                inventory, offset = (
                    source["inventory_account_id_snapshot"],
                    source["cogs_account_id_snapshot"],
                )
                _account(scope.company_id, inventory, {"asset"})
                _account(scope.company_id, offset, {"expense", "cost_of_sales"})
            value = _money(line["quantity"] * line["unit_cost_company"], 6)
            if line["from_warehouse_id"]:
                reservations = _rows(
                    "SELECT * FROM erp.inventory_reservations WHERE company_id=%s "
                    "AND source_type='inventory_document_line' AND source_id=%s "
                    "AND status='active' ORDER BY id FOR UPDATE",
                    [scope.company_id, line["id"]],
                )
                for reservation in reservations:
                    _run(
                        "UPDATE erp.inventory_reservations SET status='consumed' WHERE id=%s "
                        "AND company_id=%s",
                        [reservation["id"], scope.company_id],
                    )
                    _serial_reservation_event(scope.company_id, reservation, "reservation_consumed")
                scope_key = (line["from_warehouse_id"], line["item_id"], line["lot_id"])
                position = _position(scope.company_id, *scope_key)
                quantity, stock_value = running.get(
                    scope_key, (position["on_hand_quantity"], position["value_company"])
                )
                try:
                    calculated = scope_average_issue(
                        on_hand=quantity,
                        stock_value=stock_value,
                        reserved=position["reserved_quantity"],
                        quantity=line["quantity"],
                        currency_precision=facts["precision"],
                    )
                    if scope_key not in layer_balances:
                        layer_balances[scope_key] = locked_layers(scope.company_id, scope_key)
                    layers = layer_balances[scope_key]
                    if layers is not None:
                        uses = allocate_average_issue(layers, calculated)
                        layer_uses[line["id"]] = uses
                        layer_balances[scope_key] = remaining_layers(layers, uses)
                except CostingError as exc:
                    raise Conflict(exc.code, str(exc)) from exc
                value = calculated.value_company
                running[scope_key] = (
                    calculated.remaining_quantity,
                    calculated.remaining_value_company,
                )
                cost_basis[line["id"]] = {
                    "basis_quantity": quantity,
                    "basis_value_company": stock_value,
                    "reserved_quantity": position["reserved_quantity"],
                    "issue_quantity": calculated.quantity,
                    "issue_value_company": value,
                    "unit_cost_company": calculated.unit_cost_company,
                }
            if _money(value, facts["precision"]) != value:
                raise Conflict(
                    "STOCK_COST_ROUNDING_REQUIRED", "Cost requires a reviewed GL rounding policy."
                )
            costs.append((line, value, inventory, offset))
            _run(
                "UPDATE erp.inventory_document_lines SET value_company=%s,"
                "unit_cost_company=%s,inventory_account_id_snapshot=%s,"
                "offset_account_id_snapshot=%s WHERE company_id=%s AND id=%s",
                [
                    value,
                    _money(value / line["quantity"], 6),
                    inventory,
                    offset,
                    scope.company_id,
                    line["id"],
                ],
            )
        entry = None
        if kind != "transfer" and any(value > 0 for _, value, _, _ in costs):
            entry = _journal(
                scope, document_id, "inventory_document", document["document_date"], data, key
            )
            account_totals: dict[tuple[uuid.UUID, uuid.UUID], D] = {}
            for _unused_line, value, inventory, offset in costs:
                if not value:
                    continue
                assert offset is not None
                if offset == inventory:
                    raise Conflict(
                        "STOCK_OFFSET_INVALID", "Inventory and offset accounts must differ."
                    )
                pair = (inventory, offset)
                account_totals[pair] = account_totals.get(pair, D(0)) + value
            debit = kind in {"receipt", "adjustment_in"}
            for index, ((inventory, offset), value) in enumerate(
                sorted(account_totals.items(), key=lambda row: (str(row[0][0]), str(row[0][1])))
            ):
                number = index * 2 + 1
                _line(
                    scope.company_id,
                    entry,
                    number,
                    inventory,
                    value,
                    debit,
                    facts,
                    facts["precision"],
                )
                _line(
                    scope.company_id,
                    entry,
                    number + 1,
                    offset,
                    value,
                    not debit,
                    facts,
                    facts["precision"],
                )
            _run("SELECT erp.post_journal_entry(%s,%s)", [scope.company_id, entry])
        serialized_bulk_receipt = False
        if kind == "receipt" and len(costs) >= 100:
            serial_count = _one(
                "SELECT count(*) AS quantity FROM erp.inventory_document_lines l "
                "JOIN erp.items i ON i.company_id=l.company_id AND i.id=l.item_id "
                "WHERE l.company_id=%s AND l.inventory_document_id=%s AND i.track_serials",
                [scope.company_id, document_id],
            )["quantity"]
            serialized_bulk_receipt = serial_count == len(costs)
        if serialized_bulk_receipt:
            _run(
                "INSERT INTO erp.stock_movements(company_id,event_key,occurred_at,"
                "warehouse_id,item_id,lot_id,movement_kind,quantity_delta,"
                "unit_cost_company,value_delta_company,source_type,source_id,"
                "source_line_id,journal_entry_id,inventory_document_line_id) "
                "SELECT %s,'inventory:'||%s||':'||x.line_id::text||':1',%s,"
                "x.warehouse_id,x.item_id,x.lot_id,'receipt',1,x.value,"
                "x.value,'inventory_document',%s,x.line_id,%s,x.line_id "
                "FROM unnest(%s::uuid[],%s::uuid[],%s::uuid[],%s::uuid[],%s::numeric[]) "
                "x(line_id,warehouse_id,item_id,lot_id,value)",
                [
                    scope.company_id, str(document_id), document["document_date"],
                    document_id, entry,
                    [line["id"] for line, _, _, _ in costs],
                    [line["to_warehouse_id"] for line, _, _, _ in costs],
                    [line["item_id"] for line, _, _, _ in costs],
                    [line["lot_id"] for line, _, _, _ in costs],
                    [value for _, value, _, _ in costs],
                ],
            )
        for line, value, _, _ in costs:
            if serialized_bulk_receipt:
                continue
            for warehouse, sign in ((line["from_warehouse_id"], -1), (line["to_warehouse_id"], 1)):
                if warehouse:
                    movement_id = uuid.uuid4()
                    movement_unit_cost = (
                        cost_basis[line["id"]]["unit_cost_company"]
                        if sign == -1
                        else _money(value / line["quantity"], 6)
                    )
                    _run(
                        "INSERT INTO "
                        "erp.stock_movements(id,company_id,event_key,occurred_at,warehouse_id,"
                        "item_id,lot_id,movement_kind,quantity_delta,unit_cost_company,value_de"
                        "lta_company,"
                        "source_type,source_id,source_line_id,journal_entry_id,inventory_docume"
                        "nt_line_id) "
                        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                        [
                            movement_id,
                            scope.company_id,
                            f"inventory:{document_id}:{line['id']}:{sign}",
                            document["document_date"],
                            warehouse,
                            line["item_id"],
                            line["lot_id"],
                            "transfer"
                            if kind == "transfer"
                            else "receipt"
                            if sign == 1
                            else "issue",
                            sign * line["quantity"],
                            movement_unit_cost,
                            sign * value,
                            "sales_invoice" if kind == "shipment" else "inventory_document",
                            shipment_invoices[line["id"]] if kind == "shipment" else document_id,
                            line["sales_invoice_line_id"] if kind == "shipment" else line["id"],
                            entry,
                            line["id"],
                        ],
                    )
                    if sign == -1:
                        assert policy is not None
                        record_cost_basis(
                            scope,
                            movement_id,
                            policy["id"],
                            cost_basis[line["id"]],
                            currency_code=facts["currency_code"],
                            currency_precision=facts["precision"],
                        )
                        if line["id"] in layer_uses:
                            record_layer_uses(scope.company_id, movement_id, layer_uses[line["id"]])
        _run(
            "UPDATE erp.inventory_documents SET status='posted',journal_entry_id=%s,"
            "row_version=row_version+1 WHERE company_id=%s AND id=%s",
            [entry, scope.company_id, document_id],
        )
        _effect(
            scope, document_id, "inventory.document.posted", aggregate_type="inventory_document"
        )
        result = document_detail(scope.company_id, document_id)
        _finish(receipt, result, result_type="inventory_document")
        return result
