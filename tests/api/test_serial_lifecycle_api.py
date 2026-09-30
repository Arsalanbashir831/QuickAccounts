import concurrent.futures
import uuid

import pytest
from django.db import DatabaseError, close_old_connections, connection, transaction

from apps.inventory.stock_commands import create_serial_lots_bulk
from common.access.scopes import CompanyScope
from tests.api.test_purchase_bill_drafts_api import _root
from tests.api.test_sales_invoice_drafts_api import _payload
from tests.api.test_sales_returns_api import _command, _posting, _stock_setup

pytestmark = [pytest.mark.api, pytest.mark.p0, pytest.mark.django_db(transaction=True)]


def test_concurrent_duplicate_serial_registration_has_one_winner(
    accounting_context: dict,
) -> None:
    setup = _stock_setup(accounting_context)
    item_id = _serialized_item(accounting_context, setup)
    scope = CompanyScope(
        tenant_id=accounting_context["tenant"],
        company_id=accounting_context["company"],
        user_id=accounting_context["user"],
    )

    def worker(number: int) -> str:
        close_old_connections()
        try:
            create_serial_lots_bulk(
                scope, item_id, ["RACE-SERIAL"],
                key=f"serial-race-{number}", request_id=None,
            )
            return "created"
        except DatabaseError:
            return "duplicate"
        finally:
            close_old_connections()

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(worker, (1, 2)))
    assert sorted(outcomes) == ["created", "duplicate"]
    other_item = _serialized_item(accounting_context, setup)
    other = create_serial_lots_bulk(
        scope, other_item, ["RACE-SERIAL"], key="same-label-other-item",
        request_id=None,
    )
    assert other["count"] == 1


def _serialized_item(context: dict, setup: dict) -> uuid.UUID:
    item_id = uuid.uuid4()
    with connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO erp.items(id,company_id,sku,name,item_kind,base_uom_id,track_serials) "
            "SELECT %s,company_id,%s,'Serialized unit','stock',base_uom_id,true "
            "FROM erp.items WHERE id=%s",
            [item_id, str(item_id), setup["ids"]["item"]],
        )
        cursor.execute(
            "INSERT INTO erp.item_accounting_profiles(company_id,item_id,inventory_account_id,"
            "cogs_account_id,purchase_account_id,revenue_account_id) "
            "SELECT company_id,%s,inventory_account_id,cogs_account_id,"
            "purchase_account_id,revenue_account_id FROM erp.item_accounting_profiles "
            "WHERE company_id=%s AND item_id=%s",
            [item_id, context["company"], setup["ids"]["item"]],
        )
    return item_id


def test_failed_receipt_post_rolls_back_serial_identity(accounting_context: dict) -> None:
    setup = _stock_setup(accounting_context)
    item_id = _serialized_item(accounting_context, setup)
    root = _root(accounting_context)
    lot = setup["client"].post(
        f"{root}/inventory/lots/serial-bulk",
        {"item_id": str(item_id), "serial_numbers": ["ROLLBACK-001"]},
        format="json", HTTP_IDEMPOTENCY_KEY="rollback-serial-lot",
    )
    assert lot.status_code == 201, lot.json()
    document = setup["client"].post(
        f"{root}/inventory/documents",
        {"document_no": str(uuid.uuid4()), "document_kind": "receipt",
         "document_date": "2026-09-27", "reason": "Failed serial receipt",
         "lines": [{"item_id": str(item_id), "lot_id": lot.json()["lots"][0]["id"],
                    "quantity": "1", "to_warehouse_id": str(setup["warehouse"]),
                    "unit_cost_company": "100"}]},
        format="json", HTTP_IDEMPOTENCY_KEY="rollback-serial-receipt",
    )
    assert document.status_code == 201, document.json()
    failed = _command(
        setup["client"], f"{root}/inventory/documents/{document.json()['id']}/post",
        _posting(accounting_context) | {"offset_account_id": str(uuid.uuid4())},
        document.json()["row_version"],
    )
    assert failed.status_code == 409, failed.json()
    lookup = setup["client"].get(f"{root}/inventory/serials?serial=ROLLBACK-001")
    assert lookup.status_code == 200, lookup.json()
    assert lookup.json()["results"] == []


