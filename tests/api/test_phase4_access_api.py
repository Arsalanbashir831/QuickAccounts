import concurrent.futures
import hashlib
import uuid

import pytest
from django.contrib.auth import get_user_model
from django.db import close_old_connections, connection
from rest_framework.test import APIClient

pytest_plugins = ("tests.api.test_accounting_api",)


def _company_root(context: dict[str, object]) -> str:
    return f"/api/v1/tenants/{context['tenant']}/companies/{context['company']}"


def _license_root(context: dict[str, object]) -> str:
    return f"/api/v1/tenants/{context['tenant']}/license"


@pytest.mark.api
@pytest.mark.p0
@pytest.mark.django_db(transaction=True)
def test_module_changes_are_revisioned_atomic_and_idempotent(
    accounting_context: dict[str, object],
) -> None:
    client = accounting_context["client"]
    assert isinstance(client, APIClient)
    root = _company_root(accounting_context)

    modules = client.get(f"{root}/admin/modules")
    assert modules.status_code == 200, modules.json()
    revision = modules.json()["policy_revision"]
    payload = {
        "changes": [{"module_code": "sales", "mode": "enabled"}],
        "reason": "Enable customer invoicing",
    }
    missing_precondition = client.post(
        f"{root}/admin/module-changes",
        payload,
        format="json",
        HTTP_IDEMPOTENCY_KEY="modules-sales-1",
    )
    assert missing_precondition.status_code == 428

    headers = {
        "HTTP_IDEMPOTENCY_KEY": "modules-sales-1",
        "HTTP_IF_MATCH": f'"{revision}"',
    }
    first = client.post(f"{root}/admin/module-changes", payload, format="json", **headers)
    replay = client.post(f"{root}/admin/module-changes", payload, format="json", **headers)
    assert first.status_code == 200, first.json()
    assert replay.status_code == 200, replay.json()
    assert replay.json() == first.json()
    assert first.json()["policy_revision"] > revision
    assert len(first.json()["audit_event_ids"]) == 1

    stale = client.post(
        f"{root}/admin/module-changes",
        {"changes": [{"module_code": "sales", "mode": "read_only"}], "reason": "Pause"},
        format="json",
        HTTP_IDEMPOTENCY_KEY="modules-sales-stale",
        HTTP_IF_MATCH=f'"{revision}"',
    )
    assert stale.status_code == 412, stale.json()

    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT mode FROM erp.company_module_settings "
            "WHERE company_id=%s AND module_code='sales'",
            [accounting_context["company"]],
        )
        assert cursor.fetchone()[0] == "enabled"
        cursor.execute(
            "SELECT count(*) FROM erp.outbox_events WHERE company_id=%s "
            "AND event_type='company.modules.changed'",
            [accounting_context["company"]],
        )
        assert cursor.fetchone()[0] == 1


@pytest.mark.api
@pytest.mark.p0
@pytest.mark.django_db(transaction=True)
def test_module_change_blockers_and_capabilities(accounting_context: dict[str, object]) -> None:
    client = accounting_context["client"]
    assert isinstance(client, APIClient)
    root = _company_root(accounting_context)
    modules = client.get(f"{root}/admin/modules").json()
    revision = modules["policy_revision"]

    required = client.post(
        f"{root}/admin/module-changes",
        {
            "changes": [{"module_code": "accounting", "mode": "disabled"}],
            "reason": "Invalid required-module change",
        },
        format="json",
        HTTP_IDEMPOTENCY_KEY="required-module",
        HTTP_IF_MATCH=f'"{revision}"',
    )
    assert required.status_code == 409, required.json()
    assert required.json()["error"]["code"] == "MODULE_CHANGE_BLOCKED"

    unlicensed = client.post(
        f"{root}/admin/module-changes",
        {
            "changes": [{"module_code": "inventory", "mode": "enabled"}],
            "reason": "Attempt unavailable feature",
        },
        format="json",
        HTTP_IDEMPOTENCY_KEY="unlicensed-inventory",
        HTTP_IF_MATCH=f'"{revision}"',
    )
    assert unlicensed.status_code == 403, unlicensed.json()
    assert unlicensed.json()["error"]["code"] == "FEATURE_NOT_LICENSED"

    capabilities = client.get(f"{root}/capabilities")
    assert capabilities.status_code == 200, capabilities.json()
    accounting = next(
        module for module in capabilities.json()["modules"] if module["module_code"] == "accounting"
    )
    inventory = next(
        module for module in capabilities.json()["modules"] if module["module_code"] == "inventory"
    )
    assert accounting["write_allowed"] is True
    assert "FEATURE_NOT_LICENSED" in inventory["denial_reasons"]
    post_action = next(
        action
        for action in capabilities.json()["actions"]
        if action["code"] == "accounting.entry.post"
    )
    assert post_action["allowed"] is True


