import concurrent.futures
import uuid

import pytest
from django.db import close_old_connections, connection, transaction
from rest_framework.test import APIClient

from apps.sales.services import post_sales_invoice
from common.access.scopes import CompanyScope
from common.api.errors import APIError
from tests.api.test_sales_invoice_drafts_api import (
    _enable_sales,
    _payload,
    _root,
    _seed_sales_facts,
)


def _seed_posting_configuration(
    context: dict[str, object], ids: dict[str, uuid.UUID]
) -> dict[str, uuid.UUID]:
    posting = {
        name: uuid.uuid4()
        for name in ("receivable", "revenue", "tax", "inventory", "cogs", "journal")
    }
    suffix = posting["receivable"].hex[:8]
    with connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO erp.accounts(id,company_id,code,name,account_type,normal_balance)
            VALUES
              (%s,%s,%s,'Trade receivables','receivable','debit'),
              (%s,%s,%s,'Service revenue','revenue','credit'),
              (%s,%s,%s,'Output tax','liability','credit'),
              (%s,%s,%s,'Inventory','asset','debit'),
              (%s,%s,%s,'Cost of sales','cost_of_sales','debit')
            """,
            [
                posting["receivable"],
                context["company"],
                f"AR-{suffix}",
                posting["revenue"],
                context["company"],
                f"REV-{suffix}",
                posting["tax"],
                context["company"],
                f"TAX-{suffix}",
                posting["inventory"],
                context["company"],
                f"INV-{suffix}",
                posting["cogs"],
                context["company"],
                f"COGS-{suffix}",
            ],
        )
        cursor.execute(
            """
            INSERT INTO erp.accounting_posting_rules(
                company_id,event_code,role_code,account_id
            ) VALUES (%s,'sales_invoice','accounts_receivable',%s)
            """,
            [context["company"], posting["receivable"]],
        )
        cursor.execute(
            """
            INSERT INTO erp.item_accounting_profiles(
                company_id,item_id,revenue_account_id
            ) VALUES (%s,%s,%s)
            """,
            [context["company"], ids["item"], posting["revenue"]],
        )
        cursor.execute(
            """
            UPDATE erp.tax_code_components SET output_tax_account_id=%s
            WHERE company_id=%s AND id=%s
            """,
            [posting["tax"], context["company"], ids["tax_component"]],
        )
        cursor.execute(
            """
            INSERT INTO erp.company_tax_registrations(
                company_id,jurisdiction_id,tax_type_id,registration_number,valid_from
            ) VALUES (%s,%s,%s,%s,'2026-01-01')
            """,
            [
                context["company"],
                ids["jurisdiction"],
                ids["tax_type"],
                f"COMP-{suffix}",
            ],
        )
        cursor.execute(
            """
            INSERT INTO erp.partner_tax_registrations(
                company_id,partner_id,jurisdiction_id,tax_type_id,
                registration_number,valid_from,is_verified
            ) VALUES (%s,%s,%s,%s,%s,'2026-01-01',true)
            """,
            [
                context["company"],
                ids["partner"],
                ids["jurisdiction"],
                ids["tax_type"],
                f"CUST-{suffix}",
            ],
        )
        cursor.execute(
            """
            INSERT INTO erp.journals(id,company_id,code,name,journal_type)
            VALUES (%s,%s,%s,'Sales journal','sales')
            """,
            [posting["journal"], context["company"], f"SJ-{suffix}"],
        )
        cursor.execute(
            """
            INSERT INTO erp.document_sequences(
                company_id,fiscal_period_id,sequence_code,prefix,next_value,padding_length
            ) VALUES
              (%s,%s,'SALES_INVOICE','SI-',1,6),
              (%s,%s,'SALES_JOURNAL','SJ-',1,6)
            """,
            [
                context["company"],
                context["period"],
                context["company"],
                context["period"],
            ],
        )
    return posting


def _create_calculated_invoice(
    client: APIClient,
    context: dict[str, object],
    ids: dict[str, uuid.UUID],
    invoice_no: str,
) -> dict[str, object]:
    url = f"{_root(context)}/sales/invoices"
    created = client.post(url, _payload(ids, invoice_no), format="json")
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


def _change_module(
    client: APIClient, context: dict[str, object], module_code: str, mode: str
) -> None:
    root = _root(context)
    modules = client.get(f"{root}/admin/modules")
    assert modules.status_code == 200, modules.json()
    response = client.post(
        f"{root}/admin/module-changes",
        {
            "changes": [{"module_code": module_code, "mode": mode}],
            "reason": f"P5.5 set {module_code} to {mode}",
        },
        format="json",
        HTTP_IDEMPOTENCY_KEY=f"p55-{module_code}-{mode}-{uuid.uuid4()}",
        HTTP_IF_MATCH=f'"{modules.json()["policy_revision"]}"',
    )
    assert response.status_code == 200, response.json()


@pytest.mark.api
@pytest.mark.p0
@pytest.mark.django_db(transaction=True)
def test_post_invoice_is_atomic_idempotent_and_creates_linked_credit_reversal(
    accounting_context: dict[str, object],
) -> None:
    client = accounting_context["client"]
    assert isinstance(client, APIClient)
    _enable_sales(client, accounting_context)
    ids = _seed_sales_facts(accounting_context)
    posting = _seed_posting_configuration(accounting_context, ids)
    invoices_url = f"{_root(accounting_context)}/sales/invoices"
    draft = _create_calculated_invoice(client, accounting_context, ids, "DRAFT-P54-ORIGINAL")
    post_url = f"{invoices_url}/{draft['id']}/post"
    headers = {"HTTP_IF_MATCH": '"2"', "HTTP_IDEMPOTENCY_KEY": "post-original-p55"}

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
    assert original["invoice_no"] == "SI-000001"
    assert original["journal_entry_id"]
    component = original["lines"][0]["tax_components"][0]
    assert component["output_tax_account_id_snapshot"] == str(posting["tax"])
    assert component["account_role_snapshot"] == "output_tax"
    assert component["polarity_snapshot"] == "credit"

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
            WHERE company_id=%s AND operation='sales.invoice.post'
              AND resource_key=%s AND status='completed'
            """,
            [accounting_context["company"], original["id"]],
        )
        assert cursor.fetchone()[0] == 1
        cursor.execute(
            """
            SELECT count(*) FROM erp.outbox_events
            WHERE company_id=%s AND aggregate_id=%s
              AND event_type='sales.invoice.posted'
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
            WHERE company_id=%s AND object_type='sales_invoice' AND object_id=%s
              AND action='sales_invoice.posted'
            """,
            [accounting_context["company"], original["id"]],
        )
        assert cursor.fetchone()[0] == 1

    credit = client.post(
        f"{invoices_url}/{original['id']}/credit-notes",
        {
            "invoice_no": "DRAFT-P55-CREDIT",
            "issue_date": "2026-09-28",
            "source_line_ids": [original["lines"][0]["id"]],
        },
        format="json",
    )
    assert credit.status_code == 201, credit.json()
    credit_draft = credit.json()
    assert credit_draft["document_kind"] == "credit_note"
    assert credit_draft["credit_of_invoice_id"] == original["id"]
    assert credit_draft["lines"][0]["credit_of_invoice_line_id"] == original["lines"][0]["id"]

    duplicate_credit = client.post(
        f"{invoices_url}/{original['id']}/credit-notes",
        {
            "invoice_no": "DRAFT-P55-DUPLICATE",
            "issue_date": "2026-09-28",
            "source_line_ids": [original["lines"][0]["id"]],
        },
        format="json",
    )
    assert duplicate_credit.status_code == 409
    assert duplicate_credit.json()["error"]["code"] == "INVOICE_LINE_ALREADY_CREDITED"

    credit_posted = client.post(
        f"{invoices_url}/{credit_draft['id']}/post",
        _post_payload(accounting_context, posting),
        format="json",
        HTTP_IF_MATCH='"1"',
        HTTP_IDEMPOTENCY_KEY="post-credit-p55",
    )
    assert credit_posted.status_code == 200, credit_posted.json()
    correction = credit_posted.json()
    assert correction["status"] == "posted"
    assert correction["lines"][0]["tax_amount"] == original["lines"][0]["tax_amount"]
    assert correction["lines"][0]["tax_components"][0]["polarity_snapshot"] == "debit"
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
    assert credit_lines[0][0] == "receivable"
    assert credit_lines[0][1] == 0
    assert credit_lines[0][2] > 0
    assert all(row[1] > 0 for row in credit_lines[1:])


