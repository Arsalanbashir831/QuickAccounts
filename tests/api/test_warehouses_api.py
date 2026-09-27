import concurrent.futures
import uuid

import pytest
from django.contrib.auth import get_user_model
from django.db import IntegrityError, close_old_connections, connection, transaction
from rest_framework.test import APIClient

from apps.inventory.warehouse_services import save_warehouse
from common.access.scopes import CompanyScope
from common.api.errors import PreconditionFailed
from tests.api.test_items_api import _seed_uom
from tests.api.test_sales_invoice_posting_api import _bind_inventory_license, _change_module

pytestmark = [pytest.mark.api, pytest.mark.p0, pytest.mark.django_db(transaction=True)]


def _setup(context: dict[str, object]) -> tuple[APIClient, str]:
    client = context["client"]
    assert isinstance(client, APIClient)
    _bind_inventory_license(context)
    _change_module(client, context, "inventory", "enabled")
    return (
        client,
        f"/api/v1/tenants/{context['tenant']}/companies/{context['company']}/inventory/warehouses",
    )


def test_warehouse_crud_revision_pagination_and_audit(
    accounting_context: dict[str, object],
) -> None:
    client, url = _setup(accounting_context)
    created = client.post(
        url, {"code": "RET", "name": "Returns", "stock_category": "quarantine"}, format="json"
    )
    assert created.status_code == 201, created.json()
    detail_url = f"{url}/{created.json()['id']}"
    assert client.get(detail_url).json()["stock_category"] == "quarantine"
    duplicate = client.post(url, {"code": "RET", "name": "Duplicate"}, format="json")
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "WAREHOUSE_CODE_EXISTS"
    changed = client.patch(detail_url, {"name": "Inspection"}, format="json", HTTP_IF_MATCH='"1"')
    assert changed.status_code == 200, changed.json()
    assert changed.json()["row_version"] == 2
    assert (
        client.patch(detail_url, {"name": "Stale"}, format="json", HTTP_IF_MATCH='"1"').status_code
        == 412
    )
    assert client.patch(detail_url, {}, format="json", HTTP_IF_MATCH='"2"').status_code == 400
    assert client.delete(detail_url).status_code == 405
    assert client.get(f"{url}/{uuid.uuid4()}").status_code == 404
    assert client.get(f"{url}?cursor=bad").status_code == 400
    second = client.post(url, {"code": "MAIN", "name": "Main"}, format="json")
    assert second.status_code == 201
    assert second.json()["stock_category"] == "sellable"
    first = client.get(f"{url}?limit=1").json()
    last = client.get(f"{url}?limit=1&cursor={first['next_cursor']}").json()
    assert len(last["results"]) == 1
    assert last["results"][0]["id"] != first["results"][0]["id"]
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) FROM erp.company_audit_events "
            "WHERE company_id=%s AND object_type='warehouse'",
            [accounting_context["company"]],
        )
        assert cursor.fetchone()[0] == 3


@pytest.mark.parametrize("category", ["quarantine", "damaged", "supplier_return"])
def test_non_sellable_balances_and_database_guards(
    accounting_context: dict[str, object], category: str
) -> None:
    client, url = _setup(accounting_context)
    warehouse = client.post(
        url, {"code": "RET", "name": "Returns", "stock_category": category}, format="json"
    ).json()
    item = uuid.uuid4()
    uom = _seed_uom()
    with connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO erp.items(id,company_id,sku,name,item_kind,base_uom_id) "
            "VALUES (%s,%s,'STK','Stock','stock',%s)",
            [item, accounting_context["company"], uom],
        )
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT set_config('app.tenant_id',%s,true)", [str(accounting_context["tenant"])]
        )
        cursor.execute(
            "INSERT INTO erp.stock_movements(company_id,event_key,occurred_at,warehouse_id,"
            "item_id,movement_kind,quantity_delta,unit_cost_company,value_delta_company) "
            "VALUES (%s,'opening',clock_timestamp(),%s,%s,'receipt',3,10,30)",
            [accounting_context["company"], warehouse["id"], item],
        )
    balances = client.get(f"{url}/{warehouse['id']}/balances")
    assert balances.status_code == 200, balances.json()
    row = balances.json()["results"][0]
    assert float(row["on_hand_quantity"]) == 3
    assert float(row["available_quantity"]) == 0
    assert float(row["value_company"]) == 30
    for changes, code in [
        ({"stock_category": "sellable"}, "WAREHOUSE_CATEGORY_IN_USE"),
        ({"is_active": False}, "WAREHOUSE_BALANCE_IN_USE"),
    ]:
        response = client.patch(
            f"{url}/{warehouse['id']}", changes, format="json", HTTP_IF_MATCH='"1"'
        )
        assert response.status_code == 409, response.json()
        assert response.json()["error"]["code"] == code
    with pytest.raises(IntegrityError), transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT set_config('app.tenant_id',%s,true)", [str(accounting_context["tenant"])]
        )
        cursor.execute(
            "INSERT INTO erp.stock_movements(company_id,event_key,occurred_at,warehouse_id,"
            "item_id,movement_kind,quantity_delta,unit_cost_company,value_delta_company,"
            "source_type,source_id) VALUES (%s,'sale',clock_timestamp(),%s,%s,"
            "'issue',-1,10,-10,'sales_invoice',%s)",
            [accounting_context["company"], warehouse["id"], item, uuid.uuid4()],
        )
    with pytest.raises(IntegrityError), transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT set_config('app.tenant_id',%s,true)", [str(accounting_context["tenant"])]
        )
        cursor.execute(
            "INSERT INTO erp.inventory_reservations(company_id,warehouse_id,item_id,"
            "reservation_key,source_type,source_id,quantity) "
            "VALUES (%s,%s,%s,'reserve','sales_order',%s,1)",
            [accounting_context["company"], warehouse["id"], item, uuid.uuid4()],
        )
    assert (
        float(
            client.get(f"{url}/{warehouse['id']}/balances").json()["results"][0]["on_hand_quantity"]
        )
        == 3
    )


