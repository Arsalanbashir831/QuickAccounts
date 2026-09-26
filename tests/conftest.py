import uuid

import pytest
from django.contrib.auth import get_user_model
from django.db import connection
from rest_framework.test import APIClient


@pytest.fixture
def accounting_context(db: object, settings: object) -> dict[str, object]:
    names = (
        "tenant",
        "company",
        "user",
        "product",
        "plan",
        "version",
        "license",
        "term",
        "journal",
        "period",
        "cash",
        "equity",
    )
    ids = {name: uuid.uuid4() for name in names}
    product_code = f"quickaccounts-{ids['product']}"
    settings.ERP_PRODUCT_CODE = product_code
    with connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO erp.currencies(code,name,minor_units) VALUES ('USD','US Dollar',2) "
            "ON CONFLICT DO NOTHING"
        )
        cursor.execute("INSERT INTO erp.tenants(id,name) VALUES (%s,'API tenant')", [ids["tenant"]])
        cursor.execute(
            """
            INSERT INTO erp.companies(id,tenant_id,code,legal_name,functional_currency)
            VALUES (%s,%s,'MAIN','API Company','USD')
            """,
            [ids["company"], ids["tenant"]],
        )
        cursor.execute(
            """
            INSERT INTO identity.users(id,email,password,first_name)
            VALUES (%s,%s,'!','Owner')
            """,
            [ids["user"], f"owner-{ids['user']}@example.com"],
        )
        cursor.execute(
            """
            INSERT INTO identity.tenant_memberships(tenant_id,user_id,tenant_role)
            VALUES (%s,%s,'owner')
            """,
            [ids["tenant"], ids["user"]],
        )
        cursor.execute(
            """
            INSERT INTO identity.company_memberships(tenant_id,company_id,user_id)
            VALUES (%s,%s,%s)
            """,
            [ids["tenant"], ids["company"], ids["user"]],
        )
        cursor.execute(
            "INSERT INTO licensing.products(id,product_code,name) VALUES (%s,%s,'QuickAccounts')",
            [ids["product"], product_code],
        )
        cursor.execute(
            "INSERT INTO licensing.plans(id,product_id,plan_code,name) "
            "VALUES (%s,%s,'standard','Standard')",
            [ids["plan"], ids["product"]],
        )
        cursor.execute(
            """
            INSERT INTO licensing.plan_versions(
                id,product_id,plan_id,version_number,term_unit,max_activations,
                price_currency,price_amount
            ) VALUES (%s,%s,%s,1,'lifetime',1,'USD',25)
            """,
            [ids["version"], ids["product"], ids["plan"]],
        )
        cursor.execute(
            """
            INSERT INTO licensing.plan_features(plan_version_id,feature_code,is_enabled)
            VALUES (%s,'module.sales',true)
            """,
            [ids["version"]],
        )
        cursor.execute(
            "UPDATE licensing.plan_versions SET published_at=clock_timestamp() WHERE id=%s",
            [ids["version"]],
        )
        cursor.execute(
            """
            INSERT INTO licensing.licenses(
                id,tenant_id,product_id,license_number_hash,license_number_last4,status
            ) VALUES (%s,%s,%s,%s,'1234','active')
            """,
            [ids["license"], ids["tenant"], ids["product"], uuid.uuid4().bytes * 2],
        )
        cursor.execute(
            """
            INSERT INTO licensing.license_terms(
                id,tenant_id,license_id,plan_version_id,term_unit_snapshot,
                starts_at,max_activations_snapshot
            ) VALUES (%s,%s,%s,%s,'lifetime',clock_timestamp()-interval '1 day',1)
            """,
            [ids["term"], ids["tenant"], ids["license"], ids["version"]],
        )
        cursor.execute(
            """
            UPDATE licensing.tenant_product_bindings SET license_id=%s
            WHERE tenant_id=%s AND product_id=%s
            """,
            [ids["license"], ids["tenant"], ids["product"]],
        )
        cursor.execute(
            """
            INSERT INTO erp.journals(id,company_id,code,name,journal_type)
            VALUES (%s,%s,'GJ','General Journal','general')
            """,
            [ids["journal"], ids["company"]],
        )
        cursor.execute(
            """
            INSERT INTO erp.fiscal_periods(id,company_id,code,starts_on,ends_on)
            VALUES (%s,%s,'2026-09','2026-09-01','2026-09-30')
            """,
            [ids["period"], ids["company"]],
        )
        cursor.execute(
            """
            INSERT INTO erp.document_sequences(
                company_id,fiscal_period_id,sequence_code,prefix,next_value,padding_length
            ) VALUES (%s,%s,'MANUAL_JOURNAL','GJ-',1,6)
            """,
            [ids["company"], ids["period"]],
        )
        cursor.execute(
            """
            INSERT INTO erp.accounts(
                id,company_id,code,name,account_type,normal_balance
            ) VALUES
                (%s,%s,'1000','Cash','asset','debit'),
                (%s,%s,'3000','Opening Equity','equity','credit')
            """,
            [ids["cash"], ids["company"], ids["equity"], ids["company"]],
        )
    user = get_user_model().objects.get(pk=ids["user"])
    client = APIClient()
    client.force_login(user)
    ids["client"] = client
    return ids
