import uuid

import pytest
from django.db import connection
from rest_framework.test import APIClient


def _root(context: dict[str, object]) -> str:
    return f"/api/v1/tenants/{context['tenant']}/companies/{context['company']}"


def _bind_license(context: dict[str, object], features: tuple[str, ...]) -> None:
    plan_id = uuid.uuid4()
    version_id = uuid.uuid4()
    license_id = uuid.uuid4()
    term_id = uuid.uuid4()
    suffix = plan_id.hex[:10]
    with connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO licensing.plans(id,product_id,plan_code,name)
            VALUES (%s,%s,%s,'Purchasing plan')
            """,
            [plan_id, context["product"], f"purchasing-{suffix}"],
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
        for feature in features:
            cursor.execute(
                """
                INSERT INTO licensing.plan_features(plan_version_id,feature_code,is_enabled)
                VALUES (%s,%s,true)
                """,
                [version_id, feature],
            )
        cursor.execute(
            "UPDATE licensing.plan_versions SET published_at=clock_timestamp() WHERE id=%s",
            [version_id],
        )
        cursor.execute(
            """
            INSERT INTO licensing.licenses(
                id,tenant_id,product_id,license_number_hash,license_number_last4,status
            ) VALUES (%s,%s,%s,%s,'4321','active')
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
            "reason": f"P5.6 set {module_code} to {mode}",
        },
        format="json",
        HTTP_IDEMPOTENCY_KEY=f"p56-{module_code}-{mode}-{uuid.uuid4()}",
        HTTP_IF_MATCH=f'"{modules.json()["policy_revision"]}"',
    )
    assert response.status_code == 200, response.json()


def _enable_purchasing(client: APIClient, context: dict[str, object]) -> None:
    _bind_license(context, ("module.purchasing",))
    _change_module(client, context, "purchasing", "enabled")


def _seed_purchase_facts(context: dict[str, object]) -> dict[str, uuid.UUID]:
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
            ) VALUES (%s,%s,%s,'Draft supplier','supplier')
            """,
            [ids["partner"], context["company"], f"SUP-{suffix}"],
        )
        cursor.execute(
            """
            INSERT INTO erp.partner_addresses(
                id,company_id,partner_id,address_kind,line_1,city,country_code,is_default
            ) VALUES (%s,%s,%s,'billing','20 Procurement Road','Karachi','PK',true)
            """,
            [ids["address"], context["company"], ids["partner"]],
        )
        cursor.execute(
            "INSERT INTO erp.uom_categories(id,code,name) VALUES (%s,%s,'Quantity')",
            [ids["uom_category"], f"PQTY-{suffix}"],
        )
        cursor.execute(
            "INSERT INTO erp.uoms(id,category_id,code,name,to_base_factor) "
            "VALUES (%s,%s,%s,'Each',1)",
            [ids["uom"], ids["uom_category"], f"PEA-{suffix}"],
        )
        cursor.execute(
            """
            INSERT INTO erp.items(id,company_id,sku,name,item_kind,base_uom_id)
            VALUES (%s,%s,%s,'Maintenance service','service',%s)
            """,
            [ids["item"], context["company"], f"PSRV-{suffix}", ids["uom"]],
        )
        cursor.execute(
            """
            INSERT INTO erp.tax_jurisdictions(
                id,country_code,jurisdiction_code,name,jurisdiction_level
            ) VALUES (%s,'PK',%s,'Test jurisdiction','country')
            """,
            [ids["jurisdiction"], f"PTST-{suffix}"],
        )
        cursor.execute(
            """
            INSERT INTO erp.tax_types(
                id,jurisdiction_id,code,name,tax_family,calculation_stage,tax_direction
            ) VALUES (%s,%s,%s,'Test input tax','vat','line','input')
            """,
            [ids["tax_type"], ids["jurisdiction"], f"IN-{suffix}"],
        )
        cursor.execute(
            """
            INSERT INTO erp.tax_rate_schedules(
                id,jurisdiction_id,tax_type_id,schedule_code,name
            ) VALUES (%s,%s,%s,%s,'Test ten percent input')
            """,
            [ids["schedule"], ids["jurisdiction"], ids["tax_type"], f"PTEN-{suffix}"],
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
            ) VALUES (%s,%s,%s,%s,'Test purchase tax','purchase',false)
            """,
            [ids["tax_code"], context["company"], ids["jurisdiction"], f"PTAX-{suffix}"],
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


