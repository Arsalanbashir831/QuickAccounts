import concurrent.futures
import uuid
from decimal import Decimal
from importlib import import_module

import pytest
from django.db import DatabaseError, close_old_connections, connection, transaction

from apps.purchasing.services import create_linked_supplier_credit
from common.access.scopes import CompanyScope
from common.api.errors import APIError
from tests.api.test_purchase_bill_drafts_api import _root
from tests.api.test_sales_returns_api import _command
from tests.api.test_supplier_return_costing_api import _setup

pytestmark = [pytest.mark.api, pytest.mark.p0, pytest.mark.django_db(transaction=True)]


def test_partial_supplier_return_migration_reverse_forward(accounting_context):
    migration = import_module("apps.database.migrations.0039_partial_supplier_returns")
    transfer = import_module("apps.database.migrations.0040_supplier_transfer_provenance")
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(transfer.REVERSE)
        cursor.execute(migration.reverse_sql())
        cursor.execute(migration.SQL)
        cursor.execute(transfer.SQL)
        transaction.set_rollback(True)


def test_two_partial_credits_physically_return_exact_source_quantity(accounting_context):
    setup = _setup(accounting_context, partial_quantity="1")
    first = setup["credit"]
    assert Decimal(first["lines"][0]["quantity"]) == 1
    assert Decimal(first["lines"][0]["credit_quantity_offset"]) == 0
    response = _command(setup["client"], setup["url"], setup["payload"], 1, "first-return")
    assert response.status_code == 200, response.json()
    source_bill = first["credit_of_bill_id"]
    body = {
        "bill_no": str(uuid.uuid4()),
        "bill_date": "2026-09-28",
        "source_line_ids": [first["lines"][0]["credit_of_bill_line_id"]],
        "partial_quantities": ["1"],
    }
    response = setup["client"].post(
        f"{_root(accounting_context)}/purchasing/bills/{source_bill}/credit-notes",
        body,
        format="json",
    )
    assert response.status_code == 201, response.json()
    second = response.json()
    assert Decimal(second["lines"][0]["credit_quantity_offset"]) == 1
    over = setup["client"].post(
        f"{_root(accounting_context)}/purchasing/bills/{source_bill}/credit-notes",
        body | {"bill_no": str(uuid.uuid4())},
        format="json",
    )
    assert over.status_code == 409, over.json()
    response = _command(
        setup["client"],
        f"{_root(accounting_context)}/purchasing/bills/{second['id']}/post",
        setup["payload"],
        1,
        "second-return",
    )
    assert response.status_code == 200, response.json()
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT coalesce(sum(quantity_delta),0) FROM erp.stock_movements "
            "WHERE company_id=%s AND source_type='purchase_bill' "
            "AND source_id IN (%s,%s)",
            [accounting_context["company"], first["id"], second["id"]],
        )
        assert cursor.fetchone()[0] == -2
    migration = import_module("apps.database.migrations.0039_partial_supplier_returns")
    with pytest.raises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(migration.reverse_sql())


def test_partial_return_after_one_proven_transfer(accounting_context):
    setup = _setup(accounting_context, partial_quantity="1")
    first = setup["credit"]
    assert _command(setup["client"], setup["url"], setup["payload"], 1).status_code == 200
    root = _root(accounting_context)
    response = setup["client"].post(
        f"{root}/inventory/warehouses",
        {
            "code": str(uuid.uuid4()),
            "name": "Supplier return dispatch",
            "stock_category": "supplier_return",
            "operational_role": "supplier_return",
        },
        format="json",
    )
    assert response.status_code == 201, response.json()
    warehouse = response.json()["id"]
    response = _command(
        setup["client"],
        f"{root}/inventory/documents",
        {
            "document_no": str(uuid.uuid4()),
            "document_kind": "transfer",
            "document_date": "2026-09-28",
            "reason": "Move original remaining stock",
            "lines": [
                {
                    "item_id": str(setup["ids"]["item"]),
                    "quantity": "1",
                    "from_warehouse_id": str(setup["warehouse"]),
                    "to_warehouse_id": warehouse,
                }
            ],
        },
        1,
    )
    assert response.status_code == 201, response.json()
    document = response.json()
    response = _command(
        setup["client"],
        f"{root}/inventory/documents/{document['id']}/post",
        {
            "journal_id": str(setup["posting"]["journal"]),
            "fiscal_period_id": str(accounting_context["period"]),
        },
        document["row_version"],
    )
    assert response.status_code == 200, response.json()
    body = {
        "bill_no": str(uuid.uuid4()),
        "bill_date": "2026-09-28",
        "source_line_ids": [first["lines"][0]["credit_of_bill_line_id"]],
        "partial_quantities": ["1"],
    }
    response = setup["client"].post(
        f"{root}/purchasing/bills/{first['credit_of_bill_id']}/credit-notes", body, format="json"
    )
    assert response.status_code == 201, response.json()
    credit = response.json()
    response = _command(
        setup["client"],
        f"{root}/purchasing/bills/{credit['id']}/post",
        setup["payload"] | {"warehouse_id": warehouse},
        1,
    )
    assert response.status_code == 200, response.json()
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT on_hand_quantity,value_company FROM erp.inventory_positions "
            "WHERE company_id=%s AND warehouse_id=%s AND item_id=%s",
            [accounting_context["company"], warehouse, setup["ids"]["item"]],
        )
        assert cursor.fetchone() == (0, 0)


def test_concurrent_partial_credit_creation_reserves_only_one_open_draft(accounting_context):
    setup = _setup(accounting_context, partial_quantity="1")
    first = setup["credit"]
    assert _command(setup["client"], setup["url"], setup["payload"], 1).status_code == 200
    scope = CompanyScope(
        tenant_id=accounting_context["tenant"],
        company_id=accounting_context["company"],
        user_id=accounting_context["user"],
    )

    def create(_: int) -> str:
        close_old_connections()
        try:
            result = create_linked_supplier_credit(
                scope,
                first["credit_of_bill_id"],
                {
                    "bill_no": str(uuid.uuid4()),
                    "bill_date": first["bill_date"],
                    "source_line_ids": [uuid.UUID(first["lines"][0]["credit_of_bill_line_id"])],
                    "partial_quantities": [Decimal(1)],
                },
                request_id=None,
            )
            return result["status"]
        except APIError as exc:
            return exc.default_code
        finally:
            close_old_connections()

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(create, range(2)))
    assert sorted(outcomes) == ["BILL_LINE_ALREADY_CREDITED", "draft"]
