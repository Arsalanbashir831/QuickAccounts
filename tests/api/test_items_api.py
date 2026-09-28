import uuid

import pytest
from django.contrib.auth import get_user_model
from django.db import connection
from rest_framework.test import APIClient


def _company_root(context: dict[str, object]) -> str:
    return f"/api/v1/tenants/{context['tenant']}/companies/{context['company']}"


def _items_url(context: dict[str, object]) -> str:
    return f"{_company_root(context)}/inventory/items"


def _seed_uom() -> uuid.UUID:
    category_id = uuid.uuid4()
    uom_id = uuid.uuid4()
    suffix = uom_id.hex[:10]
    with connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO erp.uom_categories(id,code,name) VALUES (%s,%s,%s)",
            [category_id, f"QTY-{suffix}", f"Quantity {suffix}"],
        )
        cursor.execute(
            """
            INSERT INTO erp.uoms(id,category_id,code,name,to_base_factor)
            VALUES (%s,%s,%s,%s,1)
            """,
            [uom_id, category_id, f"EA-{suffix}", f"Each {suffix}"],
        )
    return uom_id


def _item_payload(sku: str, uom_id: uuid.UUID, *, kind: str = "service") -> dict[str, object]:
    return {
        "sku": sku,
        "name": f"Item {sku}",
        "item_kind": kind,
        "base_uom_id": str(uom_id),
    }


def _create_item(
    client: APIClient,
    context: dict[str, object],
    sku: str,
    uom_id: uuid.UUID,
    *,
    kind: str = "service",
) -> dict[str, object]:
    response = client.post(
        _items_url(context), _item_payload(sku, uom_id, kind=kind), format="json"
    )
    assert response.status_code == 201, response.json()
    return response.json()


@pytest.mark.api
@pytest.mark.p0
@pytest.mark.django_db(transaction=True)
def test_item_catalog_is_revisioned_and_independent_from_inventory_module(
    accounting_context: dict[str, object],
) -> None:
    client = accounting_context["client"]
    assert isinstance(client, APIClient)
    uom_id = _seed_uom()
    items_url = _items_url(accounting_context)
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT mode FROM erp.company_module_settings
            WHERE company_id = %s AND module_code = 'inventory'
            """,
            [accounting_context["company"]],
        )
        assert cursor.fetchone()[0] == "disabled"

    service = _create_item(client, accounting_context, "SRV-001", uom_id)
    assert service["item_kind"] == "service"
    assert service["row_version"] == 1
    item_id = service["id"]

    stock = client.post(
        items_url,
        {
            **_item_payload("STK-001", uom_id, kind="stock"),
            "track_lots": True,
        },
        format="json",
    )
    assert stock.status_code == 201, stock.json()

    non_stock = _create_item(client, accounting_context, "NST-001", uom_id, kind="non_stock")
    assert non_stock["item_kind"] == "non_stock"

    invalid_tracking = client.post(
        items_url,
        {
            **_item_payload("SRV-BAD", uom_id),
            "track_serials": True,
        },
        format="json",
    )
    assert invalid_tracking.status_code == 400

    duplicate = client.post(items_url, _item_payload("SRV-001", uom_id), format="json")
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "ITEM_SKU_EXISTS"

    stale = client.patch(
        f"{items_url}/{item_id}",
        {"name": "Updated service"},
        format="json",
        HTTP_IF_MATCH='"2"',
    )
    assert stale.status_code == 412
    updated = client.patch(
        f"{items_url}/{item_id}",
        {"name": "Updated service"},
        format="json",
        HTTP_IF_MATCH='"1"',
    )
    assert updated.status_code == 200, updated.json()
    assert updated["ETag"] == '"2"'
    assert updated.json()["name"] == "Updated service"

    first_page = client.get(f"{items_url}?limit=2")
    assert first_page.status_code == 200, first_page.json()
    assert len(first_page.json()["results"]) == 2
    assert first_page.json()["next_cursor"]
    second_page = client.get(f"{items_url}?limit=2&cursor={first_page.json()['next_cursor']}")
    assert second_page.status_code == 200, second_page.json()
    assert len(second_page.json()["results"]) == 1

    service_filter = client.get(f"{items_url}?item_kind=service&q=SRV")
    assert service_filter.status_code == 200, service_filter.json()
    assert [row["sku"] for row in service_filter.json()["results"]] == ["SRV-001"]

    uoms = client.get(f"{_company_root(accounting_context)}/reference/uoms")
    assert uoms.status_code == 200, uoms.json()
    uom_rows = uoms.json()["results"]
    while not any(row["id"] == str(uom_id) for row in uom_rows) and uoms.json()["next_cursor"]:
        uoms = client.get(
            f"{_company_root(accounting_context)}/reference/uoms",
            {"cursor": uoms.json()["next_cursor"]},
        )
        assert uoms.status_code == 200, uoms.json()
        uom_rows.extend(uoms.json()["results"])
    assert any(row["id"] == str(uom_id) for row in uom_rows)

    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT action, request_id
            FROM erp.company_audit_events
            WHERE company_id = %s AND object_type = 'item'
            ORDER BY occurred_at, id
            """,
            [accounting_context["company"]],
        )
        events = cursor.fetchall()
    assert [event[0] for event in events] == [
        "item.created",
        "item.created",
        "item.created",
        "item.updated",
    ]
    assert all(event[1] for event in events)