def _receive_serial(
    context: dict, setup: dict, item_id: uuid.UUID, warehouse_id: str, label: str,
) -> tuple[str, str]:
    root = _root(context)
    lot = setup["client"].post(
        f"{root}/inventory/lots/serial-bulk",
        {"item_id": str(item_id), "serial_numbers": [label]},
        format="json", HTTP_IDEMPOTENCY_KEY=f"lot-{label}",
    )
    assert lot.status_code == 201, lot.json()
    lot_id = lot.json()["lots"][0]["id"]
    receipt = setup["client"].post(
        f"{root}/inventory/documents",
        {"document_no": str(uuid.uuid4()), "document_kind": "receipt",
         "document_date": "2026-09-27", "reason": "Serialized replacement receipt",
         "lines": [{"item_id": str(item_id), "lot_id": lot_id,
                    "quantity": "1", "to_warehouse_id": warehouse_id,
                    "unit_cost_company": "100"}]},
        format="json", HTTP_IDEMPOTENCY_KEY=f"receipt-{label}",
    )
    assert receipt.status_code == 201, receipt.json()
    posted = _command(
        setup["client"], f"{root}/inventory/documents/{receipt.json()['id']}/post",
        _posting(context) | {"offset_account_id": str(setup["posting"]["cogs"])},
        receipt.json()["row_version"],
    )
    assert posted.status_code == 200, posted.json()
    serial = setup["client"].get(f"{root}/inventory/serials?serial={label}").json()
    return lot_id, serial["results"][0]["id"]


