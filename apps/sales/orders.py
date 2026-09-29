"""Nonfinancial orders and explicit delivery evidence, with atomic invoice conversion."""

import uuid
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, cast

from django.db import transaction

from apps.payments.services import _claim, _context, _json, _rows, _run
from apps.sales.return_services import _effect, _finish, _one
from apps.sales.services import create_sales_invoice
from common.access.scopes import CompanyScope, assert_company_write, bind_and_verify_company
from common.api.errors import Conflict, PreconditionFailed


def order_detail(company: uuid.UUID, order: uuid.UUID) -> dict[str, Any]:
    result = _one("SELECT * FROM erp.sales_orders WHERE company_id=%s AND id=%s", [company, order])
    result["lines"] = _rows(
        "SELECT * FROM erp.sales_order_lines WHERE company_id=%s AND sales_order_id=%s "
        "ORDER BY line_no",
        [company, order],
    )
    result["invoices"] = _rows(
        "SELECT id,invoice_no,status,row_version FROM erp.sales_invoices WHERE company_id=%s "
        "AND sales_order_id=%s AND document_kind='invoice' ORDER BY id",
        [company, order],
    )
    return cast(dict[str, Any], _json(result))


def catalog(
    company: uuid.UUID, kind: str, after: uuid.UUID | None, limit: int
) -> list[dict[str, Any]]:
    table = {"orders": "sales_orders", "channels": "sales_channels"}[kind]
    return cast(
        list[dict[str, Any]],
        _json(
            _rows(
                f"SELECT * FROM erp.{table} WHERE company_id=%s "
                "AND (%s::uuid IS NULL OR id>%s) ORDER BY id LIMIT %s",
                [company, after, after, limit],
            )
        ),
    )


def save_channel(
    scope: CompanyScope,
    data: dict[str, Any],
    *,
    channel: uuid.UUID | None,
    revision: int | None,
    key: str,
    request_id: str | None,
) -> dict[str, Any]:
    with transaction.atomic(durable=True):
        bind_and_verify_company(scope)
        receipt, replay = _claim(
            scope,
            "sales.channel.save",
            str(channel or data["code"]),
            key,
            data | {"revision": revision},
        )
        assert_company_write(scope.company_id, "sales", "sales.order.manage")
        if replay is not None:
            return replay
        _context(request_id)
        if channel is None:
            row = _one(
                "INSERT INTO erp.sales_channels(company_id,code,name,channel_kind,is_active) "
                "VALUES (%s,%s,%s,%s,%s) RETURNING id",
                [
                    scope.company_id,
                    data["code"],
                    data["name"],
                    data["channel_kind"],
                    data["is_active"],
                ],
            )
            channel = row["id"]
        else:
            row = _one(
                "SELECT * FROM erp.sales_channels WHERE company_id=%s AND id=%s FOR UPDATE",
                [scope.company_id, channel],
            )
            if row["row_version"] != revision:
                raise PreconditionFailed(row["row_version"])
            fields = [f for f in ("code", "name", "channel_kind", "is_active") if f in data]
            _run(
                f"UPDATE erp.sales_channels SET {','.join(f'{f}=%s' for f in fields)} "
                "WHERE company_id=%s AND id=%s",
                [*(data[f] for f in fields), scope.company_id, channel],
            )
        result = cast(
            dict[str, Any],
            _json(
                _one(
                    "SELECT * FROM erp.sales_channels WHERE company_id=%s AND id=%s",
                    [scope.company_id, channel],
                )
            ),
        )
        _effect(
            scope,
            channel,
            f"sales.channel.saved.{result['row_version']}",
            aggregate_type="sales_channel",
        )
        _finish(receipt, result, 201 if revision is None else 200, result_type="sales_channel")
        return result