def test_inventory_disabled_blocks_warehouse_access(accounting_context: dict[str, object]) -> None:
    client = accounting_context["client"]
    assert isinstance(client, APIClient)
    url = (
        f"/api/v1/tenants/{accounting_context['tenant']}"
        f"/companies/{accounting_context['company']}/inventory/warehouses"
    )
    assert client.get(url).status_code == 403
    assert client.post(url, {"code": "MAIN", "name": "Main"}, format="json").status_code == 403


def test_empty_warehouse_lifecycle_and_read_only_policy(
    accounting_context: dict[str, object],
) -> None:
    client, url = _setup(accounting_context)
    warehouse = client.post(url, {"code": "EMPTY", "name": "Empty"}, format="json").json()
    detail = f"{url}/{warehouse['id']}"
    changed = client.patch(
        detail,
        {"stock_category": "damaged", "is_active": False},
        format="json",
        HTTP_IF_MATCH='"1"',
    )
    assert changed.status_code == 200, changed.json()
    with pytest.raises(IntegrityError), transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "DELETE FROM erp.warehouses WHERE company_id=%s AND id=%s",
            [accounting_context["company"], warehouse["id"]],
        )
    _change_module(client, accounting_context, "inventory", "read_only")
    assert client.get(detail).status_code == 200
    assert client.get(f"{detail}/balances").status_code == 200
    assert (
        client.patch(detail, {"is_active": True}, format="json", HTTP_IF_MATCH='"2"').status_code
        == 403
    )


@pytest.mark.concurrency
def test_warehouse_concurrent_revisions(accounting_context: dict[str, object]) -> None:
    client, url = _setup(accounting_context)
    warehouse = client.post(url, {"code": "MAIN", "name": "Main"}, format="json").json()
    scope = CompanyScope(
        tenant_id=accounting_context["tenant"],
        company_id=accounting_context["company"],
        user_id=accounting_context["user"],
    )

    def update(name: str) -> str:
        close_old_connections()
        try:
            save_warehouse(
                scope, {"name": name}, warehouse_id=uuid.UUID(warehouse["id"]), expected_revision=1
            )
            return "updated"
        except PreconditionFailed:
            return "stale"
        finally:
            close_old_connections()

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(update, ["First", "Second"]))
    assert sorted(results) == ["stale", "updated"]
    assert client.get(f"{url}/{warehouse['id']}").json()["row_version"] == 2


@pytest.mark.security
def test_warehouse_membership_permission_and_scope(accounting_context: dict[str, object]) -> None:
    owner, url = _setup(accounting_context)
    warehouse = owner.post(url, {"code": "MAIN", "name": "Main"}, format="json").json()
    other_company = uuid.uuid4()
    with connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO erp.companies(id,tenant_id,code,legal_name,functional_currency) "
            "VALUES (%s,%s,'OTHER','Other','USD')",
            [other_company, accounting_context["tenant"]],
        )
        cursor.execute(
            "INSERT INTO erp.warehouses(company_id,code,name) "
            "VALUES (%s,'OTHER','Other') RETURNING id",
            [other_company],
        )
        hidden = cursor.fetchone()[0]
    assert owner.get(f"{url}/{hidden}").status_code == 404
    assert owner.get(f"{url}/{hidden}/balances").status_code == 404
    assert (
        owner.patch(
            f"{url}/{hidden}", {"name": "Changed"}, format="json", HTTP_IF_MATCH='"1"'
        ).status_code
        == 404
    )
    member = get_user_model().objects.create_user(email=f"member-{uuid.uuid4()}@example.com")
    with connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO identity.tenant_memberships(tenant_id,user_id,tenant_role) "
            "VALUES (%s,%s,'member')",
            [accounting_context["tenant"], member.pk],
        )
        cursor.execute(
            "INSERT INTO identity.company_memberships(tenant_id,company_id,user_id) "
            "VALUES (%s,%s,%s)",
            [accounting_context["tenant"], accounting_context["company"], member.pk],
        )
    client = APIClient()
    client.force_authenticate(member)
    assert client.get(url).status_code == 403
    assert client.post(url, {"code": "BAD", "name": "Denied"}, format="json").status_code == 403
    assert (
        client.patch(
            f"{url}/{warehouse['id']}", {"name": "Denied"}, format="json", HTTP_IF_MATCH='"1"'
        ).status_code
        == 403
    )
    assert owner.get(f"{url}/{warehouse['id']}").json()["row_version"] == 1