def test_serial_receipt_transfer_and_exact_lookup(accounting_context: dict) -> None:
    setup = _stock_setup(accounting_context)
    item_id = _serialized_item(accounting_context, setup)
    root = _root(accounting_context)
    response = setup["client"].post(
        f"{root}/inventory/lots/serial-bulk",
        {"item_id": str(item_id), "serial_numbers": ["SN-001"]},
        format="json",
        HTTP_IDEMPOTENCY_KEY="serial-bulk-1",
    )
    assert response.status_code == 201, response.json()
    lot_id = response.json()["lots"][0]["id"]
    response = setup["client"].post(
        f"{root}/inventory/documents",
        {
            "document_no": str(uuid.uuid4()),
            "document_kind": "receipt",
            "document_date": "2026-09-27",
            "reason": "Serialized purchase receipt",
            "lines": [
                {
                    "item_id": str(item_id),
                    "lot_id": lot_id,
                    "quantity": "1",
                    "to_warehouse_id": str(setup["warehouse"]),
                    "unit_cost_company": "100",
                }
            ],
        },
        format="json",
        HTTP_IDEMPOTENCY_KEY="serial-receipt-1",
    )
    assert response.status_code == 201, response.json()
    document = response.json()
    posted = _command(
        setup["client"],
        f"{root}/inventory/documents/{document['id']}/post",
        _posting(accounting_context) | {"offset_account_id": str(setup["posting"]["cogs"])},
        document["row_version"],
    )
    assert posted.status_code == 200, posted.json()
    lookup = setup["client"].get(f"{root}/inventory/serials?serial= sn-001 ")
    assert lookup.status_code == 200, lookup.json()
    serial = lookup.json()["results"][0]
    assert serial["current_warehouse_id"] == str(setup["warehouse"])
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("GRANT USAGE ON SCHEMA erp TO quickaccounts_runtime")
        cursor.execute(
            "GRANT SELECT ON erp.companies,erp.inventory_serials "
            "TO quickaccounts_runtime"
        )
        cursor.execute("SET LOCAL ROLE quickaccounts_runtime")
        cursor.execute("SELECT set_config('app.tenant_id',%s,true)", [str(uuid.uuid4())])
        cursor.execute("SELECT count(*) FROM erp.inventory_serials WHERE id=%s", [serial["id"]])
        assert cursor.fetchone()[0] == 0
        transaction.set_rollback(True)
    assert (
        len(
            setup["client"]
            .get(f"{root}/inventory/serials/{serial['id']}/history")
            .json()["results"]
        )
        == 1
    )
    duplicate = setup["client"].post(
        f"{root}/inventory/lots/serial-bulk",
        {"item_id": str(item_id), "serial_numbers": [" sn-001 "]},
        format="json",
        HTTP_IDEMPOTENCY_KEY="serial-duplicate",
    )
    assert duplicate.status_code == 409
    other = (
        setup["client"]
        .post(
            f"{root}/inventory/warehouses",
            {"code": "SERIAL-OTHER", "name": "Serial destination"},
            format="json",
        )
        .json()
    )
    response = setup["client"].post(
        f"{root}/inventory/documents",
        {
            "document_no": str(uuid.uuid4()),
            "document_kind": "transfer",
            "document_date": "2026-09-27",
            "reason": "Serial transfer",
            "lines": [
                {
                    "item_id": str(item_id),
                    "lot_id": lot_id,
                    "quantity": "1",
                    "from_warehouse_id": str(setup["warehouse"]),
                    "to_warehouse_id": other["id"],
                }
            ],
        },
        format="json",
        HTTP_IDEMPOTENCY_KEY="serial-transfer-1",
    )
    assert response.status_code == 201, response.json()
    transfer = response.json()
    posted = _command(
        setup["client"],
        f"{root}/inventory/documents/{transfer['id']}/post",
        _posting(accounting_context),
        transfer["row_version"],
    )
    assert posted.status_code == 200, posted.json()
    detail = setup["client"].get(f"{root}/inventory/serials/{serial['id']}").json()
    assert detail["current_warehouse_id"] == other["id"]
    history = setup["client"].get(f"{root}/inventory/serials/{serial['id']}/history").json()
    assert [row["quantity_delta"] for row in history["results"]] == [
        "1.000000",
        "-1.000000",
        "1.000000",
    ]
    invoice_body = _payload(setup["ids"], str(uuid.uuid4()))
    invoice_body["lines"][0]["item_id"] = str(item_id)
    invoice_body["lines"][0]["quantity"] = "1"
    response = setup["client"].post(f"{root}/sales/invoices", invoice_body, format="json")
    assert response.status_code == 201, response.json()
    invoice = response.json()
    calculated = _command(
        setup["client"],
        f"{root}/sales/invoices/{invoice['id']}/calculate",
        {},
        invoice["row_version"],
    )
    assert calculated.status_code == 200, calculated.json()
    posted_invoice = _command(
        setup["client"],
        f"{root}/sales/invoices/{invoice['id']}/post",
        _posting(accounting_context)
        | {
            "stock_fulfillment": "deferred",
            "journal_id": str(setup["posting"]["journal"]),
        },
        calculated.json()["row_version"],
    )
    assert posted_invoice.status_code == 200, posted_invoice.json()
    invoice_line_id = posted_invoice.json()["lines"][0]["id"]
    response = setup["client"].post(
        f"{root}/inventory/documents",
        {
            "document_no": str(uuid.uuid4()),
            "document_kind": "shipment",
            "document_date": "2026-09-27",
            "reason": "Serialized shipment",
            "lines": [
                {
                    "item_id": str(item_id),
                    "lot_id": lot_id,
                    "quantity": "1",
                    "from_warehouse_id": other["id"],
                    "sales_invoice_line_id": invoice_line_id,
                }
            ],
        },
        format="json",
        HTTP_IDEMPOTENCY_KEY="serial-shipment-1",
    )
    assert response.status_code == 201, response.json()
    shipment = response.json()
    reserved = _command(
        setup["client"], f"{root}/inventory/reservations",
        {"line_id": shipment["lines"][0]["id"],
         "document_revision": shipment["row_version"],
         "reservation_key": "serial-shipment-reservation", "quantity": "1"},
        shipment["row_version"],
    )
    assert reserved.status_code == 201, reserved.json()
    released_reservation = _command(
        setup["client"],
        f"{root}/inventory/reservations/{reserved.json()['id']}/release",
        {}, reserved.json()["row_version"],
    )
    assert released_reservation.status_code == 200, released_reservation.json()
    reserved_again = _command(
        setup["client"], f"{root}/inventory/reservations",
        {"line_id": shipment["lines"][0]["id"],
         "document_revision": shipment["row_version"],
         "reservation_key": "serial-shipment-reservation-2", "quantity": "1"},
        shipment["row_version"],
    )
    assert reserved_again.status_code == 201, reserved_again.json()
    shipped = _command(
        setup["client"],
        f"{root}/inventory/documents/{shipment['id']}/post",
        _posting(accounting_context),
        shipment["row_version"],
    )
    assert shipped.status_code == 200, shipped.json()
    detail = setup["client"].get(f"{root}/inventory/serials/{serial['id']}").json()
    assert detail["lifecycle_status"] == "with_customer"
    assert detail["last_sales_invoice_line_id"] == invoice_line_id
    actions = setup["client"].get(f"{root}/inventory/serials/{serial['id']}/history").json()
    assert "reserved" in [row["movement_kind"] for row in actions["results"]]
    assert "reservation_released" in [row["movement_kind"] for row in actions["results"]]
    assert "reservation_consumed" in [row["movement_kind"] for row in actions["results"]]
    abandoned = _command(
        setup["client"], f"{root}/sales/invoices/{invoice['id']}/returns",
        {"return_no": str(uuid.uuid4()), "kind": "return",
         "return_date": "2026-09-27", "reason": "Cancelled serial intake",
         "lines": [{"sales_invoice_line_id": invoice_line_id, "quantity": "1"}]}, 1,
    )
    assert abandoned.status_code == 201, abandoned.json()
    abandoned_inspection = _command(
        setup["client"], f"{root}/sales/returns/{abandoned.json()['id']}/inspect",
        {"lines": [{"line_id": abandoned.json()["lines"][0]["id"],
                    "received_quantity": "1", "condition": "resellable",
                    "disposition": "restock", "warehouse_id": other["id"],
                    "serial_ids": [serial["id"]]}]},
        abandoned.json()["row_version"],
    )
    assert abandoned_inspection.status_code == 200, abandoned_inspection.json()
    voided = _command(
        setup["client"], f"{root}/sales/returns/{abandoned.json()['id']}/void",
        {}, abandoned_inspection.json()["row_version"],
    )
    assert voided.status_code == 200, voided.json()
    assert setup["client"].get(f"{root}/inventory/serials/{serial['id']}").json()[
        "lifecycle_status"
    ] == "with_customer"
    response = _command(
        setup["client"], f"{root}/sales/invoices/{invoice['id']}/returns",
        {"return_no": str(uuid.uuid4()), "kind": "return", "return_date": "2026-09-27",
         "reason": "Serialized item returned",
         "lines": [{"sales_invoice_line_id": invoice_line_id, "quantity": "1"}]}, 1,
    )
    assert response.status_code == 201, response.json()
    returned = response.json()
    damaged_warehouse = setup["client"].post(
        f"{root}/inventory/warehouses",
        {"code": "SERIAL-REPAIR", "name": "Serialized repairs", "stock_category": "damaged"},
        format="json",
    ).json()
    response = _command(
        setup["client"], f"{root}/sales/returns/{returned['id']}/inspect",
        {"lines": [{"line_id": returned["lines"][0]["id"],
                    "received_quantity": "1", "condition": "damaged",
                    "disposition": "damaged", "warehouse_id": damaged_warehouse["id"],
                    "serial_ids": [serial["id"]]}]}, returned["row_version"],
    )
    assert response.status_code == 200, response.json()
    inspected = response.json()
    response = _command(
        setup["client"], f"{root}/sales/returns/{returned['id']}/post",
        _posting(accounting_context), inspected["row_version"],
    )
    assert response.status_code == 200, response.json()
    detail = setup["client"].get(f"{root}/inventory/serials/{serial['id']}").json()
    assert detail["lifecycle_status"] == "in_stock"
    assert detail["current_warehouse_id"] == damaged_warehouse["id"]
    job_response = _command(
        setup["client"], f"{root}/inventory/repairs",
        {"line_id": returned["lines"][0]["id"], "serial_id": serial["id"],
         "warehouse_id": damaged_warehouse["id"], "quantity": "1",
         "diagnosis": "Replace damaged connector"},
        response.json()["row_version"],
    )
    assert job_response.status_code == 201, job_response.json()
    job = job_response.json()
    repaired = _command(
        setup["client"], f"{root}/inventory/repairs/{job['id']}",
        {"status": "repaired"}, job["row_version"],
    )
    assert repaired.status_code == 200, repaired.json()
    qc = _command(
        setup["client"], f"{root}/inventory/repairs/{job['id']}",
        {"status": "qc_passed", "qc_notes": "Function test passed"},
        repaired.json()["row_version"],
    )
    assert qc.status_code == 200, qc.json()
    released = _command(
        setup["client"], f"{root}/sales/returns/{returned['id']}/stock-dispositions",
        _posting(accounting_context) | {
            "line_id": returned["lines"][0]["id"], "serial_id": serial["id"],
            "repair_job_id": job["id"], "from_warehouse_id": damaged_warehouse["id"],
            "to_warehouse_id": other["id"], "quantity": "1",
            "action_date": "2026-09-30", "reason": "QC-passed serial restock",
        }, response.json()["row_version"],
    )
    assert released.status_code == 200, released.json()
    assert setup["client"].get(f"{root}/inventory/serials/{serial['id']}").json()[
        "current_warehouse_id"
    ] == other["id"]
    new_lot = setup["client"].post(
        f"{root}/inventory/lots/serial-bulk",
        {"item_id": str(item_id), "serial_numbers": ["SN-900"]},
        format="json", HTTP_IDEMPOTENCY_KEY="replacement-lot",
    ).json()["lots"][0]["id"]
    receipt = setup["client"].post(
        f"{root}/inventory/documents",
        {"document_no": str(uuid.uuid4()), "document_kind": "receipt",
         "document_date": "2026-09-27", "reason": "Replacement stock receipt",
         "lines": [{"item_id": str(item_id), "lot_id": new_lot, "quantity": "1",
                    "to_warehouse_id": other["id"], "unit_cost_company": "100"}]},
        format="json", HTTP_IDEMPOTENCY_KEY="replacement-receipt",
    )
    assert receipt.status_code == 201, receipt.json()
    posted_receipt = _command(
        setup["client"], f"{root}/inventory/documents/{receipt.json()['id']}/post",
        _posting(accounting_context) | {"offset_account_id": str(setup["posting"]["cogs"])},
        receipt.json()["row_version"],
    )
    assert posted_receipt.status_code == 200, posted_receipt.json()
    new_serial = setup["client"].get(f"{root}/inventory/serials?serial=SN-900").json()[
        "results"
    ][0]
    replacement_body = _payload(setup["ids"], str(uuid.uuid4()))
    replacement_body["lines"][0]["item_id"] = str(item_id)
    replacement_body["lines"][0]["quantity"] = "1"
    draft = setup["client"].post(f"{root}/sales/invoices", replacement_body, format="json")
    assert draft.status_code == 201, draft.json()
    calculated_replacement = _command(
        setup["client"], f"{root}/sales/invoices/{draft.json()['id']}/calculate",
        {}, draft.json()["row_version"],
    )
    assert calculated_replacement.status_code == 200, calculated_replacement.json()
    replaced = _command(
        setup["client"], f"{root}/sales/returns/{returned['id']}/replace",
        _posting(accounting_context) | {
            "journal_id": str(setup["posting"]["journal"]),
            "replacement_invoice_id": draft.json()["id"],
            "replacement_revision": calculated_replacement.json()["row_version"],
            "stock_fulfillment": "deferred",
        }, response.json()["row_version"],
    )
    assert replaced.status_code == 200, replaced.json()
    replacement_line = (
        setup["client"].get(f"{root}/sales/invoices/{draft.json()['id']}").json()["lines"][0]
    )
    replacement_shipment = setup["client"].post(
        f"{root}/inventory/documents",
        {"document_no": str(uuid.uuid4()), "document_kind": "shipment",
         "document_date": "2026-09-30", "reason": "Replacement shipment",
         "lines": [{"item_id": str(item_id), "lot_id": new_lot, "quantity": "1",
                    "from_warehouse_id": other["id"],
                    "sales_invoice_line_id": replacement_line["id"]}]},
        format="json", HTTP_IDEMPOTENCY_KEY="replacement-shipment",
    )
    assert replacement_shipment.status_code == 201, replacement_shipment.json()
    shipped_replacement = _command(
        setup["client"],
        f"{root}/inventory/documents/{replacement_shipment.json()['id']}/post",
        _posting(accounting_context), replacement_shipment.json()["row_version"],
    )
    assert shipped_replacement.status_code == 200, shipped_replacement.json()
    link = _command(
        setup["client"], f"{root}/sales/returns/{returned['id']}/replacement-serials",
        {"original_serial_id": serial["id"], "replacement_serial_id": new_serial["id"],
         "replacement_invoice_line_id": replacement_line["id"]},
        replaced.json()["row_version"],
    )
    assert link.status_code == 201, link.json()
    links = setup["client"].get(
        f"{root}/sales/returns/{returned['id']}/replacement-serials"
    )
    assert links.status_code == 200, links.json()
    assert links.json()["results"][0]["replacement_serial_id"] == new_serial["id"]
    second_return = _command(
        setup["client"], f"{root}/sales/invoices/{draft.json()['id']}/returns",
        {"return_no": str(uuid.uuid4()), "kind": "return",
         "return_date": "2026-09-30", "reason": "Replacement unit failed",
         "lines": [{"sales_invoice_line_id": replacement_line["id"], "quantity": "1"}]},
        1,
    )
    assert second_return.status_code == 201, second_return.json()
    second_inspection = _command(
        setup["client"],
        f"{root}/sales/returns/{second_return.json()['id']}/inspect",
        {"lines": [{"line_id": second_return.json()["lines"][0]["id"],
                    "received_quantity": "1", "condition": "damaged",
                    "disposition": "damaged", "warehouse_id": damaged_warehouse["id"],
                    "serial_ids": [new_serial["id"]]}]},
        second_return.json()["row_version"],
    )
    assert second_inspection.status_code == 200, second_inspection.json()
    posted_second_return = _command(
        setup["client"], f"{root}/sales/returns/{second_return.json()['id']}/post",
        _posting(accounting_context), second_inspection.json()["row_version"],
    )
    assert posted_second_return.status_code == 200, posted_second_return.json()
    third_lot, third_serial_id = _receive_serial(
        accounting_context, setup, item_id, other["id"], "SN-1500",
    )
    third_body = _payload(setup["ids"], str(uuid.uuid4()))
    third_body["issue_date"] = "2026-09-30"
    third_body["lines"][0]["item_id"] = str(item_id)
    third_body["lines"][0]["quantity"] = "1"
    third_draft = setup["client"].post(
        f"{root}/sales/invoices", third_body, format="json",
    )
    assert third_draft.status_code == 201, third_draft.json()
    third_calculated = _command(
        setup["client"], f"{root}/sales/invoices/{third_draft.json()['id']}/calculate",
        {}, third_draft.json()["row_version"],
    )
    assert third_calculated.status_code == 200, third_calculated.json()
    second_replaced = _command(
        setup["client"], f"{root}/sales/returns/{second_return.json()['id']}/replace",
        _posting(accounting_context) | {
            "journal_id": str(setup["posting"]["journal"]),
            "replacement_invoice_id": third_draft.json()["id"],
            "replacement_revision": third_calculated.json()["row_version"],
            "stock_fulfillment": "deferred",
        },
        posted_second_return.json()["row_version"],
    )
    assert second_replaced.status_code == 200, second_replaced.json()
    third_line = setup["client"].get(
        f"{root}/sales/invoices/{third_draft.json()['id']}"
    ).json()["lines"][0]
    third_shipment = setup["client"].post(
        f"{root}/inventory/documents",
        {"document_no": str(uuid.uuid4()), "document_kind": "shipment",
         "document_date": "2026-09-30", "reason": "Second-generation replacement",
         "lines": [{"item_id": str(item_id), "lot_id": third_lot,
                    "quantity": "1", "from_warehouse_id": other["id"],
                    "sales_invoice_line_id": third_line["id"]}]},
        format="json", HTTP_IDEMPOTENCY_KEY="third-shipment",
    )
    assert third_shipment.status_code == 201, third_shipment.json()
    third_shipped = _command(
        setup["client"],
        f"{root}/inventory/documents/{third_shipment.json()['id']}/post",
        _posting(accounting_context), third_shipment.json()["row_version"],
    )
    assert third_shipped.status_code == 200, third_shipped.json()
    second_link = _command(
        setup["client"],
        f"{root}/sales/returns/{second_return.json()['id']}/replacement-serials",
        {"original_serial_id": new_serial["id"], "replacement_serial_id": third_serial_id,
         "replacement_invoice_line_id": third_line["id"]},
        second_replaced.json()["row_version"],
    )
    assert second_link.status_code == 201, second_link.json()
    assert setup["client"].get(f"{root}/inventory/serials/{third_serial_id}").json()[
        "last_sales_invoice_line_id"
    ] == third_line["id"]
    scrapped = _command(
        setup["client"],
        f"{root}/sales/returns/{second_return.json()['id']}/stock-dispositions",
        _posting(accounting_context) | {
            "line_id": second_return.json()["lines"][0]["id"],
            "serial_id": new_serial["id"],
            "from_warehouse_id": damaged_warehouse["id"],
            "quantity": "1", "action_date": "2026-09-30",
            "reason": "Nonrepairable replacement unit",
            "approve_write_off": True,
            "loss_account_id": str(setup["posting"]["cogs"]),
        }, posted_second_return.json()["row_version"],
    )
    assert scrapped.status_code == 200, scrapped.json()
    assert setup["client"].get(f"{root}/inventory/serials/{new_serial['id']}").json()[
        "lifecycle_status"
    ] == "disposed"
    resale_body = _payload(setup["ids"], str(uuid.uuid4()))
    resale_body["issue_date"] = "2026-09-30"
    resale_body["lines"][0]["item_id"] = str(item_id)
    resale_body["lines"][0]["quantity"] = "1"
    resale_draft = setup["client"].post(
        f"{root}/sales/invoices", resale_body, format="json",
    )
    assert resale_draft.status_code == 201, resale_draft.json()
    resale_calculated = _command(
        setup["client"], f"{root}/sales/invoices/{resale_draft.json()['id']}/calculate",
        {}, resale_draft.json()["row_version"],
    )
    assert resale_calculated.status_code == 200, resale_calculated.json()
    resale_posted = _command(
        setup["client"], f"{root}/sales/invoices/{resale_draft.json()['id']}/post",
        _posting(accounting_context) | {
            "journal_id": str(setup["posting"]["journal"]),
            "stock_fulfillment": "deferred",
        }, resale_calculated.json()["row_version"],
    )
    assert resale_posted.status_code == 200, resale_posted.json()
    resale_line_id = resale_posted.json()["lines"][0]["id"]
    resale_shipment = setup["client"].post(
        f"{root}/inventory/documents",
        {"document_no": str(uuid.uuid4()), "document_kind": "shipment",
         "document_date": "2026-09-30", "reason": "Resell QC-passed serial",
         "lines": [{"item_id": str(item_id), "lot_id": lot_id, "quantity": "1",
                    "from_warehouse_id": other["id"],
                    "sales_invoice_line_id": resale_line_id}]},
        format="json", HTTP_IDEMPOTENCY_KEY="repaired-serial-resale",
    )
    assert resale_shipment.status_code == 201, resale_shipment.json()
    resale_shipped = _command(
        setup["client"], f"{root}/inventory/documents/{resale_shipment.json()['id']}/post",
        _posting(accounting_context), resale_shipment.json()["row_version"],
    )
    assert resale_shipped.status_code == 200, resale_shipped.json()
    assert setup["client"].get(f"{root}/inventory/serials/{serial['id']}").json()[
        "last_sales_invoice_line_id"
    ] == resale_line_id
    first_page = setup["client"].get(
        f"{root}/inventory/serials/{serial['id']}/history?limit=2"
    ).json()
    assert first_page["next_cursor"] is not None
    second_page = setup["client"].get(
        f"{root}/inventory/serials/{serial['id']}/history"
        f"?limit=2&after={first_page['next_cursor']}"
    ).json()
    assert {row["id"] for row in first_page["results"]}.isdisjoint(
        row["id"] for row in second_page["results"]
    )


