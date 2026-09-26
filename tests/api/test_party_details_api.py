import uuid

import pytest
from django.contrib.auth import get_user_model
from django.db import connection
from rest_framework.test import APIClient


def _partners_url(context: dict[str, object]) -> str:
    return f"/api/v1/tenants/{context['tenant']}/companies/{context['company']}/partners"


def _create_partner(client: APIClient, context: dict[str, object], code: str) -> str:
    response = client.post(
        _partners_url(context),
        {
            "partner_code": code,
            "display_name": f"Partner {code}",
            "partner_kind": "both",
        },
        format="json",
    )
    assert response.status_code == 201, response.json()
    return response.json()["id"]


def _seed_tax_catalog() -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, str, str]:
    jurisdiction_id = uuid.uuid4()
    other_jurisdiction_id = uuid.uuid4()
    tax_type_id = uuid.uuid4()
    jurisdiction_code = f"AE-FTA-{other_jurisdiction_id.hex[:8]}"
    tax_type_code = f"VAT-{tax_type_id.hex[:8]}"
    with connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO erp.tax_jurisdictions(
                id,country_code,jurisdiction_code,name,jurisdiction_level
            ) VALUES
                (%s,'PK',%s,'Pakistan Federal','country'),
                (%s,'AE',%s,'United Arab Emirates Federal','country')
            """,
            [
                jurisdiction_id,
                f"PK-FBR-{jurisdiction_id.hex[:8]}",
                other_jurisdiction_id,
                jurisdiction_code,
            ],
        )
        cursor.execute(
            """
            INSERT INTO erp.tax_types(
                id,jurisdiction_id,code,name,tax_family,calculation_stage,tax_direction
            ) VALUES (%s,%s,%s,'Value Added Tax','vat','line','bidirectional')
            """,
            [tax_type_id, other_jurisdiction_id, tax_type_code],
        )
    return (
        jurisdiction_id,
        other_jurisdiction_id,
        tax_type_id,
        jurisdiction_code,
        tax_type_code,
    )


@pytest.mark.api
@pytest.mark.p0
@pytest.mark.django_db(transaction=True)
def test_partner_address_create_list_update_and_default_guard(
    accounting_context: dict[str, object],
) -> None:
    client = accounting_context["client"]
    assert isinstance(client, APIClient)
    partner_id = _create_partner(client, accounting_context, "BOTH-ADDR")
    addresses_url = f"{_partners_url(accounting_context)}/{partner_id}/addresses"

    billing = client.post(
        addresses_url,
        {
            "address_kind": "billing",
            "line_1": "1 Main Boulevard",
            "city": "Lahore",
            "country_code": "pk",
            "is_default": True,
        },
        format="json",
    )
    assert billing.status_code == 201, billing.json()
    assert billing["ETag"] == '"1"'
    assert billing.json()["country_code"] == "PK"
    address_id = billing.json()["id"]

    duplicate_default = client.post(
        addresses_url,
        {
            "address_kind": "billing",
            "line_1": "2 Main Boulevard",
            "is_default": True,
        },
        format="json",
    )
    assert duplicate_default.status_code == 409
    assert duplicate_default.json()["error"]["code"] == "DEFAULT_ADDRESS_EXISTS"

    shipping = client.post(
        addresses_url,
        {
            "address_kind": "shipping",
            "line_1": "Warehouse Road",
            "is_default": True,
        },
        format="json",
    )
    assert shipping.status_code == 201, shipping.json()

    first_page = client.get(f"{addresses_url}?limit=1")
    assert first_page.status_code == 200, first_page.json()
    assert len(first_page.json()["results"]) == 1
    assert first_page.json()["next_cursor"]
    second_page = client.get(f"{addresses_url}?limit=1&cursor={first_page.json()['next_cursor']}")
    assert second_page.status_code == 200, second_page.json()
    assert len(second_page.json()["results"]) == 1

    stale = client.patch(
        f"{addresses_url}/{address_id}",
        {"city": "Karachi"},
        format="json",
        HTTP_IF_MATCH='"2"',
    )
    assert stale.status_code == 412

    updated = client.patch(
        f"{addresses_url}/{address_id}",
        {"city": "Karachi"},
        format="json",
        HTTP_IF_MATCH='"1"',
    )
    assert updated.status_code == 200, updated.json()
    assert updated["ETag"] == '"2"'
    assert updated.json()["city"] == "Karachi"

    other_partner_id = _create_partner(client, accounting_context, "OTHER-ADDR")
    hidden = client.patch(
        f"{_partners_url(accounting_context)}/{other_partner_id}/addresses/{address_id}",
        {"city": "Islamabad"},
        format="json",
        HTTP_IF_MATCH='"2"',
    )
    assert hidden.status_code == 404

    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT action, request_id
            FROM erp.company_audit_events
            WHERE company_id = %s AND object_type = 'partner_address'
            ORDER BY occurred_at, id
            """,
            [accounting_context["company"]],
        )
        events = cursor.fetchall()
    assert [event[0] for event in events] == [
        "party.address.created",
        "party.address.created",
        "party.address.updated",
    ]
    assert all(event[1] for event in events)