def save_order(
    scope: CompanyScope,
    data: dict[str, Any],
    *,
    order: uuid.UUID | None,
    revision: int | None,
    key: str,
    request_id: str | None,
) -> dict[str, Any]:
    with transaction.atomic(durable=True):
        bind_and_verify_company(scope)
        receipt, replay = _claim(
            scope,
            "sales.order.save",
            str(order or data["order_no"]),
            key,
            data | {"revision": revision},
        )
        assert_company_write(scope.company_id, "sales", "sales.order.manage")
        if replay is not None:
            return replay
        _context(request_id)
        fields = (
            "order_no",
            "partner_id",
            "channel_id",
            "order_date",
            "currency_code",
            "external_ref",
        )
        if order is None:
            row = _one(
                "INSERT INTO erp.sales_orders(company_id,order_no,partner_id,channel_id,"
                "order_date,currency_code,external_ref) VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                [scope.company_id, *(data.get(f) for f in fields)],
            )
            order = row["id"]
        else:
            row = _one(
                "SELECT * FROM erp.sales_orders WHERE company_id=%s AND id=%s FOR UPDATE",
                [scope.company_id, order],
            )
            if row["row_version"] != revision:
                raise PreconditionFailed(row["row_version"])
            if row["status"] != "draft":
                raise Conflict("ORDER_NOT_DRAFT", "Only draft orders can change.")
            updates = [f for f in fields if f in data]
            assignments = ",".join(f"{f}=%s" for f in updates) or "row_version=row_version"
            _run(
                f"UPDATE erp.sales_orders SET {assignments} WHERE company_id=%s AND id=%s",
                [*(data[f] for f in updates), scope.company_id, order],
            )
            if "lines" in data:
                _run(
                    "DELETE FROM erp.sales_order_lines WHERE company_id=%s AND sales_order_id=%s",
                    [scope.company_id, order],
                )
        for number, line in enumerate(data.get("lines", []), 1):
            _run(
                "INSERT INTO erp.sales_order_lines(company_id,sales_order_id,line_no,item_id,"
                "description,ordered_quantity,unit_price,discount_percent,tax_code_id) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                [
                    scope.company_id,
                    order,
                    number,
                    line["item_id"],
                    line["description"],
                    line["quantity"],
                    line["unit_price"],
                    line["discount_percent"],
                    line.get("tax_code_id"),
                ],
            )
        result = order_detail(scope.company_id, order)
        _effect(
            scope, order, f"sales.order.saved.{result['row_version']}", aggregate_type="sales_order"
        )
        _finish(receipt, result, 201 if revision is None else 200, result_type="sales_order")
        return result