def _seed_posting_configuration(
    context: dict[str, object], ids: dict[str, uuid.UUID]
) -> dict[str, uuid.UUID]:
    posting = {
        name: uuid.uuid4()
        for name in ("payable", "purchase", "tax", "inventory", "journal")
    }
    suffix = posting["payable"].hex[:8]
    with connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO erp.accounts(id,company_id,code,name,account_type,normal_balance)
            VALUES
              (%s,%s,%s,'Trade payables','payable','credit'),
              (%s,%s,%s,'Purchases','expense','debit'),
              (%s,%s,%s,'Input tax','asset','debit'),
              (%s,%s,%s,'Inventory','asset','debit')
            """,
            [
                posting["payable"],
                context["company"],
                f"AP-{suffix}",
                posting["purchase"],
                context["company"],
                f"PUR-{suffix}",
                posting["tax"],
                context["company"],
                f"ITAX-{suffix}",
                posting["inventory"],
                context["company"],
                f"PINV-{suffix}",
            ],
        )
        cursor.execute(
            """
            INSERT INTO erp.accounting_posting_rules(
                company_id,event_code,role_code,account_id
            ) VALUES (%s,'purchase_bill','accounts_payable',%s)
            """,
            [context["company"], posting["payable"]],
        )
        cursor.execute(
            """
            INSERT INTO erp.item_accounting_profiles(
                company_id,item_id,purchase_account_id
            ) VALUES (%s,%s,%s)
            """,
            [context["company"], ids["item"], posting["purchase"]],
        )
        cursor.execute(
            """
            UPDATE erp.tax_code_components SET input_tax_account_id=%s
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
                f"PCOMP-{suffix}",
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
                f"PSUP-{suffix}",
            ],
        )
        cursor.execute(
            """
            INSERT INTO erp.journals(id,company_id,code,name,journal_type)
            VALUES (%s,%s,%s,'Purchase journal','purchase')
            """,
            [posting["journal"], context["company"], f"PJ-{suffix}"],
        )
        cursor.execute(
            """
            INSERT INTO erp.document_sequences(
                company_id,fiscal_period_id,sequence_code,prefix,next_value,padding_length
            ) VALUES
              (%s,%s,'PURCHASE_BILL','PB-',1,6),
              (%s,%s,'PURCHASE_JOURNAL','PJ-',1,6)
            """,
            [
                context["company"],
                context["period"],
                context["company"],
                context["period"],
            ],
        )
    return posting


def _payload(ids: dict[str, uuid.UUID], bill_no: str) -> dict[str, object]:
    return {
        "bill_no": bill_no,
        "supplier_id": str(ids["partner"]),
        "bill_date": "2026-09-27",
        "tax_point_date": "2026-09-27",
        "tax_jurisdiction_id": str(ids["jurisdiction"]),
        "due_date": "2026-10-27",
        "currency_code": "USD",
        "addresses": [
            {
                "address_kind": "bill_to",
                "source_address_id": str(ids["address"]),
                "recipient_name": "Draft supplier accounts",
                "tax_registration_no": "TEST-REG-P1",
            }
        ],
        "lines": [
            {
                "item_id": str(ids["item"]),
                "description": "Deterministic maintenance",
                "quantity": "2.000000",
                "unit_cost": "50.000000",
                "tax_code_id": str(ids["tax_code"]),
            }
        ],
    }