@pytest.mark.api
@pytest.mark.p0
@pytest.mark.django_db(transaction=True)
def test_post_rejects_missing_registration_without_partial_effects(
    accounting_context: dict[str, object],
) -> None:
    client = accounting_context["client"]
    assert isinstance(client, APIClient)
    _enable_sales(client, accounting_context)
    ids = _seed_sales_facts(accounting_context)
    posting = _seed_posting_configuration(accounting_context, ids)
    draft = _create_calculated_invoice(client, accounting_context, ids, "DRAFT-P55-TAX")
    with connection.cursor() as cursor:
        cursor.execute(
            "DELETE FROM erp.partner_tax_registrations WHERE company_id=%s AND partner_id=%s",
            [accounting_context["company"], ids["partner"]],
        )
    response = client.post(
        f"{_root(accounting_context)}/sales/invoices/{draft['id']}/post",
        _post_payload(accounting_context, posting),
        format="json",
        HTTP_IF_MATCH='"2"',
        HTTP_IDEMPOTENCY_KEY="post-tax-invalid-p55",
    )
    assert response.status_code == 409, response.json()
    assert response.json()["error"]["code"] == "TAX_REGISTRATION_REQUIRED"
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT status,journal_entry_id FROM erp.sales_invoices WHERE id=%s",
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
def test_post_denied_in_sales_read_only_mode_without_effects(
    accounting_context: dict[str, object],
) -> None:
    client = accounting_context["client"]
    assert isinstance(client, APIClient)
    _enable_sales(client, accounting_context)
    ids = _seed_sales_facts(accounting_context)
    posting = _seed_posting_configuration(accounting_context, ids)
    draft = _create_calculated_invoice(client, accounting_context, ids, "DRAFT-P55-READONLY")
    _change_module(client, accounting_context, "sales", "read_only")
    response = client.post(
        f"{_root(accounting_context)}/sales/invoices/{draft['id']}/post",
        _post_payload(accounting_context, posting),
        format="json",
        HTTP_IF_MATCH='"2"',
        HTTP_IDEMPOTENCY_KEY="post-readonly-p55",
    )
    assert response.status_code == 403, response.json()
    assert response.json()["error"]["code"] == "MODULE_WRITE_DENIED"
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT status,journal_entry_id FROM erp.sales_invoices WHERE id=%s",
            [draft["id"]],
        )
        assert cursor.fetchone() == ("draft", None)


