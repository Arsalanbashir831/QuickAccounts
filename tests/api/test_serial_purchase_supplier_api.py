import uuid

import pytest
from django.db import connection

from tests.api.test_purchase_bill_drafts_api import (
    _bind_license,
    _change_module,
    _enable_purchasing,
    _payload,
    _root,
    _seed_posting_configuration,
    _seed_purchase_facts,
)
from tests.api.test_purchase_bill_posting_api import _post_payload

pytestmark = [pytest.mark.api, pytest.mark.p0, pytest.mark.django_db(transaction=True)]


def test_serial_purchase_receipt_and_linked_physical_supplier_credit(
    accounting_context: dict,
) -> None:
    client = accounting_context["client"]
    _enable_purchasing(client, accounting_context)
    ids = _seed_purchase_facts(accounting_context)
    posting = _seed_posting_configuration(accounting_context, ids)
    _bind_license(accounting_context, ("module.purchasing", "module.inventory"))
    _change_module(client, accounting_context, "inventory", "enabled")
    item_id = uuid.uuid4()
    warehouse_id = uuid.uuid4()
    with connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO erp.items(id,company_id,sku,name,item_kind,base_uom_id,"
            "track_serials) VALUES (%s,%s,%s,'Purchased serials','stock',%s,true)",
            [item_id, accounting_context["company"], f"SER-{item_id.hex[:8]}", ids["uom"]],
        )
        cursor.execute(
            "INSERT INTO erp.item_accounting_profiles(company_id,item_id,"
            "purchase_account_id,inventory_account_id) VALUES (%s,%s,%s,%s)",
            [accounting_context["company"], item_id,
             posting["purchase"], posting["inventory"]],
        )
        cursor.execute(
            "INSERT INTO erp.warehouses(id,company_id,code,name) "
            "VALUES (%s,%s,%s,'Supplier receiving')",
            [warehouse_id, accounting_context["company"], f"WH-{item_id.hex[:8]}"],
        )
    root = _root(accounting_context)
    bills = f"{root}/purchasing/bills"
    body = _payload(ids, str(uuid.uuid4()))
    body["lines"][0]["item_id"] = str(item_id)
    draft = client.post(bills, body, format="json")
    assert draft.status_code == 201, draft.json()
    calculated = client.post(
        f"{bills}/{draft.json()['id']}/calculate",
        format="json", HTTP_IF_MATCH='"1"',
    )
    assert calculated.status_code == 200, calculated.json()
    line_id = calculated.json()["lines"][0]["id"]
    posting_body = _post_payload(accounting_context, posting) | {
        "warehouse_id": str(warehouse_id),
        "serial_receipts": [{
            "purchase_bill_line_id": line_id,
            "serial_numbers": ["PUR-SN-001", "PUR-SN-002"],
        }],
    }
    invalid_receipt = client.post(
        f"{bills}/{draft.json()['id']}/post",
        posting_body | {"serial_receipts": [{
            "purchase_bill_line_id": line_id, "serial_numbers": ["PUR-SN-001"],
        }]},
        format="json", HTTP_IF_MATCH='"2"', HTTP_IDEMPOTENCY_KEY="serial-purchase-short",
    )
    assert invalid_receipt.status_code == 409, invalid_receipt.json()
    assert client.get(f"{root}/inventory/serials?serial=PUR-SN-001").json()[
        "results"
    ] == []
    posted = client.post(
        f"{bills}/{draft.json()['id']}/post", posting_body,
        format="json", HTTP_IF_MATCH='"2"', HTTP_IDEMPOTENCY_KEY="serial-purchase",
    )
    assert posted.status_code == 200, posted.json()
    serials = []
    for label in ("PUR-SN-001", "PUR-SN-002"):
        lookup = client.get(f"{root}/inventory/serials?serial={label}")
        assert lookup.status_code == 200, lookup.json()
        serials.append(lookup.json()["results"][0])
    assert all(serial["current_warehouse_id"] == str(warehouse_id) for serial in serials)
    page = client.get(f"{root}/inventory/serials?warehouse_id={warehouse_id}&limit=1")
    assert page.status_code == 200, page.json()
    assert page.json()["next_cursor"] is not None
    next_page = client.get(
        f"{root}/inventory/serials?warehouse_id={warehouse_id}"
        f"&limit=1&after={page.json()['next_cursor']}"
    )
    assert next_page.status_code == 200, next_page.json()
    assert page.json()["results"][0]["id"] != next_page.json()["results"][0]["id"]
    source_lookup = client.get(
        f"{root}/inventory/serials?source_type=purchase_bill"
        f"&source_id={posted.json()['id']}"
    )
    assert source_lookup.status_code == 200, source_lookup.json()
    assert {row["id"] for row in source_lookup.json()["results"]} == {
        serial["id"] for serial in serials
    }
    credit = client.post(
        f"{bills}/{posted.json()['id']}/credit-notes",
        {"bill_no": str(uuid.uuid4()), "bill_date": "2026-09-28",
         "source_line_ids": [line_id], "partial_quantities": ["1"]},
        format="json",
    )
    assert credit.status_code == 201, credit.json()
    invalid_credit = client.post(
        f"{bills}/{credit.json()['id']}/post",
        _post_payload(accounting_context, posting) | {
            "warehouse_id": str(warehouse_id),
            "serial_returns": [{
                "purchase_bill_line_id": credit.json()["lines"][0]["id"],
                "serial_ids": [serials[0]["id"], serials[1]["id"]],
            }],
        },
        format="json", HTTP_IF_MATCH='"1"', HTTP_IDEMPOTENCY_KEY="serial-credit-overselected",
    )
    assert invalid_credit.status_code == 409, invalid_credit.json()
    assert client.get(f"{root}/inventory/serials/{serials[0]['id']}").json()[
        "current_warehouse_id"
    ] == str(warehouse_id)
    supplier_warehouse = client.post(
        f"{root}/inventory/warehouses",
        {"code": f"SUP-{item_id.hex[:8]}", "name": "Supplier dispatch",
         "stock_category": "supplier_return"},
        format="json",
    )
    assert supplier_warehouse.status_code == 201, supplier_warehouse.json()
    selected_detail = client.get(f"{root}/inventory/serials/{serials[0]['id']}").json()
    transfer = client.post(
        f"{root}/inventory/documents",
        {"document_no": str(uuid.uuid4()), "document_kind": "transfer",
         "document_date": "2026-09-28", "reason": "Supplier return staging",
         "lines": [{"item_id": str(item_id), "lot_id": selected_detail["lot_id"],
                    "quantity": "1", "from_warehouse_id": str(warehouse_id),
                    "to_warehouse_id": supplier_warehouse.json()["id"]}]},
        format="json", HTTP_IDEMPOTENCY_KEY="supplier-serial-transfer",
    )
    assert transfer.status_code == 201, transfer.json()
    transferred = client.post(
        f"{root}/inventory/documents/{transfer.json()['id']}/post",
        _post_payload(accounting_context, posting),
        format="json", HTTP_IF_MATCH=f'"{transfer.json()["row_version"]}"',
        HTTP_IDEMPOTENCY_KEY="supplier-serial-transfer-post",
    )
    assert transferred.status_code == 200, transferred.json()
    credit_post = client.post(
        f"{bills}/{credit.json()['id']}/post",
        _post_payload(accounting_context, posting) | {
            "warehouse_id": supplier_warehouse.json()["id"],
            "serial_returns": [{
                "purchase_bill_line_id": credit.json()["lines"][0]["id"],
                "serial_ids": [serials[0]["id"]],
            }],
        },
        format="json", HTTP_IF_MATCH='"1"', HTTP_IDEMPOTENCY_KEY="serial-supplier-return",
    )
    assert credit_post.status_code == 200, credit_post.json()
    gone = client.get(f"{root}/inventory/serials/{serials[0]['id']}")
    retained = client.get(f"{root}/inventory/serials/{serials[1]['id']}")
    assert gone.json()["lifecycle_status"] == "outside"
    assert retained.json()["current_warehouse_id"] == str(warehouse_id)
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT coalesce(sum(on_hand_quantity),0) FROM erp.inventory_positions "
            "WHERE company_id=%s AND item_id=%s",
            [accounting_context["company"], item_id],
        )
        quantity = cursor.fetchone()[0]
        cursor.execute(
            "SELECT count(*) FROM erp.inventory_serials WHERE company_id=%s "
            "AND item_id=%s AND lifecycle_status='in_stock'",
            [accounting_context["company"], item_id],
        )
        assert cursor.fetchone()[0] == quantity == 1
