import uuid
from pathlib import Path

import pytest
from django.db import DatabaseError, connection, transaction


@pytest.mark.integration
@pytest.mark.p0
@pytest.mark.django_db(transaction=True)
def test_company_provisioning_creates_required_and_optional_module_defaults(
    accounting_context: dict[str, object],
) -> None:
    company_id = uuid.uuid4()
    with connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO erp.companies(
                id,tenant_id,code,legal_name,functional_currency
            ) VALUES (%s,%s,'POLICY','Policy Test Company','USD')
            """,
            [company_id, accounting_context["tenant"]],
        )
        cursor.execute(
            """
            SELECT module_code,mode FROM erp.company_module_settings
            WHERE company_id=%s ORDER BY module_code
            """,
            [company_id],
        )
        modes = dict(cursor.fetchall())
        cursor.execute(
            "SELECT revision FROM erp.company_policy_state WHERE company_id=%s",
            [company_id],
        )
        revision = cursor.fetchone()[0]
    assert modes["core"] == "enabled"
    assert modes["accounting"] == "enabled"
    assert modes["tax_calculation"] == "enabled"
    assert modes["sales"] == "disabled"
    assert modes["manufacturing"] == "disabled"
    assert revision > 0


@pytest.mark.integration
@pytest.mark.p0
@pytest.mark.django_db(transaction=True)
def test_terms_require_a_published_plan_for_the_same_product(
    accounting_context: dict[str, object],
) -> None:
    draft_version = uuid.uuid4()
    second_product = uuid.uuid4()
    second_plan = uuid.uuid4()
    second_version = uuid.uuid4()
    candidate_license = uuid.uuid4()
    with connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO licensing.plan_versions(
                id,product_id,plan_id,version_number,term_unit,term_count,max_activations
            ) VALUES (%s,%s,%s,2,'month',1,1)
            """,
            [draft_version, accounting_context["product"], accounting_context["plan"]],
        )
        cursor.execute(
            """
            INSERT INTO licensing.licenses(
                id,tenant_id,product_id,license_number_hash,license_number_last4,status
            ) VALUES (%s,%s,%s,%s,'5678','active')
            """,
            [
                candidate_license,
                accounting_context["tenant"],
                accounting_context["product"],
                uuid.uuid4().bytes * 2,
            ],
        )
    with pytest.raises(DatabaseError), transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO licensing.license_terms(
                    tenant_id,license_id,plan_version_id,term_unit_snapshot,
                    term_count_snapshot,starts_at,expires_at,max_activations_snapshot
                ) VALUES (%s,%s,%s,'month',1,clock_timestamp(),
                          clock_timestamp()+interval '1 month',1)
                """,
                [accounting_context["tenant"], candidate_license, draft_version],
            )

    with connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO licensing.products(id,product_code,name) VALUES (%s,%s,'Other')",
            [second_product, f"other-{second_product}"],
        )
        cursor.execute(
            """
            INSERT INTO licensing.plans(id,product_id,plan_code,name)
            VALUES (%s,%s,'other','Other Plan')
            """,
            [second_plan, second_product],
        )
        cursor.execute(
            """
            INSERT INTO licensing.plan_versions(
                id,product_id,plan_id,version_number,term_unit,term_count,
                max_activations,published_at
            ) VALUES (%s,%s,%s,1,'month',1,1,clock_timestamp())
            """,
            [second_version, second_product, second_plan],
        )
    with pytest.raises(DatabaseError), transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO licensing.license_terms(
                    tenant_id,license_id,plan_version_id,term_unit_snapshot,
                    term_count_snapshot,starts_at,expires_at,max_activations_snapshot
                ) VALUES (%s,%s,%s,'month',1,clock_timestamp(),
                          clock_timestamp()+interval '1 month',1)
                """,
                [accounting_context["tenant"], candidate_license, second_version],
            )


@pytest.mark.integration
@pytest.mark.p0
@pytest.mark.django_db(transaction=True)
def test_published_plan_and_issued_license_history_are_immutable(
    accounting_context: dict[str, object],
) -> None:
    with pytest.raises(DatabaseError), transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE licensing.plan_versions SET price_amount=99 WHERE id=%s",
                [accounting_context["version"]],
            )
    with pytest.raises(DatabaseError), transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute(
                "DELETE FROM licensing.plan_features WHERE plan_version_id=%s",
                [accounting_context["version"]],
            )
    with pytest.raises(DatabaseError), transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE licensing.license_terms "
                "SET starts_at=starts_at-interval '1 day' WHERE id=%s",
                [accounting_context["term"]],
            )

    event_id = uuid.uuid4()
    with connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO licensing.license_events(
                id,tenant_id,license_id,event_type,event_data
            ) VALUES (%s,%s,%s,'activated','{}'::jsonb)
            """,
            [event_id, accounting_context["tenant"], accounting_context["license"]],
        )
    with pytest.raises(DatabaseError), transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM licensing.license_events WHERE id=%s", [event_id])


@pytest.mark.unit
def test_runtime_role_cannot_bypass_company_module_policy_tables() -> None:
    grant_file = Path(__file__).resolve().parents[2] / "deploy/vps/grant-runtime.sql"
    sql = grant_file.read_text()
    assert "REVOKE INSERT, UPDATE, DELETE ON" in sql
    assert "erp.company_policy_state" in sql
    assert "erp.company_module_settings" in sql
    assert "erp.company_audit_events" in sql
