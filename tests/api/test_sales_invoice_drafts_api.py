import uuid

import pytest
from django.db import connection
from rest_framework.test import APIClient


def _root(context: dict[str, object]) -> str:
    return f"/api/v1/tenants/{context['tenant']}/companies/{context['company']}"


def _enable_sales(client: APIClient, context: dict[str, object]) -> None:
    root = _root(context)
    modules = client.get(f"{root}/admin/modules")
    assert modules.status_code == 200, modules.json()
    revision = modules.json()["policy_revision"]
    response = client.post(
        f"{root}/admin/module-changes",
        {
            "changes": [{"module_code": "sales", "mode": "enabled"}],
            "reason": "P5.4 sales invoice verification",
        },
        format="json",
        HTTP_IDEMPOTENCY_KEY=f"enable-sales-{uuid.uuid4()}",
        HTTP_IF_MATCH=f'"{revision}"',
    )
    assert response.status_code == 200, response.json()


def _seed_sales_facts(context: dict[str, object]) -> dict[str, uuid.UUID]:
    ids = {
        name: uuid.uuid4()
        for name in (
            "partner",
            "address",
            "uom_category",
            "uom",
            "item",
            "jurisdiction",
            "tax_type",
            "schedule",
            "rate_version",
            "tax_code",
            "tax_component",
        )
    }
    suffix = ids["partner"].hex[:10]
    with connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO erp.business_partners(
                id,company_id,partner_code,display_name,partner_kind
            ) VALUES (%s,%s,%s,'Draft customer','customer')
            """,
            [ids["partner"], context["company"], f"CUS-{suffix}"],
        )
        cursor.execute(
            """
            INSERT INTO erp.partner_addresses(
                id,company_id,partner_id,address_kind,line_1,city,country_code,is_default
            ) VALUES (%s,%s,%s,'billing','10 Calculation Road','Karachi','PK',true)
            """,
            [ids["address"], context["company"], ids["partner"]],
        )
        cursor.execute(
            "INSERT INTO erp.uom_categories(id,code,name) VALUES (%s,%s,'Quantity')",
            [ids["uom_category"], f"QTY-{suffix}"],
        )
        cursor.execute(
            "INSERT INTO erp.uoms(id,category_id,code,name,to_base_factor) "
            "VALUES (%s,%s,%s,'Each',1)",
            [ids["uom"], ids["uom_category"], f"EA-{suffix}"],
        )
        cursor.execute(
            """
            INSERT INTO erp.items(id,company_id,sku,name,item_kind,base_uom_id)
            VALUES (%s,%s,%s,'Consulting service','service',%s)
            """,
            [ids["item"], context["company"], f"SRV-{suffix}", ids["uom"]],
        )
        cursor.execute(
            """
            INSERT INTO erp.tax_jurisdictions(
                id,country_code,jurisdiction_code,name,jurisdiction_level
            ) VALUES (%s,'PK',%s,'Test jurisdiction','country')
            """,
            [ids["jurisdiction"], f"TEST-{suffix}"],
        )
        cursor.execute(
            """
            INSERT INTO erp.tax_types(
                id,jurisdiction_id,code,name,tax_family,calculation_stage,tax_direction
            ) VALUES (%s,%s,%s,'Test output tax','sales','line','output')
            """,
            [ids["tax_type"], ids["jurisdiction"], f"OUT-{suffix}"],
        )
        cursor.execute(
            """
            INSERT INTO erp.tax_rate_schedules(
                id,jurisdiction_id,tax_type_id,schedule_code,name
            ) VALUES (%s,%s,%s,%s,'Test ten percent')
            """,
            [ids["schedule"], ids["jurisdiction"], ids["tax_type"], f"TEN-{suffix}"],
        )
        cursor.execute(
            """
            INSERT INTO erp.tax_rate_versions(
                id,jurisdiction_id,rate_schedule_id,version_code,valid_from,
                calculation_method,calculation_base,rate,recovery_percent
            ) VALUES (%s,%s,%s,'2026-v1','2026-01-01','percentage','net_amount',10,100)
            """,
            [ids["rate_version"], ids["jurisdiction"], ids["schedule"]],
        )
        cursor.execute(
            """
            INSERT INTO erp.tax_codes(
                id,company_id,tax_jurisdiction_id,code,name,tax_scope,is_tax_inclusive
            ) VALUES (%s,%s,%s,%s,'Test sales tax','sales',false)
            """,
            [ids["tax_code"], context["company"], ids["jurisdiction"], f"TAX-{suffix}"],
        )
        cursor.execute(
            """
            INSERT INTO erp.tax_code_components(
                id,company_id,tax_jurisdiction_id,tax_code_id,rate_schedule_id,sequence_no
            ) VALUES (%s,%s,%s,%s,%s,1)
            """,
            [
                ids["tax_component"],
                context["company"],
                ids["jurisdiction"],
                ids["tax_code"],
                ids["schedule"],
            ],
        )
    return ids


def _payload(ids: dict[str, uuid.UUID], invoice_no: str) -> dict[str, object]:
    return {
        "invoice_no": invoice_no,
        "partner_id": str(ids["partner"]),
        "issue_date": "2026-09-27",
        "tax_point_date": "2026-09-27",
        "tax_jurisdiction_id": str(ids["jurisdiction"]),
        "due_date": "2026-10-27",
        "currency_code": "USD",
        "addresses": [
            {
                "address_kind": "billing",
                "source_address_id": str(ids["address"]),
                "recipient_name": "Draft customer accounts",
                "tax_registration_no": "TEST-REG-1",
            }
        ],
        "lines": [
            {
                "item_id": str(ids["item"]),
                "description": "Deterministic consulting",
                "quantity": "2.000000",
                "unit_price": "50.000000",
                "discount_amount": "5.000000",
                "tax_code_id": str(ids["tax_code"]),
            }
        ],
    }


@pytest.mark.api
@pytest.mark.p0
@pytest.mark.django_db(transaction=True)
def test_sales_invoice_draft_revision_and_deterministic_tax_snapshot(
    accounting_context: dict[str, object],
) -> None:
    client = accounting_context["client"]
    assert isinstance(client, APIClient)
    _enable_sales(client, accounting_context)
    ids = _seed_sales_facts(accounting_context)
    invoices_url = f"{_root(accounting_context)}/sales/invoices"

    created = client.post(invoices_url, _payload(ids, "INV-P54-001"), format="json")
    assert created.status_code == 201, created.json()
    draft = created.json()
    assert created["ETag"] == '"1"'
    assert draft["status"] == "draft"
    assert draft["net_total"] == "95.000000"
    assert draft["tax_total"] == "0.000000"
    assert draft["gross_total"] == "95.000000"
    assert draft["addresses"][0]["line_1"] == "10 Calculation Road"
    invoice_id = draft["id"]

    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) FROM erp.journal_entries WHERE company_id=%s AND source_id=%s",
            [accounting_context["company"], invoice_id],
        )
        assert cursor.fetchone()[0] == 0
        cursor.execute(
            """
            SELECT count(*) FROM erp.stock_movements
            WHERE company_id=%s AND source_id=%s
            """,
            [accounting_context["company"], invoice_id],
        )
        assert cursor.fetchone()[0] == 0

    edited = client.patch(
        f"{invoices_url}/{invoice_id}",
        {"due_date": "2026-11-01"},
        format="json",
        HTTP_IF_MATCH='"1"',
    )
    assert edited.status_code == 200, edited.json()
    assert edited["ETag"] == '"2"'

    stale = client.patch(
        f"{invoices_url}/{invoice_id}",
        {"invoice_no": "STALE-NUMBER"},
        format="json",
        HTTP_IF_MATCH='"1"',
    )
    assert stale.status_code == 412, stale.json()
    assert stale.json()["error"]["details"]["current_revision"] == "2"

    calculated = client.post(
        f"{invoices_url}/{invoice_id}/calculate",
        format="json",
        HTTP_IF_MATCH='"2"',
    )
    assert calculated.status_code == 200, calculated.json()
    result = calculated.json()
    assert calculated["ETag"] == '"3"'
    assert result["net_total"] == "95.000000"
    assert result["tax_total"] == "9.500000"
    assert result["gross_total"] == "104.500000"
    component = result["lines"][0]["tax_components"][0]
    assert component["tax_jurisdiction_id"] == str(ids["jurisdiction"])
    assert component["tax_rate_version_id"] == str(ids["rate_version"])
    assert component["rate_version_code_snapshot"] == "2026-v1"
    assert component["taxable_base_amount"] == "95.000000"
    assert component["rate_snapshot"] == "10.00000000"
    assert component["rounding_method_snapshot"] == "half_up"
    assert component["rounding_precision_snapshot"] == 2
    assert component["recovery_percent_snapshot"] == "100.000000"
    assert component["currency_code_snapshot"] == "USD"

    recalculated_stale = client.post(
        f"{invoices_url}/{invoice_id}/calculate",
        format="json",
        HTTP_IF_MATCH='"2"',
    )
    assert recalculated_stale.status_code == 412

    fetched = client.get(f"{invoices_url}/{invoice_id}")
    assert fetched.status_code == 200, fetched.json()
    assert fetched.json() == result
    listed = client.get(f"{invoices_url}?status=draft")
    assert listed.status_code == 200, listed.json()
    assert [row["id"] for row in listed.json()["results"]] == [invoice_id]

    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT action FROM erp.company_audit_events
            WHERE company_id=%s AND object_type='sales_invoice'
            ORDER BY occurred_at, id
            """,
            [accounting_context["company"]],
        )
        assert [row[0] for row in cursor.fetchall()] == [
            "sales_invoice.created",
            "sales_invoice.updated",
            "sales_invoice.calculated",
        ]


