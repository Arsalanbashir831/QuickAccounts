import uuid

import pytest
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import override_settings
from rest_framework.test import APIClient


@pytest.mark.api
@pytest.mark.django_db(transaction=True)
def test_platform_provisions_tenant_owner_and_company_atomically() -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO erp.currencies(code,name,minor_units) VALUES ('USD','US Dollar',2) "
            "ON CONFLICT DO NOTHING"
        )
    owner_client = APIClient()
    operator = get_user_model().objects.create_superuser(
        email=f"operator-{uuid.uuid4()}@example.com", password="Operator-password-2026!"
    )
    operator_client = APIClient()
    operator_client.force_login(operator)
    owner_email = f"owner-{uuid.uuid4()}@example.com"
    payload = {
        "name": "New tenant", "company_code": "MAIN", "company_name": "Main Company",
        "currency": "USD", "timezone_name": "UTC", "business_type": "retail",
        "owner_email": owner_email, "owner_password": "Owner-password-2026!",
        "reason": "Approved registration",
    }
    url = "/platform-api/v1/tenants"
    assert owner_client.post(url, payload, format="json").status_code == 401
    owner = get_user_model().objects.create_user(
        email=f"other-{uuid.uuid4()}@example.com", password="Owner-password-2026!"
    )
    owner_client.force_login(owner)
    assert owner_client.post(url, payload, format="json").status_code == 403
    created = operator_client.post(
        url, payload, format="json", HTTP_IDEMPOTENCY_KEY="new-tenant-registration"
    )
    assert created.status_code == 201, created.json()
    replay = operator_client.post(
        url, payload, format="json", HTTP_IDEMPOTENCY_KEY="new-tenant-registration"
    )
    assert replay.json() == created.json()
    provisioned_owner = get_user_model().objects.get(email=owner_email)
    assert provisioned_owner.check_password(payload["owner_password"])
    owner_client.force_login(provisioned_owner)
    discovered = owner_client.get("/api/v1/tenants/")
    assert discovered.status_code == 200, discovered.json()
    assert str(created.json()["tenant_id"]) in {
        str(row["tenant_id"]) for row in discovered.json()["results"]
    }
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) FROM erp.tenants WHERE id=%s", [created.json()["tenant_id"]]
        )
        assert cursor.fetchone()[0] == 1
        cursor.execute(
            "SELECT count(*) FROM identity.company_memberships WHERE company_id=%s "
            "AND user_id=%s", [created.json()["company_id"], created.json()["owner_user_id"]]
        )
        assert cursor.fetchone()[0] == 1
    company = operator_client.post(
        f"/platform-api/v1/tenants/{created.json()['tenant_id']}/companies",
        {"code": "SECOND", "legal_name": "Second Company", "currency": "USD",
         "business_type": "wholesale", "owner_user_id": created.json()["owner_user_id"],
         "reason": "Additional legal entity"},
        format="json", HTTP_IDEMPOTENCY_KEY="second-company",
    )
    assert company.status_code == 201, company.json()
    assert company.json()["company_id"] != created.json()["company_id"]


@pytest.mark.api
@pytest.mark.django_db(transaction=True)
def test_failed_platform_provision_has_no_partial_tenant() -> None:
    operator = get_user_model().objects.create_superuser(
        email=f"operator-{uuid.uuid4()}@example.com", password="Operator-password-2026!"
    )
    client = APIClient()
    client.force_login(operator)
    name = f"Invalid tenant {uuid.uuid4()}"
    result = client.post(
        "/platform-api/v1/tenants",
        {"name": name, "company_code": "MAIN", "company_name": "Main",
         "currency": "ZZZ", "business_type": "retail",
         "owner_email": f"owner-{uuid.uuid4()}@example.com",
         "owner_password": "Owner-password-2026!", "reason": "Test"},
        format="json", HTTP_IDEMPOTENCY_KEY="invalid-currency",
    )
    assert result.status_code == 409
    with connection.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM erp.tenants WHERE name=%s", [name])
        assert cursor.fetchone()[0] == 0


@pytest.mark.api
@pytest.mark.django_db(transaction=True)
def test_platform_onboarding_rejects_weak_password_without_server_error() -> None:
    operator = get_user_model().objects.create_superuser(
        email=f"operator-{uuid.uuid4()}@example.com", password="Operator-password-2026!"
    )
    client = APIClient()
    client.force_login(operator)
    result = client.post(
        "/platform-api/v1/tenants",
        {"name": "Weak password tenant", "company_code": "MAIN", "company_name": "Main",
         "currency": "PKR", "business_type": "retail",
         "owner_email": f"owner-{uuid.uuid4()}@example.com",
         "owner_password": "382001", "reason": "Test"},
        format="json", HTTP_IDEMPOTENCY_KEY="weak-password",
    )
    assert result.status_code == 400, result.content
    assert "owner_password" in str(result.json()["error"]["details"])


@pytest.mark.api
@pytest.mark.django_db(transaction=True)
def test_platform_onboarding_reports_missing_platform_database() -> None:
    operator = get_user_model().objects.create_superuser(
        email=f"operator-{uuid.uuid4()}@example.com", password="Operator-password-2026!"
    )
    client = APIClient()
    client.force_login(operator)
    with override_settings(DEBUG=False, PLATFORM_ALLOW_DEFAULT_CONNECTION=False):
        result = client.post(
            "/platform-api/v1/tenants",
            {"name": "Missing platform database", "company_code": "MAIN",
             "company_name": "Main", "currency": "PKR", "business_type": "retail",
             "owner_email": f"owner-{uuid.uuid4()}@example.com",
             "owner_password": "Strong-password-2026!", "reason": "Test"},
            format="json", HTTP_IDEMPOTENCY_KEY="missing-platform-database",
        )
    assert result.status_code == 503, result.content
    assert result.json()["error"]["code"] == "PLATFORM_DATABASE_NOT_CONFIGURED"