def _seed_profile_accounts(context: dict[str, object]) -> dict[str, uuid.UUID]:
    ids = {
        "inventory": uuid.uuid4(),
        "revenue": uuid.uuid4(),
        "cogs": uuid.uuid4(),
        "purchase": uuid.uuid4(),
    }
    suffix = ids["inventory"].hex[:8]
    with connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO erp.accounts(
                id,company_id,code,name,account_type,normal_balance
            ) VALUES
                (%s,%s,%s,'Inventory asset','asset','debit'),
                (%s,%s,%s,'Item revenue','revenue','credit'),
                (%s,%s,%s,'Item cost of sales','cost_of_sales','debit'),
                (%s,%s,%s,'Item purchase expense','expense','debit')
            """,
            [
                ids["inventory"],
                context["company"],
                f"INV-{suffix}",
                ids["revenue"],
                context["company"],
                f"REV-{suffix}",
                ids["cogs"],
                context["company"],
                f"COGS-{suffix}",
                ids["purchase"],
                context["company"],
                f"PUR-{suffix}",
            ],
        )
    return ids


@pytest.mark.api
@pytest.mark.p0
@pytest.mark.django_db(transaction=True)
def test_item_accounting_profile_validates_and_replaces_mapping(
    accounting_context: dict[str, object],
) -> None:
    client = accounting_context["client"]
    assert isinstance(client, APIClient)
    uom_id = _seed_uom()
    item = _create_item(client, accounting_context, "MAP-001", uom_id, kind="stock")
    profile_url = f"{_items_url(accounting_context)}/{item['id']}/accounting-profile"
    accounts = _seed_profile_accounts(accounting_context)
    payload = {
        "inventory_account_id": str(accounts["inventory"]),
        "revenue_account_id": str(accounts["revenue"]),
        "cogs_account_id": str(accounts["cogs"]),
        "purchase_account_id": str(accounts["purchase"]),
    }

    missing = client.get(profile_url)
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "ITEM_ACCOUNTING_PROFILE_NOT_FOUND"

    created = client.put(profile_url, payload, format="json")
    assert created.status_code == 200, created.json()
    assert created.json()["inventory_account_id"] == str(accounts["inventory"])
    assert created.json()["revenue_account_code"].startswith("REV-")

    invalid_type = client.put(
        profile_url,
        {**payload, "revenue_account_id": str(accounting_context["equity"])},
        format="json",
    )
    assert invalid_type.status_code == 400
    assert invalid_type.json()["error"]["code"] == "INVALID_ITEM_ACCOUNT_MAPPING"

    replaced = client.put(
        profile_url,
        {**payload, "purchase_account_id": None},
        format="json",
    )
    assert replaced.status_code == 200, replaced.json()
    assert replaced.json()["purchase_account_id"] is None

    fetched = client.get(profile_url)
    assert fetched.status_code == 200, fetched.json()
    assert fetched.json() == replaced.json()

    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT action, request_id
            FROM erp.company_audit_events
            WHERE company_id = %s AND object_type = 'item_accounting_profile'
            ORDER BY occurred_at, id
            """,
            [accounting_context["company"]],
        )
        events = cursor.fetchall()
    assert [event[0] for event in events] == [
        "item.accounting_profile.created",
        "item.accounting_profile.updated",
    ]
    assert all(event[1] for event in events)


@pytest.mark.api
@pytest.mark.security
@pytest.mark.django_db(transaction=True)
def test_item_permissions_separate_read_manage_and_accounting_setup(
    accounting_context: dict[str, object],
) -> None:
    owner_client = accounting_context["client"]
    assert isinstance(owner_client, APIClient)
    uom_id = _seed_uom()
    item = _create_item(owner_client, accounting_context, "PERM-001", uom_id)

    member_id = uuid.uuid4()
    membership_id = uuid.uuid4()
    role_id = uuid.uuid4()
    with connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO identity.users(id,email,password) VALUES (%s,%s,'!')",
            [member_id, f"item-reader-{member_id}@example.com"],
        )
        cursor.execute(
            """
            INSERT INTO identity.tenant_memberships(tenant_id,user_id,tenant_role)
            VALUES (%s,%s,'member')
            """,
            [accounting_context["tenant"], member_id],
        )
        cursor.execute(
            """
            INSERT INTO identity.company_memberships(id,tenant_id,company_id,user_id)
            VALUES (%s,%s,%s,%s)
            """,
            [
                membership_id,
                accounting_context["tenant"],
                accounting_context["company"],
                member_id,
            ],
        )
        cursor.execute(
            "INSERT INTO identity.roles(id,company_id,code,name) VALUES (%s,%s,'items','Items')",
            [role_id, accounting_context["company"]],
        )
        cursor.execute(
            """
            INSERT INTO identity.role_permissions(company_id,role_id,permission_code)
            VALUES (%s,%s,'item.view')
            """,
            [accounting_context["company"], role_id],
        )
        cursor.execute(
            """
            INSERT INTO identity.company_role_assignments(company_id,membership_id,role_id)
            VALUES (%s,%s,%s)
            """,
            [accounting_context["company"], membership_id, role_id],
        )

    client = APIClient()
    client.force_login(get_user_model().objects.get(pk=member_id))
    readable = client.get(_items_url(accounting_context))
    assert readable.status_code == 200, readable.json()

    create_denied = client.post(
        _items_url(accounting_context),
        _item_payload("PERM-403", uom_id),
        format="json",
    )
    assert create_denied.status_code == 403

    profile_denied = client.get(f"{_items_url(accounting_context)}/{item['id']}/accounting-profile")
    assert profile_denied.status_code == 403
