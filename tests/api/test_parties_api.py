import uuid

import pytest
from django.contrib.auth import get_user_model
from django.db import connection
from rest_framework.test import APIClient


def _partner_url(context: dict[str, object]) -> str:
    return f"/api/v1/tenants/{context['tenant']}/companies/{context['company']}/partners"


def _payload(code: str, name: str, kind: str = "customer") -> dict[str, object]:
    return {
        "partner_code": code,
        "display_name": name,
        "partner_kind": kind,
        "email": f"{code.lower()}@example.com",
    }


@pytest.mark.api
@pytest.mark.p0
@pytest.mark.django_db(transaction=True)
def test_partner_crud_revision_duplicate_and_audit(
    accounting_context: dict[str, object],
) -> None:
    client = accounting_context["client"]
    assert isinstance(client, APIClient)
    url = _partner_url(accounting_context)

    created = client.post(url, _payload("CUS-001", "Acme Retail"), format="json")
    assert created.status_code == 201, created.json()
    assert created["ETag"] == '"1"'
    partner_id = created.json()["id"]

    detail = client.get(f"{url}/{partner_id}")
    assert detail.status_code == 200, detail.json()
    assert detail.json()["partner_code"] == "CUS-001"

    missing_revision = client.patch(
        f"{url}/{partner_id}", {"phone": "+92-300-0000000"}, format="json"
    )
    assert missing_revision.status_code == 428
    assert missing_revision.json()["error"]["code"] == "PRECONDITION_REQUIRED"

    stale = client.patch(
        f"{url}/{partner_id}",
        {"phone": "+92-300-0000000"},
        format="json",
        HTTP_IF_MATCH='"2"',
    )
    assert stale.status_code == 412
    assert stale.json()["error"]["details"]["current_revision"] == "1"

    updated = client.patch(
        f"{url}/{partner_id}",
        {"phone": "+92-300-0000000", "partner_kind": "both"},
        format="json",
        HTTP_IF_MATCH='"1"',
    )
    assert updated.status_code == 200, updated.json()
    assert updated["ETag"] == '"2"'
    assert updated.json()["partner_kind"] == "both"

    duplicate = client.post(url, _payload("CUS-001", "Duplicate"), format="json")
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "PARTNER_CODE_EXISTS"

    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT action, request_id
            FROM erp.company_audit_events
            WHERE company_id = %s AND object_type = 'business_partner'
            ORDER BY occurred_at, id
            """,
            [accounting_context["company"]],
        )
        events = cursor.fetchall()
    assert [event[0] for event in events] == ["party.created", "party.updated"]
    assert all(event[1] for event in events)


@pytest.mark.api
@pytest.mark.p1
@pytest.mark.django_db(transaction=True)
def test_partner_list_uses_filters_and_filter_bound_cursor(
    accounting_context: dict[str, object],
) -> None:
    client = accounting_context["client"]
    assert isinstance(client, APIClient)
    url = _partner_url(accounting_context)
    for code, name, kind in (
        ("CUS-001", "Alpha", "customer"),
        ("SUP-001", "Beta", "supplier"),
        ("BOTH-001", "Gamma", "both"),
    ):
        response = client.post(url, _payload(code, name, kind), format="json")
        assert response.status_code == 201, response.json()

    first = client.get(f"{url}?limit=2")
    assert first.status_code == 200, first.json()
    assert [row["display_name"] for row in first.json()["results"]] == ["Alpha", "Beta"]
    assert first.json()["next_cursor"]

    second = client.get(f"{url}?limit=2&cursor={first.json()['next_cursor']}")
    assert second.status_code == 200, second.json()
    assert [row["display_name"] for row in second.json()["results"]] == ["Gamma"]

    suppliers = client.get(f"{url}?partner_kind=supplier&q=Be")
    assert suppliers.status_code == 200, suppliers.json()
    assert [row["partner_code"] for row in suppliers.json()["results"]] == ["SUP-001"]

    cursor_mismatch = client.get(
        f"{url}?limit=2&partner_kind=customer&cursor={first.json()['next_cursor']}"
    )
    assert cursor_mismatch.status_code == 400
    assert cursor_mismatch.json()["error"]["code"] == "INVALID_CURSOR"


@pytest.mark.api
@pytest.mark.security
@pytest.mark.django_db(transaction=True)
def test_partner_permissions_separate_read_from_manage(
    accounting_context: dict[str, object],
) -> None:
    member_id = uuid.uuid4()
    membership_id = uuid.uuid4()
    role_id = uuid.uuid4()
    with connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO identity.users(id,email,password) VALUES (%s,%s,'!')",
            [member_id, f"partner-reader-{member_id}@example.com"],
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
            "INSERT INTO identity.roles(id,company_id,code,name) VALUES (%s,%s,'reader','Reader')",
            [role_id, accounting_context["company"]],
        )
        cursor.execute(
            """
            INSERT INTO identity.role_permissions(company_id,role_id,permission_code)
            VALUES (%s,%s,'party.view')
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
    url = _partner_url(accounting_context)

    readable = client.get(url)
    assert readable.status_code == 200, readable.json()
    denied = client.post(url, _payload("CUS-403", "Denied"), format="json")
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "WRITE_ACCESS_DENIED"


@pytest.mark.api
@pytest.mark.security
@pytest.mark.django_db(transaction=True)
def test_partner_scope_hides_an_unrelated_company(
    accounting_context: dict[str, object],
) -> None:
    client = accounting_context["client"]
    assert isinstance(client, APIClient)
    response = client.get(
        f"/api/v1/tenants/{accounting_context['tenant']}/companies/{uuid.uuid4()}/partners"
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "SCOPE_NOT_FOUND"
