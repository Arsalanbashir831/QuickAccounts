import concurrent.futures
import uuid

import pytest
from django.db import close_old_connections, connection
from rest_framework.test import APIClient

from apps.purchasing.services import post_purchase_bill
from common.access.scopes import CompanyScope
from common.api.errors import APIError
from tests.api.test_purchase_bill_drafts_api import (
    _bind_license,
    _change_module,
    _enable_purchasing,
    _payload,
    _root,
    _seed_posting_configuration,
    _seed_purchase_facts,
)


def _create_calculated_bill(
    client: APIClient,
    context: dict[str, object],
    ids: dict[str, uuid.UUID],
    bill_no: str,
) -> dict[str, object]:
    url = f"{_root(context)}/purchasing/bills"
    created = client.post(url, _payload(ids, bill_no), format="json")
    assert created.status_code == 201, created.json()
    calculated = client.post(
        f"{url}/{created.json()['id']}/calculate",
        format="json",
        HTTP_IF_MATCH='"1"',
    )
    assert calculated.status_code == 200, calculated.json()
    return calculated.json()


def _post_payload(context: dict[str, object], posting: dict[str, uuid.UUID]) -> dict[str, str]:
    return {
        "fiscal_period_id": str(context["period"]),
        "journal_id": str(posting["journal"]),
    }


@pytest.mark.api
@pytest.mark.p0
@pytest.mark.django_db(transaction=True)
def test_post_bill_is_atomic_idempotent_and_creates_linked_supplier_credit(
    accounting_context: dict[str, object],
) -> None:
    client = accounting_context["client"]
    assert isinstance(client, APIClient)
    _enable_purchasing(client, accounting_context)
    ids = _seed_purchase_facts(accounting_context)
    posting = _seed_posting_configuration(accounting_context, ids)
    bills_url = f"{_root(accounting_context)}/purchasing/bills"
    draft = _create_calculated_bill(client, accounting_context, ids, "DRAFT-P56-ORIGINAL")
    post_url = f"{bills_url}/{draft['id']}/post"
    headers = {"HTTP_IF_MATCH": '"2"', "HTTP_IDEMPOTENCY_KEY": "post-original-p56"}

    posted = client.post(
        post_url,
        _post_payload(accounting_context, posting),
        format="json",
        **headers,
    )
    replay = client.post(
        post_url,
        _post_payload(accounting_context, posting),
        format="json",
        **headers,
    )
    assert posted.status_code == 200, posted.json()
    assert replay.status_code == 200, replay.json()
    assert replay.json() == posted.json()
    original = posted.json()
    assert original["status"] == "posted"
    assert original["bill_no"] == "PB-000001"
    assert original["journal_entry_id"]
    component = original["lines"][0]["tax_components"][0]
    assert component["input_tax_account_id_snapshot"] == str(posting["tax"])
    assert component["account_role_snapshot"] == "input_tax"
    assert component["polarity_snapshot"] == "debit"

    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT status FROM erp.journal_entries
            WHERE company_id=%s AND id=%s
            """,
            [accounting_context["company"], original["journal_entry_id"]],
        )
        assert cursor.fetchone() == ("posted",)
        cursor.execute(
            """
            SELECT sum(debit_amount),sum(credit_amount)
            FROM erp.journal_lines WHERE company_id=%s AND journal_entry_id=%s
            """,
            [accounting_context["company"], original["journal_entry_id"]],
        )
        debit, credit = cursor.fetchone()
        assert debit == credit
        cursor.execute(
            """
            SELECT count(*) FROM erp.api_command_receipts
            WHERE company_id=%s AND operation='purchasing.bill.post'
              AND resource_key=%s AND status='completed'
            """,
            [accounting_context["company"], original["id"]],
        )
        assert cursor.fetchone()[0] == 1
        cursor.execute(
            """
            SELECT count(*) FROM erp.outbox_events
            WHERE company_id=%s AND aggregate_id=%s
              AND event_type='purchasing.bill.posted'
            """,
            [accounting_context["company"], original["id"]],
        )
        assert cursor.fetchone()[0] == 1
        cursor.execute(
            """
            SELECT count(*) FROM erp.stock_movements
            WHERE company_id=%s AND source_id=%s
            """,
            [accounting_context["company"], original["id"]],
        )
        assert cursor.fetchone()[0] == 0
        cursor.execute(
            """
            SELECT count(*) FROM erp.company_audit_events
            WHERE company_id=%s AND object_type='purchase_bill' AND object_id=%s
              AND action='purchase_bill.posted'
            """,
            [accounting_context["company"], original["id"]],
        )
        assert cursor.fetchone()[0] == 1

    credit = client.post(
        f"{bills_url}/{original['id']}/credit-notes",
        {
            "bill_no": "DRAFT-P56-CREDIT",
            "bill_date": "2026-09-28",
            "source_line_ids": [original["lines"][0]["id"]],
        },
        format="json",
    )
    assert credit.status_code == 201, credit.json()
    credit_draft = credit.json()
    assert credit_draft["document_kind"] == "supplier_credit"
    assert credit_draft["credit_of_bill_id"] == original["id"]
    assert credit_draft["lines"][0]["credit_of_bill_line_id"] == original["lines"][0]["id"]

    duplicate_credit = client.post(
        f"{bills_url}/{original['id']}/credit-notes",
        {
            "bill_no": "DRAFT-P56-DUPLICATE",
            "bill_date": "2026-09-28",
            "source_line_ids": [original["lines"][0]["id"]],
        },
        format="json",
    )
    assert duplicate_credit.status_code == 409
    assert duplicate_credit.json()["error"]["code"] == "BILL_LINE_ALREADY_CREDITED"

    credit_posted = client.post(
        f"{bills_url}/{credit_draft['id']}/post",
        _post_payload(accounting_context, posting),
        format="json",
        HTTP_IF_MATCH='"1"',
        HTTP_IDEMPOTENCY_KEY="post-credit-p56",
    )
    assert credit_posted.status_code == 200, credit_posted.json()
    correction = credit_posted.json()
    assert correction["status"] == "posted"
    assert correction["lines"][0]["tax_amount"] == original["lines"][0]["tax_amount"]
    assert correction["lines"][0]["tax_components"][0]["polarity_snapshot"] == "credit"
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT a.account_type,l.transaction_debit,l.transaction_credit
            FROM erp.journal_lines l
            JOIN erp.accounts a ON a.company_id=l.company_id AND a.id=l.account_id
            WHERE l.company_id=%s AND l.journal_entry_id=%s
            ORDER BY l.line_no
            """,
            [accounting_context["company"], correction["journal_entry_id"]],
        )
        credit_lines = cursor.fetchall()
    assert credit_lines[0][0] == "payable"
    assert credit_lines[0][1] > 0
    assert credit_lines[0][2] == 0
    assert all(row[2] > 0 for row in credit_lines[1:])