@pytest.mark.api
@pytest.mark.p0
@pytest.mark.django_db(transaction=True)
def test_activation_quota_and_expiry_safe_cleanup(accounting_context: dict[str, object]) -> None:
    client = accounting_context["client"]
    assert isinstance(client, APIClient)
    root = _license_root(accounting_context)

    summary = client.get(root)
    assert summary.status_code == 200, summary.json()
    assert summary.json()["license_number_last4"] == "1234"
    assert "license_number_hash" not in summary.json()
    assert summary.json()["activation_quota"] == 1

    first = client.post(
        f"{root}/activations",
        {"device_fingerprint": "device-one-unique-fingerprint"},
        format="json",
        HTTP_IDEMPOTENCY_KEY="activation-one",
    )
    replay = client.post(
        f"{root}/activations",
        {"device_fingerprint": "device-one-unique-fingerprint"},
        format="json",
        HTTP_IDEMPOTENCY_KEY="activation-one",
    )
    assert first.status_code == 201, first.json()
    assert replay.status_code == 201, replay.json()
    assert replay.json() == first.json()

    second = client.post(
        f"{root}/activations",
        {"device_fingerprint": "device-two-unique-fingerprint"},
        format="json",
        HTTP_IDEMPOTENCY_KEY="activation-two",
    )
    assert second.status_code == 409, second.json()
    assert second.json()["error"]["code"] == "ACTIVATION_QUOTA_EXCEEDED"

    with connection.cursor() as cursor:
        cursor.execute(
            "UPDATE licensing.licenses SET status='suspended' WHERE id=%s",
            [accounting_context["license"]],
        )
    activation_id = first.json()["id"]
    heartbeat = client.post(f"{root}/activations/{activation_id}/heartbeat", {}, format="json")
    deactivate = client.post(f"{root}/activations/{activation_id}/deactivate", {}, format="json")
    assert heartbeat.status_code == 200, heartbeat.json()
    assert deactivate.status_code == 200, deactivate.json()
    assert deactivate.json()["deactivated_at"] is not None


@pytest.mark.api
@pytest.mark.p1
@pytest.mark.django_db(transaction=True)
def test_renewal_order_replays_without_duplicates(accounting_context: dict[str, object]) -> None:
    client = accounting_context["client"]
    assert isinstance(client, APIClient)
    url = f"{_license_root(accounting_context)}/renewal-orders"
    payload = {"plan_version_id": str(accounting_context["version"]), "provider": "manual"}
    first = client.post(
        url,
        payload,
        format="json",
        HTTP_IDEMPOTENCY_KEY="renewal-order-one",
    )
    replay = client.post(
        url,
        payload,
        format="json",
        HTTP_IDEMPOTENCY_KEY="renewal-order-one",
    )
    assert first.status_code == 201, first.json()
    assert replay.status_code == 201, replay.json()
    assert first.json()["id"] == replay.json()["id"]
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) FROM licensing.billing_orders WHERE tenant_id=%s AND order_key=%s",
            [accounting_context["tenant"], "renewal-order-one"],
        )
        assert cursor.fetchone()[0] == 1


@pytest.mark.concurrency
@pytest.mark.p0
@pytest.mark.django_db(transaction=True)
def test_concurrent_module_changes_allow_one_revision_winner(
    accounting_context: dict[str, object],
) -> None:
    client = accounting_context["client"]
    assert isinstance(client, APIClient)
    revision = client.get(f"{_company_root(accounting_context)}/admin/modules").json()[
        "policy_revision"
    ]
    user_id = accounting_context["user"]
    url = f"{_company_root(accounting_context)}/admin/module-changes"

    def change(mode: str) -> int:
        close_old_connections()
        from django.contrib.auth import get_user_model

        local_client = APIClient()
        local_client.force_login(get_user_model().objects.get(pk=user_id))
        response = local_client.post(
            url,
            {
                "changes": [{"module_code": "sales", "mode": mode}],
                "reason": f"Concurrent {mode}",
            },
            format="json",
            HTTP_IDEMPOTENCY_KEY=f"concurrent-{mode}-{uuid.uuid4()}",
            HTTP_IF_MATCH=f'"{revision}"',
        )
        close_old_connections()
        return response.status_code

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(change, ["enabled", "read_only"]))
    assert sorted(statuses) == [200, 412]