def test_bulk_receipt_registers_more_than_one_thousand_serials(accounting_context: dict) -> None:
    setup = _stock_setup(accounting_context)
    item_id = _serialized_item(accounting_context, setup)
    root = _root(accounting_context)
    serial_numbers = [f"BULK-{number:05d}" for number in range(1001)]
    response = setup["client"].post(
        f"{root}/inventory/lots/serial-bulk",
        {"item_id": str(item_id), "serial_numbers": serial_numbers},
        format="json",
        HTTP_IDEMPOTENCY_KEY="serial-bulk-1001",
    )
    assert response.status_code == 201, response.json()
    lots = response.json()["lots"]
    assert len(lots) == 1001
    response = setup["client"].post(
        f"{root}/inventory/documents",
        {
            "document_no": str(uuid.uuid4()),
            "document_kind": "receipt",
            "document_date": "2026-09-27",
            "reason": "Bulk serialized receipt",
            "lines": [
                {
                    "item_id": str(item_id),
                    "lot_id": lot["id"],
                    "quantity": "1",
                    "to_warehouse_id": str(setup["warehouse"]),
                    "unit_cost_company": "1",
                }
                for lot in lots
            ],
        },
        format="json",
        HTTP_IDEMPOTENCY_KEY="serialized-receipt-1001",
    )
    assert response.status_code == 201, response.json()
    document = response.json()
    posted = _command(
        setup["client"],
        f"{root}/inventory/documents/{document['id']}/post",
        _posting(accounting_context) | {"offset_account_id": str(setup["posting"]["cogs"])},
        document["row_version"],
    )
    assert posted.status_code == 200, posted.json()
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) FROM erp.inventory_serials WHERE company_id=%s AND item_id=%s",
            [accounting_context["company"], item_id],
        )
        assert cursor.fetchone()[0] == 1001
    lookup = setup["client"].get(f"{root}/inventory/serials?serial=BULK-01000")
    assert lookup.status_code == 200 and len(lookup.json()["results"]) == 1