@pytest.mark.api
@pytest.mark.p0
@pytest.mark.django_db(transaction=True)
def test_post_rejects_missing_registration_without_partial_effects(
    accounting_context: dict[str, object],
) -> None:
    client = accounting_context["client"]
    assert isinstance(client, APIClient)
    _enable_purchasing(client, accounting_context)
    ids = _seed_purchase_facts(accounting_context)
    posting = _seed_posting_configuration(accounting_context, ids)
    draft = _create_calculated_bill(client, accounting_context, ids, "DRAFT-P56-TAX")
    with connection.cursor() as cursor:
        cursor.execute(
            "DELETE FROM erp.partner_tax_registrations WHERE company_id=%s AND partner_id=%s",
            [accounting_context["company"], ids["partner"]],
        )
    response = client.post(
        f"{_root(accounting_context)}/purchasing/bills/{draft['id']}/post",
        _post_payload(accounting_context, posting),
        format="json",
        HTTP_IF_MATCH='"2"',
        HTTP_IDEMPOTENCY_KEY="post-tax-invalid-p56",
    )
    assert response.status_code == 409, response.json()
    assert response.json()["error"]["code"] == "TAX_REGISTRATION_REQUIRED"
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT status,journal_entry_id FROM erp.purchase_bills WHERE id=%s",
            [draft["id"]],
        )
        assert cursor.fetchone() == ("draft", None)
        cursor.execute(
            "SELECT count(*) FROM erp.journal_entries WHERE source_id=%s",
            [draft["id"]],
        )
        assert cursor.fetchone()[0] == 0