@pytest.mark.api
@pytest.mark.p0
@pytest.mark.django_db(transaction=True)
def test_partner_tax_registration_validity_catalog_and_duplicate_guard(
    accounting_context: dict[str, object],
) -> None:
    client = accounting_context["client"]
    assert isinstance(client, APIClient)
    partner_id = _create_partner(client, accounting_context, "BOTH-TAX")
    registrations_url = f"{_partners_url(accounting_context)}/{partner_id}/tax-registrations"
    (
        jurisdiction_id,
        other_jurisdiction_id,
        tax_type_id,
        jurisdiction_code,
        tax_type_code,
    ) = _seed_tax_catalog()
    payload = {
        "jurisdiction_id": str(other_jurisdiction_id),
        "tax_type_id": str(tax_type_id),
        "registration_number": "VAT-10001",
        "valid_from": "2026-01-01",
        "valid_to": "2027-01-01",
        "is_verified": True,
    }

    created = client.post(registrations_url, payload, format="json")
    assert created.status_code == 201, created.json()
    assert created.json()["jurisdiction_code"] == jurisdiction_code
    assert created.json()["tax_type_code"] == tax_type_code

    duplicate = client.post(registrations_url, payload, format="json")
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "TAX_REGISTRATION_EXISTS"

    invalid_dates = client.post(
        registrations_url,
        {**payload, "registration_number": "VAT-10002", "valid_to": "2026-01-01"},
        format="json",
    )
    assert invalid_dates.status_code == 400

    wrong_jurisdiction = client.post(
        registrations_url,
        {
            **payload,
            "jurisdiction_id": str(jurisdiction_id),
            "registration_number": "VAT-10003",
        },
        format="json",
    )
    assert wrong_jurisdiction.status_code == 400
    assert wrong_jurisdiction.json()["error"]["code"] == "INVALID_TAX_REGISTRATION_TYPE"

    listed = client.get(f"{registrations_url}?is_active=true")
    assert listed.status_code == 200, listed.json()
    assert [row["registration_number"] for row in listed.json()["results"]] == ["VAT-10001"]

    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT action, request_id
            FROM erp.company_audit_events
            WHERE company_id = %s AND object_type = 'partner_tax_registration'
            """,
            [accounting_context["company"]],
        )
        events = cursor.fetchall()
    assert len(events) == 1
    assert events[0][0] == "party.tax_registration.created"
    assert events[0][1]


@pytest.mark.api
@pytest.mark.security
@pytest.mark.django_db(transaction=True)
def test_partner_tax_registration_requires_tax_configuration_permission(
    accounting_context: dict[str, object],
) -> None:
    owner_client = accounting_context["client"]
    assert isinstance(owner_client, APIClient)
    partner_id = _create_partner(owner_client, accounting_context, "TAX-PERM")

    member_id = uuid.uuid4()
    membership_id = uuid.uuid4()
    role_id = uuid.uuid4()
    with connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO identity.users(id,email,password) VALUES (%s,%s,'!')",
            [member_id, f"party-manager-{member_id}@example.com"],
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
            "INSERT INTO identity.roles(id,company_id,code,name) VALUES (%s,%s,'party','Party')",
            [role_id, accounting_context["company"]],
        )
        cursor.execute(
            """
            INSERT INTO identity.role_permissions(company_id,role_id,permission_code)
            VALUES (%s,%s,'party.view'),(%s,%s,'party.manage')
            """,
            [
                accounting_context["company"],
                role_id,
                accounting_context["company"],
                role_id,
            ],
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
    response = client.get(f"{_partners_url(accounting_context)}/{partner_id}/tax-registrations")
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "PERMISSION_DENIED"