@pytest.mark.api
@pytest.mark.security
@pytest.mark.django_db(transaction=True)
def test_sales_invoice_bounds_and_company_hidden_id(
    accounting_context: dict[str, object],
) -> None:
    client = accounting_context["client"]
    assert isinstance(client, APIClient)
    _enable_sales(client, accounting_context)
    ids = _seed_sales_facts(accounting_context)
    invoices_url = f"{_root(accounting_context)}/sales/invoices"
    payload = _payload(ids, "INV-P54-BOUND")
    line = payload["lines"][0]  # type: ignore[index]
    payload["lines"] = [line for _ in range(50)]
    maximum = client.post(invoices_url, payload, format="json")
    assert maximum.status_code == 201, maximum.json()
    assert len(maximum.json()["lines"]) == 50

    too_many_payload = {**payload, "invoice_no": "INV-P54-TOO-MANY"}
    too_many_payload["lines"] = [line for _ in range(51)]
    too_many = client.post(invoices_url, too_many_payload, format="json")
    assert too_many.status_code == 400

    hidden_id = uuid.uuid4()
    hidden_company = uuid.uuid4()
    with connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO erp.companies(id,tenant_id,code,legal_name,functional_currency)
            VALUES (%s,%s,%s,'Hidden company','USD')
            """,
            [hidden_company, accounting_context["tenant"], f"H-{hidden_company.hex[:8]}"],
        )
        cursor.execute(
            """
            INSERT INTO erp.sales_invoices(
                id,company_id,invoice_no,issue_date,tax_point_date,currency_code
            ) VALUES (%s,%s,'HIDDEN-P54','2026-09-27','2026-09-27','USD')
            """,
            [hidden_id, hidden_company],
        )
    hidden = client.get(f"{invoices_url}/{hidden_id}")
    assert hidden.status_code == 404
    listed = client.get(invoices_url)
    assert listed.status_code == 200, listed.json()
    assert all(row["id"] != str(hidden_id) for row in listed.json()["results"])