@pytest.mark.api
@pytest.mark.p0
@pytest.mark.django_db(transaction=True)
def test_post_allocates_partial_recoverable_tax_correctly(
    accounting_context: dict[str, object],
) -> None:
    client = accounting_context["client"]
    assert isinstance(client, APIClient)
    _enable_purchasing(client, accounting_context)
    ids = _seed_purchase_facts(accounting_context)
    posting = _seed_posting_configuration(accounting_context, ids)
    with connection.cursor() as cursor:
        cursor.execute(
            "UPDATE erp.tax_rate_versions SET recovery_percent=50 WHERE id=%s",
            [ids["rate_version"]],
        )
    bill = _create_calculated_bill(client, accounting_context, ids, "DRAFT-P56-RECOVERY")
    posted = client.post(
        f"{_root(accounting_context)}/purchasing/bills/{bill['id']}/post",
        _post_payload(accounting_context, posting),
        format="json",
        HTTP_IF_MATCH='"2"',
        HTTP_IDEMPOTENCY_KEY="post-partial-recovery-p56",
    )
    assert posted.status_code == 200, posted.json()
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT a.account_type,l.debit_amount,l.credit_amount
            FROM erp.journal_lines l
            JOIN erp.accounts a ON a.company_id=l.company_id AND a.id=l.account_id
            WHERE l.company_id=%s AND l.journal_entry_id=%s
            ORDER BY l.line_no
            """,
            [accounting_context["company"], posted.json()["journal_entry_id"]],
        )
        lines = cursor.fetchall()
    assert lines[0][0] == "payable"
    assert lines[0][2] == 110
    assert (lines[1][0], lines[1][1], lines[1][2]) == ("expense", 105, 0)
    assert (lines[2][0], lines[2][1], lines[2][2]) == ("asset", 5, 0)


@pytest.mark.api
@pytest.mark.p0
@pytest.mark.django_db(transaction=True)
def test_post_denied_in_purchasing_read_only_mode_without_effects(
    accounting_context: dict[str, object],
) -> None:
    client = accounting_context["client"]
    assert isinstance(client, APIClient)
    _enable_purchasing(client, accounting_context)
    ids = _seed_purchase_facts(accounting_context)
    posting = _seed_posting_configuration(accounting_context, ids)
    draft = _create_calculated_bill(client, accounting_context, ids, "DRAFT-P56-READONLY")
    _change_module(client, accounting_context, "purchasing", "read_only")
    response = client.post(
        f"{_root(accounting_context)}/purchasing/bills/{draft['id']}/post",
        _post_payload(accounting_context, posting),
        format="json",
        HTTP_IF_MATCH='"2"',
        HTTP_IDEMPOTENCY_KEY="post-readonly-p56",
    )
    assert response.status_code == 403, response.json()
    assert response.json()["error"]["code"] == "MODULE_WRITE_DENIED"
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT status,journal_entry_id FROM erp.purchase_bills WHERE id=%s",
            [draft["id"]],
        )
        assert cursor.fetchone() == ("draft", None)


@pytest.mark.api
@pytest.mark.p1
@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("adopted", [False, True])
def test_stock_bill_requires_inventory_and_posts_receipt_effects(
    accounting_context: dict[str, object],
    adopted: bool,
) -> None:
    client = accounting_context["client"]
    assert isinstance(client, APIClient)
    _enable_purchasing(client, accounting_context)
    ids = _seed_purchase_facts(accounting_context)
    posting = _seed_posting_configuration(accounting_context, ids)
    stock_item = uuid.uuid4()
    warehouse = uuid.uuid4()
    suffix = stock_item.hex[:8]
    with connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO erp.items(id,company_id,sku,name,item_kind,base_uom_id)
            VALUES (%s,%s,%s,'Untracked stock item','stock',%s)
            """,
            [stock_item, accounting_context["company"], f"STK-{suffix}", ids["uom"]],
        )
        cursor.execute(
            """
            INSERT INTO erp.item_accounting_profiles(
                company_id,item_id,purchase_account_id,inventory_account_id
            ) VALUES (%s,%s,%s,%s)
            """,
            [
                accounting_context["company"],
                stock_item,
                posting["purchase"],
                posting["inventory"],
            ],
        )
        cursor.execute(
            """
            INSERT INTO erp.warehouses(id,company_id,code,name)
            VALUES (%s,%s,%s,'Main warehouse')
            """,
            [warehouse, accounting_context["company"], f"WH-{suffix}"],
        )
    payload = _payload(ids, "DRAFT-P56-STOCK")
    payload["lines"][0]["item_id"] = str(stock_item)  # type: ignore[index]
    bills_url = f"{_root(accounting_context)}/purchasing/bills"
    created = client.post(bills_url, payload, format="json")
    assert created.status_code == 201, created.json()
    calculated = client.post(
        f"{bills_url}/{created.json()['id']}/calculate",
        format="json",
        HTTP_IF_MATCH='"1"',
    )
    assert calculated.status_code == 200, calculated.json()
    denied = client.post(
        f"{bills_url}/{created.json()['id']}/post",
        _post_payload(accounting_context, posting),
        format="json",
        HTTP_IF_MATCH='"2"',
        HTTP_IDEMPOTENCY_KEY="stock-without-warehouse-p56",
    )
    assert denied.status_code == 409
    assert denied.json()["error"]["code"] == "STOCK_RECEIPT_WAREHOUSE_REQUIRED"

    _bind_license(accounting_context, ("module.purchasing", "module.inventory"))
    _change_module(client, accounting_context, "inventory", "enabled")
    post_payload = {
        **_post_payload(accounting_context, posting),
        "warehouse_id": str(warehouse),
    }
    posted = client.post(
        f"{bills_url}/{created.json()['id']}/post",
        post_payload,
        format="json",
        HTTP_IF_MATCH='"2"',
        HTTP_IDEMPOTENCY_KEY="stock-with-inventory-p56",
    )
    assert posted.status_code == 200, posted.json()
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT quantity_delta,unit_cost_company,value_delta_company
            FROM erp.stock_movements
            WHERE company_id=%s AND source_id=%s
            """,
            [accounting_context["company"], created.json()["id"]],
        )
        assert cursor.fetchone() == (2, 50, 100)
        cursor.execute(
            """
            SELECT on_hand_quantity,value_company FROM erp.inventory_positions
            WHERE company_id=%s AND warehouse_id=%s AND item_id=%s
            """,
            [accounting_context["company"], warehouse, stock_item],
        )
        assert cursor.fetchone() == (2, 100)

    if adopted:
        from apps.inventory.cost_checkpoints import create_cost_checkpoint

        create_cost_checkpoint(
            CompanyScope(
                tenant_id=accounting_context["tenant"],
                company_id=accounting_context["company"],
                user_id=accounting_context["user"],
            ),
            {"warehouse_id": warehouse, "item_id": stock_item, "reason": "Reviewed purchase stock"},
            key=str(uuid.uuid4()),
        )
    credit = client.post(
        f"{bills_url}/{posted.json()['id']}/credit-notes",
        {
            "bill_no": "DRAFT-P56-STOCK-CREDIT",
            "bill_date": "2026-09-28",
            "source_line_ids": [posted.json()["lines"][0]["id"]],
        },
        format="json",
    )
    assert credit.status_code == 201, credit.json()
    blocked_credit = client.post(
        f"{bills_url}/{credit.json()['id']}/post",
        _post_payload(accounting_context, posting),
        format="json",
        HTTP_IF_MATCH='"1"',
        HTTP_IDEMPOTENCY_KEY="post-stock-credit-p56",
    )
    assert blocked_credit.status_code == 409, blocked_credit.json()
    assert blocked_credit.json()["error"]["code"] == "SUPPLIER_RETURN_WAREHOUSE_REQUIRED"
    credit_after = client.get(f"{bills_url}/{credit.json()['id']}")
    assert credit_after.status_code == 200, credit_after.json()
    assert credit_after.json()["status"] == "draft"
    physical = client.post(
        f"{bills_url}/{credit.json()['id']}/post",
        post_payload,
        format="json",
        HTTP_IF_MATCH='"1"',
        HTTP_IDEMPOTENCY_KEY="physical-supplier-return",
    )
    assert physical.status_code == 200, physical.json()
    assert (
        client.post(
            f"{bills_url}/{credit.json()['id']}/post",
            post_payload,
            format="json",
            HTTP_IF_MATCH='"1"',
            HTTP_IDEMPOTENCY_KEY="physical-supplier-return",
        ).json()
        == physical.json()
    )
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT quantity_delta,value_delta_company FROM erp.stock_movements "
            "WHERE company_id=%s AND source_id=%s",
            [accounting_context["company"], credit.json()["id"]],
        )
        assert cursor.fetchone() == (-2, -100)
        cursor.execute(
            "SELECT on_hand_quantity,value_company FROM erp.inventory_positions "
            "WHERE company_id=%s AND warehouse_id=%s AND item_id=%s",
            [accounting_context["company"], warehouse, stock_item],
        )
        assert cursor.fetchone() == (0, 0)


@pytest.mark.concurrency
@pytest.mark.p0
@pytest.mark.django_db(transaction=True)
def test_two_posting_keys_create_exactly_one_bill_journal(
    accounting_context: dict[str, object],
) -> None:
    client = accounting_context["client"]
    assert isinstance(client, APIClient)
    _enable_purchasing(client, accounting_context)
    ids = _seed_purchase_facts(accounting_context)
    posting = _seed_posting_configuration(accounting_context, ids)
    draft = _create_calculated_bill(client, accounting_context, ids, "DRAFT-P56-RACE")
    scope = CompanyScope(
        tenant_id=accounting_context["tenant"],  # type: ignore[arg-type]
        company_id=accounting_context["company"],  # type: ignore[arg-type]
        user_id=accounting_context["user"],  # type: ignore[arg-type]
    )
    command = {
        "fiscal_period_id": accounting_context["period"],
        "journal_id": posting["journal"],
    }

    def worker(key: str) -> str:
        close_old_connections()
        try:
            post_purchase_bill(
                scope,
                uuid.UUID(str(draft["id"])),
                command,
                expected_revision=2,
                idempotency_key=key,
                request_id=key,
            )
            return "posted"
        except APIError as exc:
            return str(exc.default_code)
        finally:
            close_old_connections()

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(worker, ["race-post-a", "race-post-b"]))
    assert results.count("posted") == 1
    assert results.count("BILL_NOT_DRAFT") == 1
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) FROM erp.journal_entries WHERE source_id=%s AND status='posted'",
            [draft["id"]],
        )
        assert cursor.fetchone()[0] == 1
