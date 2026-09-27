import uuid
from decimal import Decimal
from typing import Any, cast

from django.db import transaction

from apps.payments.services import (
    _account,
    _claim,
    _context,
    _json,
    _line,
    _money,
    _posting_period,
    _run,
)
from apps.sales.return_services import _effect, _finish, _journal, _locked, _one
from common.access.scopes import CompanyScope, assert_company_write, bind_and_verify_company
from common.api.errors import Conflict


def dispose_return_stock(
    scope: CompanyScope,
    return_id: uuid.UUID,
    data: dict[str, Any],
    *,
    revision: int,
    key: str,
    request_id: str | None,
) -> dict[str, Any]:
    with transaction.atomic(durable=True):
        bind_and_verify_company(scope)
        receipt, replay = _claim(
            scope, "sales.return.stock.dispose", str(return_id), key, data | {"revision": revision}
        )
        assert_company_write(scope.company_id, "inventory", "inventory.post")
        if replay is not None:
            return replay
        _context(request_id)
        _, source = _locked(scope, return_id, revision, posted=True)
        _posting_period(
            scope.company_id, data["fiscal_period_id"], data["journal_id"], data["action_date"]
        )
        line = _one(
            "SELECT l.*,s.item_id,s.inventory_account_id_snapshot FROM "
            "erp.sales_return_lines l JOIN erp.sales_invoice_lines s ON "
            "s.company_id=l.company_id AND s.id=l.sales_invoice_line_id WHERE "
            "l.company_id=%s AND l.sales_return_id=%s AND l.id=%s FOR UPDATE OF l",
            [scope.company_id, return_id, data["line_id"]],
        )
        original_from = data["from_warehouse_id"]
        destination = data.get("to_warehouse_id")
        warehouses = sorted({original_from} | ({destination} if destination else set()), key=str)
        for warehouse_id in warehouses:
            warehouse = _one(
                "SELECT * FROM erp.warehouses WHERE company_id=%s AND id=%s AND is_active "
                "FOR SHARE",
                [scope.company_id, warehouse_id],
            )
            if warehouse_id == original_from and warehouse["stock_category"] == "sellable":
                raise Conflict(
                    "RETURN_STOCK_TRACEABILITY_REQUIRED",
                    "Sellable stock may have been resold; disposition requires segregated "
                    "non-sellable stock.",
                )
        balances = _one(
            "SELECT coalesce(sum(CASE WHEN to_warehouse_id=%s THEN quantity WHEN "
            "from_warehouse_id=%s THEN -quantity ELSE 0 END),0) "
            "quantity,coalesce(sum(CASE WHEN to_warehouse_id=%s THEN historical_cost "
            "WHEN from_warehouse_id=%s THEN -historical_cost ELSE 0 END),0) cost FROM "
            "erp.sales_return_stock_actions WHERE company_id=%s AND "
            "sales_return_line_id=%s",
            [
                original_from,
                original_from,
                original_from,
                original_from,
                scope.company_id,
                line["id"],
            ],
        )
        quantity = balances["quantity"] + (
            line["quantity"] if line["warehouse_id"] == original_from else 0
        )
        value = balances["cost"] + (
            line["historical_cost"] if line["warehouse_id"] == original_from else 0
        )
        requested = data["quantity"]
        if (
            line["disposition"] == "write_off"
            or requested > quantity
            or requested <= 0
            or destination == original_from
        ):
            raise Conflict(
                "RETURN_STOCK_QUANTITY_EXCEEDED",
                "Disposition exceeds remaining segregated return stock.",
            )
        cost = value if requested == quantity else _money(value * requested / quantity, 6)
        if _money(cost, source["functional_precision"]) != cost:
            raise Conflict(
                "RETURN_COST_ROUNDING_POLICY_REQUIRED",
                "Fractional costs need a reviewed GL rounding policy.",
            )
        action_id = uuid.uuid4()
        entry = None
        stock_facts = source | {
            "currency_code": source["functional_currency"],
            "exchange_rate": Decimal(1),
        }
        if destination is None:
            assert_company_write(scope.company_id, "inventory", "inventory.write_off")
            if not data.get("approve_write_off") or data.get("loss_account_id") is None:
                raise Conflict(
                    "RETURN_WRITE_OFF_APPROVAL_REQUIRED",
                    "Explicit approval and loss account are required.",
                )
            _account(scope.company_id, data["loss_account_id"], {"expense", "cost_of_sales"})
            if cost > 0:
                _account(scope.company_id, line["inventory_account_id_snapshot"], {"asset"})
                entry = _journal(
                    scope, action_id, "return_stock_action", data["action_date"], data, key
                )
                _line(
                    scope.company_id,
                    entry,
                    1,
                    data["loss_account_id"],
                    cost,
                    True,
                    stock_facts,
                    source["functional_precision"],
                )
                _line(
                    scope.company_id,
                    entry,
                    2,
                    line["inventory_account_id_snapshot"],
                    cost,
                    False,
                    stock_facts,
                    source["functional_precision"],
                )
                _run("SELECT erp.post_journal_entry(%s,%s)", [scope.company_id, entry])
        elif data.get("loss_account_id") is not None or data.get("approve_write_off"):
            raise Conflict(
                "RETURN_LOSS_ACCOUNT_NOT_APPLICABLE", "Transfers do not create inventory losses."
            )
        _run(
            "INSERT INTO "
            "erp.sales_return_stock_actions(id,company_id,sales_return_line_id,from_ware"
            "house_id,to_warehouse_id,quantity,historical_cost,action_date,reason,loss_a"
            "ccount_id,journal_entry_id) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            [
                action_id,
                scope.company_id,
                line["id"],
                original_from,
                destination,
                requested,
                cost,
                data["action_date"],
                data["reason"],
                data.get("loss_account_id"),
                entry,
            ],
        )
        for warehouse_id, sign in [(original_from, -1)] + (
            [(destination, 1)] if destination else []
        ):
            _run(
                "INSERT INTO "
                "erp.stock_movements(company_id,event_key,occurred_at,warehouse_id,item_id,m"
                "ovement_kind,quantity_delta,unit_cost_company,value_delta_company,source_ty"
                "pe,source_id,source_line_id,journal_entry_id) VALUES "
                "(%s,%s,%s,%s,%s,%s,%s,%s,%s,'return_stock_action',%s,%s,%s)",
                [
                    scope.company_id,
                    f"return-stock:{action_id}:{sign}",
                    data["action_date"],
                    warehouse_id,
                    line["item_id"],
                    "transfer" if destination else "adjustment",
                    requested * sign,
                    _money(cost / requested, 6),
                    cost * sign,
                    action_id,
                    line["id"],
                    entry,
                ],
            )
        _effect(
            scope,
            action_id,
            "sales.return.stock.disposed",
            aggregate_type="sales_return_stock_action",
        )
        result = _json(
            _one(
                "SELECT * FROM erp.sales_return_stock_actions WHERE company_id=%s AND id=%s",
                [scope.company_id, action_id],
            )
        )
        _finish(receipt, result, result_type="sales_return_stock_action")
        return cast(dict[str, Any], result)