@pytest.mark.api
@pytest.mark.p0
@pytest.mark.django_db(transaction=True)
def test_platform_license_lifecycle_requires_operator(
    accounting_context: dict[str, object],
) -> None:
    tenant_client = accounting_context["client"]
    assert isinstance(tenant_client, APIClient)
    license_id = accounting_context["license"]
    url = f"/platform-api/v1/licenses/{license_id}/suspend"
    denied = tenant_client.post(
        url,
        {"reason": "Unauthorized attempt"},
        format="json",
        HTTP_IDEMPOTENCY_KEY="tenant-suspend-attempt",
    )
    assert denied.status_code == 403

    operator = get_user_model().objects.create_superuser(
        email=f"operator-{uuid.uuid4()}@example.com",
        password="test-operator-password",
    )
    operator_client = APIClient()
    operator_client.force_login(operator)
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT entitlement_revision FROM licensing.tenant_product_bindings
            WHERE tenant_id=%s AND product_id=%s
            """,
            [accounting_context["tenant"], accounting_context["product"]],
        )
        before_revision = cursor.fetchone()[0]

    first = operator_client.post(
        url,
        {"reason": "Payment investigation"},
        format="json",
        HTTP_IDEMPOTENCY_KEY="operator-suspend-one",
    )
    replay = operator_client.post(
        url,
        {"reason": "Payment investigation"},
        format="json",
        HTTP_IDEMPOTENCY_KEY="operator-suspend-one",
    )
    assert first.status_code == 200, first.json()
    assert replay.status_code == 200, replay.json()
    assert first.json() == replay.json()
    assert first.json()["status"] == "suspended"
    assert first.json()["entitlement_revision"] > before_revision

    revoked = operator_client.post(
        f"/platform-api/v1/licenses/{license_id}/revoke",
        {"reason": "Confirmed abuse"},
        format="json",
        HTTP_IDEMPOTENCY_KEY="operator-revoke-one",
    )
    assert revoked.status_code == 200, revoked.json()
    assert revoked.json()["status"] == "revoked"
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT status FROM licensing.licenses WHERE id=%s",
            [license_id],
        )
        assert cursor.fetchone()[0] == "revoked"
        cursor.execute(
            "SELECT count(*) FROM platform.operator_audit_events "
            "WHERE object_id=%s AND object_type='license'",
            [str(license_id)],
        )
        assert cursor.fetchone()[0] == 2


@pytest.mark.api
@pytest.mark.p0
@pytest.mark.django_db(transaction=True)
def test_platform_issue_persists_only_credential_digest(
    accounting_context: dict[str, object],
) -> None:
    operator = get_user_model().objects.create_superuser(
        email=f"issuer-{uuid.uuid4()}@example.com",
        password="test-operator-password",
    )
    client = APIClient()
    client.force_login(operator)
    response = client.post(
        "/platform-api/v1/licenses",
        {
            "tenant_id": str(accounting_context["tenant"]),
            "plan_version_id": str(accounting_context["version"]),
            "reason": "Approved commercial order",
        },
        format="json",
        HTTP_IDEMPOTENCY_KEY="issue-license-one",
    )
    assert response.status_code == 201, response.json()
    credential = response.json()["credential"]
    assert credential.startswith("qa_")
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT license_number_hash,license_number_last4 FROM licensing.licenses WHERE id=%s",
            [response.json()["id"]],
        )
        digest, last_four = cursor.fetchone()
        assert bytes(digest) == hashlib.sha256(credential.strip().encode()).digest()
        assert last_four == credential[-4:]
        cursor.execute(
            """
            SELECT result_reference->>'credential'
            FROM platform.operator_command_receipts
            WHERE operation='license.issue' AND idempotency_key='issue-license-one'
            """
        )
        assert cursor.fetchone()[0] is None