@pytest.mark.api
@pytest.mark.p0
@pytest.mark.django_db(transaction=True)
def test_purchase_bill_draft_revision_and_deterministic_tax_snapshot(
    accounting_context: dict[str, object],
) -> None:
    client = accounting_context["client"]
    assert isinstance(client, APIClient)
    _enable_purchasing(client, accounting_context)
    ids = _seed_purchase_facts(accounting_context)
    bills_url = f"{_root(accounting_context)}/purchasing/bills"

    created = client.post(bills_url, _payload(ids, "BILL-P56-001"), format="json")
    assert created.status_code == 201, created.json()
    draft = created.json()
    assert created["ETag"] == '"1"'
    assert draft["status"] == "draft"
    assert draft["net_total"] == "100.000000"
    assert draft["tax_total"] == "0.000000"
    assert draft["gross_total"] == "100.000000"
    assert draft["addresses"][0]["line_1"] == "20 Procurement Road"
    bill_id = draft["id"]

    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) FROM erp.journal_entries WHERE company_id=%s AND source_id=%s",
            [accounting_context["company"], bill_id],
        )
        assert cursor.fetchone()[0] == 0
        cursor.execute(
            """
            SELECT count(*) FROM erp.stock_movements
            WHERE company_id=%s AND source_id=%s
            """,
            [accounting_context["company"], bill_id],
        )
        assert cursor.fetchone()[0] == 0

    edited = client.patch(
        f"{bills_url}/{bill_id}",
        {"due_date": "2026-11-01"},
        format="json",
        HTTP_IF_MATCH='"1"',
    )
    assert edited.status_code == 200, edited.json()
    assert edited["ETag"] == '"2"'

    stale = client.patch(
        f"{bills_url}/{bill_id}",
        {"bill_no": "STALE-NUMBER"},
        format="json",
        HTTP_IF_MATCH='"1"',
    )
    assert stale.status_code == 412, stale.json()
    assert stale.json()["error"]["details"]["current_revision"] == "2"

    calculated = client.post(
        f"{bills_url}/{bill_id}/calculate",
        format="json",
        HTTP_IF_MATCH='"2"',
    )
    assert calculated.status_code == 200, calculated.json()
    result = calculated.json()
    assert calculated["ETag"] == '"3"'
    assert result["net_total"] == "100.000000"
    assert result["tax_total"] == "10.000000"
    assert result["gross_total"] == "110.000000"
    component = result["lines"][0]["tax_components"][0]
    assert component["tax_jurisdiction_id"] == str(ids["jurisdiction"])
    assert component["tax_rate_version_id"] == str(ids["rate_version"])
    assert component["rate_version_code_snapshot"] == "2026-v1"
    assert component["taxable_base_amount"] == "100.000000"
    assert component["rate_snapshot"] == "10.00000000"
    assert component["rounding_method_snapshot"] == "half_up"
    assert component["rounding_precision_snapshot"] == 2
    assert component["recovery_percent_snapshot"] == "100.000000"
    assert component["recoverable_amount"] == "10.000000"
    assert component["currency_code_snapshot"] == "USD"

    recalculated_stale = client.post(
        f"{bills_url}/{bill_id}/calculate",
        format="json",
        HTTP_IF_MATCH='"2"',
    )
    assert recalculated_stale.status_code == 412

    fetched = client.get(f"{bills_url}/{bill_id}")
    assert fetched.status_code == 200, fetched.json()
    assert fetched.json() == result
    listed = client.get(f"{bills_url}?status=draft")
    assert listed.status_code == 200, listed.json()
    assert [row["id"] for row in listed.json()["results"]] == [bill_id]

    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT action FROM erp.company_audit_events
            WHERE company_id=%s AND object_type='purchase_bill'
            ORDER BY occurred_at, id
            """,
            [accounting_context["company"]],
        )
        assert [row[0] for row in cursor.fetchall()] == [
            "purchase_bill.created",
            "purchase_bill.updated",
            "purchase_bill.calculated",
        ]


@pytest.mark.api
@pytest.mark.security
@pytest.mark.django_db(transaction=True)
def test_purchase_bill_bounds_and_company_hidden_id(
    accounting_context: dict[str, object],
) -> None:
    client = accounting_context["client"]
    assert isinstance(client, APIClient)
    _enable_purchasing(client, accounting_context)
    ids = _seed_purchase_facts(accounting_context)
    bills_url = f"{_root(accounting_context)}/purchasing/bills"
    payload = _payload(ids, "BILL-P56-BOUND")
    line = payload["lines"][0]  # type: ignore[index]
    payload["lines"] = [line for _ in range(50)]
    maximum = client.post(bills_url, payload, format="json")
    assert maximum.status_code == 201, maximum.json()
    assert len(maximum.json()["lines"]) == 50

    too_many_payload = {**payload, "bill_no": "BILL-P56-TOO-MANY"}
    too_many_payload["lines"] = [line for _ in range(51)]
    too_many = client.post(bills_url, too_many_payload, format="json")
    assert too_many.status_code == 400

    hidden_id = uuid.uuid4()
    hidden_company = uuid.uuid4()
    hidden_supplier = uuid.uuid4()
    with connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO erp.companies(id,tenant_id,code,legal_name,functional_currency)
            VALUES (%s,%s,%s,'Hidden company','USD')
            """,
            [hidden_company, accounting_context["tenant"], f"PH-{hidden_company.hex[:8]}"],
        )
        cursor.execute(
            """
            INSERT INTO erp.business_partners(
                id,company_id,partner_code,display_name,partner_kind
            ) VALUES (%s,%s,'HIDDEN-SUPPLIER','Hidden supplier','supplier')
            """,
            [hidden_supplier, hidden_company],
        )
        cursor.execute(
            """
            INSERT INTO erp.purchase_bills(
                id,company_id,bill_no,supplier_id,bill_date,tax_point_date,currency_code
            ) VALUES (%s,%s,'HIDDEN-P56',%s,'2026-09-27','2026-09-27','USD')
            """,
            [hidden_id, hidden_company, hidden_supplier],
        )
    hidden = client.get(f"{bills_url}/{hidden_id}")
    assert hidden.status_code == 404
    listed = client.get(bills_url)
    assert listed.status_code == 200, listed.json()
    assert all(row["id"] != str(hidden_id) for row in listed.json()["results"])