def command_order(
    scope: CompanyScope,
    order: uuid.UUID,
    data: dict[str, Any],
    *,
    revision: int,
    key: str,
    request_id: str | None,
) -> dict[str, Any]:
    with transaction.atomic(durable=True):
        bind_and_verify_company(scope)
        receipt, replay = _claim(
            scope, "sales.order.command", str(order), key, data | {"revision": revision}
        )
        assert_company_write(scope.company_id, "sales", "sales.order.manage")
        if replay is not None:
            return replay
        _context(request_id)
        row = _one(
            "SELECT * FROM erp.sales_orders WHERE company_id=%s AND id=%s FOR UPDATE",
            [scope.company_id, order],
        )
        if row["row_version"] != revision:
            raise PreconditionFailed(row["row_version"])
        if data["action"] == "invoice":
            if row["status"] != "confirmed":
                raise Conflict("ORDER_NOT_CONFIRMED", "Confirm the order before invoicing.")
            if _rows(
                "SELECT id FROM erp.sales_invoices WHERE company_id=%s AND sales_order_id=%s "
                "AND document_kind='invoice' AND status<>'void'",
                [scope.company_id, order],
            ):
                raise Conflict("ORDER_ALREADY_INVOICED", "Order already has an active invoice.")
            minor = _one(
                "SELECT minor_units FROM erp.currencies WHERE code=%s", [row["currency_code"]]
            )
            quantum = Decimal(1).scaleb(-minor["minor_units"])
            lines = _rows(
                "SELECT * FROM erp.sales_order_lines WHERE company_id=%s AND sales_order_id=%s "
                "ORDER BY line_no",
                [scope.company_id, order],
            )
            invoice = create_sales_invoice(
                scope,
                {
                    "invoice_no": data["invoice_no"],
                    "partner_id": row["partner_id"],
                    "issue_date": data["issue_date"],
                    "tax_point_date": data.get("tax_point_date", data["issue_date"]),
                    "currency_code": row["currency_code"],
                    "exchange_rate": data["exchange_rate"],
                    "tax_jurisdiction_id": data.get("tax_jurisdiction_id"),
                    "due_date": data.get("due_date"),
                    "addresses": data.get("addresses", []),
                    "lines": [
                        {
                            "item_id": line["item_id"],
                            "description": line["description"],
                            "quantity": line["ordered_quantity"],
                            "unit_price": line["unit_price"],
                            "tax_code_id": line["tax_code_id"],
                            "discount_amount": (
                                line["ordered_quantity"]
                                * line["unit_price"]
                                * line["discount_percent"]
                                / 100
                            ).quantize(quantum, rounding=ROUND_HALF_UP),
                        }
                        for line in lines
                    ],
                },
                request_id=request_id,
                _nested=True,
            )
            _run(
                "UPDATE erp.sales_invoices SET sales_order_id=%s WHERE company_id=%s AND id=%s",
                [order, scope.company_id, invoice["id"]],
            )
        else:
            _run(
                "UPDATE erp.sales_orders SET status=%s WHERE company_id=%s AND id=%s",
                [
                    {"confirm": "confirmed", "cancel": "cancelled"}[data["action"]],
                    scope.company_id,
                    order,
                ],
            )
        result = order_detail(scope.company_id, order)
        _effect(
            scope,
            order,
            f"sales.order.{data['action']}.{result['row_version']}",
            aggregate_type="sales_order",
        )
        _finish(receipt, result, result_type="sales_order")
        return result


def confirm_delivery(
    scope: CompanyScope,
    invoice: uuid.UUID,
    data: dict[str, Any],
    *,
    revision: int,
    key: str,
    request_id: str | None,
) -> dict[str, Any]:
    with transaction.atomic(durable=True):
        bind_and_verify_company(scope)
        receipt, replay = _claim(
            scope, "sales.delivery.confirm", str(invoice), key, data | {"revision": revision}
        )
        assert_company_write(scope.company_id, "sales", "sales.delivery.confirm")
        if replay is not None:
            return replay
        _context(request_id)
        preliminary = _one(
            "SELECT sales_order_id FROM erp.sales_invoices WHERE company_id=%s AND id=%s",
            [scope.company_id, invoice],
        )
        if preliminary["sales_order_id"]:
            _one(
                "SELECT id FROM erp.sales_orders WHERE company_id=%s AND id=%s FOR UPDATE",
                [scope.company_id, preliminary["sales_order_id"]],
            )
        row = _one(
            "SELECT row_version FROM erp.sales_invoices WHERE company_id=%s AND id=%s FOR UPDATE",
            [scope.company_id, invoice],
        )
        if row["row_version"] != revision:
            raise PreconditionFailed(row["row_version"])
        delivery = _one(
            "INSERT INTO erp.sales_deliveries(company_id,sales_invoice_id,delivered_date,"
            "delivery_reference,received_by,notes,confirmed_by) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING *",
            [
                scope.company_id,
                invoice,
                data["delivered_date"],
                data["delivery_reference"],
                data["received_by"],
                data["notes"],
                scope.user_id,
            ],
        )
        if preliminary["sales_order_id"]:
            _run(
                "UPDATE erp.sales_orders SET status='fulfilled' WHERE company_id=%s AND id=%s",
                [scope.company_id, preliminary["sales_order_id"]],
            )
        result = cast(dict[str, Any], _json(delivery))
        _effect(scope, delivery["id"], "sales.delivery.confirmed", aggregate_type="sales_delivery")
        _finish(receipt, result, result_type="sales_delivery")
        return result