def _bind_inventory_license(context: dict[str, object]) -> None:
    plan_id = uuid.uuid4()
    version_id = uuid.uuid4()
    license_id = uuid.uuid4()
    term_id = uuid.uuid4()
    suffix = plan_id.hex[:10]
    with connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO licensing.plans(id,product_id,plan_code,name)
            VALUES (%s,%s,%s,'Sales and inventory')
            """,
            [plan_id, context["product"], f"sales-inventory-{suffix}"],
        )
        cursor.execute(
            """
            INSERT INTO licensing.plan_versions(
                id,product_id,plan_id,version_number,term_unit,max_activations,
                price_currency,price_amount
            ) VALUES (%s,%s,%s,1,'lifetime',1,'USD',50)
            """,
            [version_id, context["product"], plan_id],
        )
        cursor.execute(
            """
            INSERT INTO licensing.plan_features(plan_version_id,feature_code,is_enabled)
            VALUES (%s,'module.sales',true),(%s,'module.inventory',true)
            """,
            [version_id, version_id],
        )
        cursor.execute(
            "UPDATE licensing.plan_versions SET published_at=clock_timestamp() WHERE id=%s",
            [version_id],
        )
        cursor.execute(
            """
            INSERT INTO licensing.licenses(
                id,tenant_id,product_id,license_number_hash,license_number_last4,status
            ) VALUES (%s,%s,%s,%s,'5678','active')
            """,
            [license_id, context["tenant"], context["product"], uuid.uuid4().bytes * 2],
        )
        cursor.execute(
            """
            INSERT INTO licensing.license_terms(
                id,tenant_id,license_id,plan_version_id,term_unit_snapshot,
                starts_at,max_activations_snapshot
            ) VALUES (%s,%s,%s,%s,'lifetime',clock_timestamp()-interval '1 day',1)
            """,
            [term_id, context["tenant"], license_id, version_id],
        )
        cursor.execute(
            """
            UPDATE licensing.tenant_product_bindings SET license_id=%s
            WHERE tenant_id=%s AND product_id=%s
            """,
            [license_id, context["tenant"], context["product"]],
        )


@pytest.mark.api
@pytest.mark.p1
@pytest.mark.django_db(transaction=True)
def test_stock_invoice_requires_inventory_and_posts_cost_effects(
    accounting_context: dict[str, object],
) -> None:
    client = accounting_context["client"]
    assert isinstance(client, APIClient)
    _enable_sales(client, accounting_context)
    ids = _seed_sales_facts(accounting_context)
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
                company_id,item_id,inventory_account_id,revenue_account_id,cogs_account_id
            ) VALUES (%s,%s,%s,%s,%s)
            """,
            [
                accounting_context["company"],
                stock_item,
                posting["inventory"],
                posting["revenue"],
                posting["cogs"],
            ],
        )
        cursor.execute(
            """
            INSERT INTO erp.warehouses(id,company_id,code,name)
            VALUES (%s,%s,%s,'Main warehouse')
            """,
            [warehouse, accounting_context["company"], f"WH-{suffix}"],
        )
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT set_config('app.tenant_id', %s, true)",
            [str(accounting_context["tenant"])],
        )
        cursor.execute(
            """
            INSERT INTO erp.stock_movements(
                company_id,event_key,occurred_at,warehouse_id,item_id,movement_kind,
                quantity_delta,unit_cost_company,value_delta_company,source_type,source_id
            ) VALUES (%s,%s,clock_timestamp(),%s,%s,'receipt',10,100,1000,
                      'opening_stock',%s)
            """,
            [
                accounting_context["company"],
                f"opening-stock-{stock_item}",
                warehouse,
                stock_item,
                uuid.uuid4(),
            ],
        )
    payload = _payload(ids, "DRAFT-P55-STOCK")
    payload["lines"][0]["item_id"] = str(stock_item)  # type: ignore[index]
    invoices_url = f"{_root(accounting_context)}/sales/invoices"
    created = client.post(invoices_url, payload, format="json")
    assert created.status_code == 201, created.json()
    calculated = client.post(
        f"{invoices_url}/{created.json()['id']}/calculate",
        format="json",
        HTTP_IF_MATCH='"1"',
    )
    assert calculated.status_code == 200, calculated.json()
    denied = client.post(
        f"{invoices_url}/{created.json()['id']}/post",
        _post_payload(accounting_context, posting),
        format="json",
        HTTP_IF_MATCH='"2"',
        HTTP_IDEMPOTENCY_KEY="stock-without-warehouse-p55",
    )
    assert denied.status_code == 409
    assert denied.json()["error"]["code"] == "STOCK_FULFILLMENT_REQUIRED"

    _bind_inventory_license(accounting_context)
    _change_module(client, accounting_context, "inventory", "enabled")
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT on_hand_quantity,reserved_quantity,value_company
            FROM erp.inventory_positions
            WHERE company_id=%s AND warehouse_id=%s AND item_id=%s AND lot_id IS NULL
            """,
            [accounting_context["company"], warehouse, stock_item],
        )
        assert cursor.fetchone() == (10, 0, 1000)
    post_payload = {
        **_post_payload(accounting_context, posting),
        "warehouse_id": str(warehouse),
    }
    posted = client.post(
        f"{invoices_url}/{created.json()['id']}/post",
        post_payload,
        format="json",
        HTTP_IF_MATCH='"2"',
        HTTP_IDEMPOTENCY_KEY="stock-with-inventory-p55",
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
        assert cursor.fetchone() == (-2, 100, -200)
        cursor.execute(
            """
            SELECT on_hand_quantity,value_company FROM erp.inventory_positions
            WHERE company_id=%s AND warehouse_id=%s AND item_id=%s
            """,
            [accounting_context["company"], warehouse, stock_item],
        )
        assert cursor.fetchone() == (8, 800)


@pytest.mark.concurrency
@pytest.mark.p0
@pytest.mark.django_db(transaction=True)
def test_two_posting_keys_create_exactly_one_invoice_journal(
    accounting_context: dict[str, object],
) -> None:
    client = accounting_context["client"]
    assert isinstance(client, APIClient)
    _enable_sales(client, accounting_context)
    ids = _seed_sales_facts(accounting_context)
    posting = _seed_posting_configuration(accounting_context, ids)
    draft = _create_calculated_invoice(client, accounting_context, ids, "DRAFT-P55-RACE")
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
            post_sales_invoice(
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
    assert results.count("INVOICE_NOT_DRAFT") == 1
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) FROM erp.journal_entries WHERE source_id=%s AND status='posted'",
            [draft["id"]],
        )
        assert cursor.fetchone()[0] == 1
