import concurrent.futures
import hashlib
import threading
import uuid

import pytest
from django.contrib.auth import get_user_model
from django.db import close_old_connections, connection, transaction
from rest_framework.test import APIClient

from common.access.scopes import CompanyScope, assert_company_write, bind_and_verify_company
from common.api.errors import PermissionDenied


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
def test_module_batch_rollback_and_company_policies_are_independent(
    accounting_context: dict[str, object],
) -> None:
    client = accounting_context["client"]
    assert isinstance(client, APIClient)
    root = _company_root(accounting_context)
    modules = client.get(f"{root}/admin/modules").json()
    revision = modules["policy_revision"]
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) FROM erp.company_audit_events WHERE company_id=%s",
            [accounting_context["company"]],
        )
        audit_before = cursor.fetchone()[0]
        cursor.execute(
            "SELECT count(*) FROM erp.outbox_events WHERE company_id=%s",
            [accounting_context["company"]],
        )
        outbox_before = cursor.fetchone()[0]

    rejected = client.post(
        f"{root}/admin/module-changes",
        {
            "changes": [
                {"module_code": "sales", "mode": "enabled"},
                {"module_code": "accounting", "mode": "disabled"},
            ],
            "reason": "Invalid mixed batch",
        },
        format="json",
        HTTP_IDEMPOTENCY_KEY="invalid-mixed-module-batch",
        HTTP_IF_MATCH=f'"{revision}"',
    )
    assert rejected.status_code == 409, rejected.json()

    other_company = uuid.uuid4()
    with connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO erp.companies(
                id,tenant_id,code,legal_name,functional_currency
            ) VALUES (%s,%s,'SECOND','Second Company','USD')
            """,
            [other_company, accounting_context["tenant"]],
        )
        cursor.execute(
            """
            INSERT INTO identity.company_memberships(tenant_id,company_id,user_id)
            VALUES (%s,%s,%s)
            """,
            [accounting_context["tenant"], other_company, accounting_context["user"]],
        )
        cursor.execute(
            """
            SELECT p.revision,s.mode
            FROM erp.company_policy_state p
            JOIN erp.company_module_settings s ON s.company_id=p.company_id
            WHERE p.company_id=%s AND s.module_code='sales'
            """,
            [accounting_context["company"]],
        )
        assert cursor.fetchone() == (revision, "disabled")
        cursor.execute(
            "SELECT count(*) FROM erp.company_audit_events WHERE company_id=%s",
            [accounting_context["company"]],
        )
        assert cursor.fetchone()[0] == audit_before
        cursor.execute(
            "SELECT count(*) FROM erp.outbox_events WHERE company_id=%s",
            [accounting_context["company"]],
        )
        assert cursor.fetchone()[0] == outbox_before

    enabled = client.post(
        f"{root}/admin/module-changes",
        {
            "changes": [{"module_code": "sales", "mode": "enabled"}],
            "reason": "Enable sales only for the main company",
        },
        format="json",
        HTTP_IDEMPOTENCY_KEY="enable-main-company-sales",
        HTTP_IF_MATCH=f'"{revision}"',
    )
    assert enabled.status_code == 200, enabled.json()
    other_root = (
        f"/api/v1/tenants/{accounting_context['tenant']}/companies/{other_company}"
    )
    other_modules = client.get(f"{other_root}/admin/modules")
    assert other_modules.status_code == 200, other_modules.json()
    other_sales = next(
        module
        for module in other_modules.json()["modules"]
        if module["module_code"] == "sales"
    )
    assert other_sales["configured_mode"] == "disabled"


@pytest.mark.api
@pytest.mark.p0
@pytest.mark.django_db(transaction=True)
def test_read_only_module_blocks_the_authoritative_write_gate(
    accounting_context: dict[str, object],
) -> None:
    client = accounting_context["client"]
    assert isinstance(client, APIClient)
    root = _company_root(accounting_context)
    revision = client.get(f"{root}/admin/modules").json()["policy_revision"]
    enabled = client.post(
        f"{root}/admin/module-changes",
        {
            "changes": [{"module_code": "sales", "mode": "enabled"}],
            "reason": "Enable sales",
        },
        format="json",
        HTTP_IDEMPOTENCY_KEY="sales-before-read-only",
        HTTP_IF_MATCH=f'"{revision}"',
    )
    assert enabled.status_code == 200, enabled.json()
    read_only = client.post(
        f"{root}/admin/module-changes",
        {
            "changes": [{"module_code": "sales", "mode": "read_only"}],
            "reason": "Freeze new sales activity",
        },
        format="json",
        HTTP_IDEMPOTENCY_KEY="sales-read-only",
        HTTP_IF_MATCH=f'"{enabled.json()["policy_revision"]}"',
    )
    assert read_only.status_code == 200, read_only.json()

    scope = CompanyScope(
        tenant_id=uuid.UUID(str(accounting_context["tenant"])),
        company_id=uuid.UUID(str(accounting_context["company"])),
        user_id=uuid.UUID(str(accounting_context["user"])),
    )
    with pytest.raises(PermissionDenied) as denied, transaction.atomic():
        bind_and_verify_company(scope)
        assert_company_write(scope.company_id, "sales", "company.modules.manage")
    assert denied.value.default_code == "MODULE_WRITE_DENIED"

    capabilities = client.get(f"{root}/capabilities").json()
    sales = next(
        module for module in capabilities["modules"] if module["module_code"] == "sales"
    )
    assert sales["read_allowed"] is True
    assert sales["write_allowed"] is False
    assert "MODULE_READ_ONLY" in sales["denial_reasons"]


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
    heartbeat_after_deactivation = client.post(
        f"{root}/activations/{activation_id}/heartbeat", {}, format="json"
    )
    assert heartbeat.status_code == 200, heartbeat.json()
    assert deactivate.status_code == 200, deactivate.json()
    assert deactivate.json()["deactivated_at"] is not None
    assert heartbeat_after_deactivation.status_code == 404


@pytest.mark.concurrency
@pytest.mark.p0
@pytest.mark.django_db(transaction=True)
def test_concurrent_activations_never_exceed_quota(
    accounting_context: dict[str, object],
) -> None:
    user_id = accounting_context["user"]
    url = f"{_license_root(accounting_context)}/activations"

    def activate(number: int) -> int:
        close_old_connections()
        local_client = APIClient()
        local_client.force_login(get_user_model().objects.get(pk=user_id))
        response = local_client.post(
            url,
            {"device_fingerprint": f"concurrent-device-fingerprint-{number}"},
            format="json",
            HTTP_IDEMPOTENCY_KEY=f"concurrent-activation-{number}",
        )
        close_old_connections()
        return response.status_code

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(activate, [1, 2]))
    assert sorted(statuses) == [201, 409]
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT count(*) FROM licensing.license_activations
            WHERE license_id=%s AND deactivated_at IS NULL
            """,
            [accounting_context["license"]],
        )
        assert cursor.fetchone()[0] == 1


@pytest.mark.api
@pytest.mark.p0
@pytest.mark.django_db(transaction=True)
def test_license_expiry_returns_a_stable_write_denial(
    accounting_context: dict[str, object],
) -> None:
    version_id = uuid.uuid4()
    expired_license_id = uuid.uuid4()
    with connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO licensing.plan_versions(
                id,product_id,plan_id,version_number,term_unit,term_count,
                max_activations,price_currency,price_amount,published_at
            ) VALUES (%s,%s,%s,2,'day',1,1,'USD',25,clock_timestamp())
            """,
            [version_id, accounting_context["product"], accounting_context["plan"]],
        )
        cursor.execute(
            """
            INSERT INTO licensing.licenses(
                id,tenant_id,product_id,license_number_hash,license_number_last4,status
            ) VALUES (%s,%s,%s,%s,'9999','active')
            """,
            [
                expired_license_id,
                accounting_context["tenant"],
                accounting_context["product"],
                uuid.uuid4().bytes * 2,
            ],
        )
        cursor.execute(
            """
            INSERT INTO licensing.license_terms(
                tenant_id,license_id,plan_version_id,term_unit_snapshot,
                term_count_snapshot,starts_at,expires_at,max_activations_snapshot
            ) VALUES (%s,%s,%s,'day',1,
                      clock_timestamp()-interval '2 days',
                      clock_timestamp()-interval '1 day',1)
            """,
            [accounting_context["tenant"], expired_license_id, version_id],
        )
        cursor.execute(
            """
            UPDATE licensing.tenant_product_bindings SET license_id=%s
            WHERE tenant_id=%s AND product_id=%s
            """,
            [
                expired_license_id,
                accounting_context["tenant"],
                accounting_context["product"],
            ],
        )

    client = accounting_context["client"]
    assert isinstance(client, APIClient)
    response = client.post(
        f"{_company_root(accounting_context)}/accounting/accounts",
        {
            "code": "1999",
            "name": "Expired license write",
            "account_type": "asset",
            "normal_balance": "debit",
        },
        format="json",
    )
    assert response.status_code == 403, response.json()
    assert response.json()["error"]["code"] == "LICENSE_EXPIRED"


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


@pytest.mark.api
@pytest.mark.p0
@pytest.mark.django_db(transaction=True)
def test_issued_license_requires_operator_assignment(
    accounting_context: dict[str, object],
) -> None:
    operator = get_user_model().objects.create_superuser(
        email=f"redeem-issuer-{uuid.uuid4()}@example.com",
        password="test-operator-password",
    )
    operator_client = APIClient()
    operator_client.force_login(operator)
    issued = operator_client.post(
        "/platform-api/v1/licenses",
        {
            "tenant_id": str(accounting_context["tenant"]),
            "plan_version_id": str(accounting_context["version"]),
            "reason": "Approved replacement license",
        },
        format="json",
        HTTP_IDEMPOTENCY_KEY="issue-license-for-redemption",
    )
    assert issued.status_code == 201, issued.json()
    new_license_id = issued.json()["id"]
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT license_id FROM licensing.tenant_product_bindings
            WHERE tenant_id=%s AND product_id=%s
            """,
            [accounting_context["tenant"], accounting_context["product"]],
        )
        assert str(cursor.fetchone()[0]) == str(accounting_context["license"])

    client = accounting_context["client"]
    assert isinstance(client, APIClient)
    url = f"{_license_root(accounting_context)}/redeem"
    assert (
        client.post(url, {"credential": issued.json()["credential"]}, format="json").status_code
        == 404
    )
    assign_url = f"/platform-api/v1/licenses/{new_license_id}/assign"
    assert client.post(assign_url, {"reason": "Attempt"}, format="json").status_code == 403
    blocked = operator_client.post(
        assign_url, {"reason": "Approved replacement"}, format="json",
        HTTP_IDEMPOTENCY_KEY="assign-license-blocked",
    )
    assert blocked.status_code == 409, blocked.json()
    with connection.cursor() as cursor:
        cursor.execute(
            "UPDATE licensing.tenant_product_bindings SET license_id=NULL "
            "WHERE tenant_id=%s AND product_id=%s",
            [accounting_context["tenant"], accounting_context["product"]],
        )
    first = operator_client.post(
        assign_url, {"reason": "Approved replacement"}, format="json",
        HTTP_IDEMPOTENCY_KEY="assign-license-one",
    )
    replay = operator_client.post(
        assign_url, {"reason": "Approved replacement"}, format="json",
        HTTP_IDEMPOTENCY_KEY="assign-license-one",
    )
    assert first.status_code == 200, first.json()
    assert replay.json() == first.json()
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT b.license_id,l.redeemed_at,l.redeemed_by,
                   count(e.id) FILTER (WHERE e.event_type='redeemed')
            FROM licensing.tenant_product_bindings b
            JOIN licensing.licenses l ON l.id=b.license_id
            LEFT JOIN licensing.license_events e ON e.license_id=l.id
            WHERE b.tenant_id=%s AND b.product_id=%s
            GROUP BY b.license_id,l.redeemed_at,l.redeemed_by
            """,
            [accounting_context["tenant"], accounting_context["product"]],
        )
        bound_license, redeemed_at, redeemed_by, event_count = cursor.fetchone()
    assert str(bound_license) == new_license_id
    assert redeemed_at is not None
    assert str(redeemed_by) == str(operator.pk)
    assert event_count == 1


@pytest.mark.api
@pytest.mark.p0
@pytest.mark.django_db(transaction=True)
def test_license_summary_and_raw_credentials_require_authenticated_membership(
    accounting_context: dict[str, object],
) -> None:
    outsider = get_user_model().objects.create_user(
        email=f"outsider-{uuid.uuid4()}@example.com",
        password="outsider-password",
    )
    outsider_client = APIClient()
    outsider_client.force_login(outsider)
    denied = outsider_client.get(_license_root(accounting_context))
    assert denied.status_code == 404

    anonymous = APIClient()
    raw_bearer = anonymous.get(
        _license_root(accounting_context),
        HTTP_AUTHORIZATION="Bearer qa_this-is-not-an-identity-credential",
    )
    assert raw_bearer.status_code in {401, 403}


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


@pytest.mark.concurrency
@pytest.mark.p0
@pytest.mark.django_db(transaction=True)
def test_module_disable_waits_for_an_authorized_business_transaction(
    accounting_context: dict[str, object],
) -> None:
    client = accounting_context["client"]
    assert isinstance(client, APIClient)
    root = _company_root(accounting_context)
    revision = client.get(f"{root}/admin/modules").json()["policy_revision"]
    enabled = client.post(
        f"{root}/admin/module-changes",
        {
            "changes": [{"module_code": "sales", "mode": "enabled"}],
            "reason": "Prepare policy lock race",
        },
        format="json",
        HTTP_IDEMPOTENCY_KEY="policy-race-enable-sales",
        HTTP_IF_MATCH=f'"{revision}"',
    )
    assert enabled.status_code == 200, enabled.json()

    authorized = threading.Event()
    release_command = threading.Event()
    disable_started = threading.Event()
    scope = CompanyScope(
        tenant_id=uuid.UUID(str(accounting_context["tenant"])),
        company_id=uuid.UUID(str(accounting_context["company"])),
        user_id=uuid.UUID(str(accounting_context["user"])),
    )

    def accepted_command() -> str:
        close_old_connections()
        try:
            with transaction.atomic():
                bind_and_verify_company(scope)
                assert_company_write(scope.company_id, "sales", "company.modules.manage")
                authorized.set()
                assert release_command.wait(timeout=5)
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT mode FROM erp.company_module_settings
                        WHERE company_id=%s AND module_code='sales'
                        """,
                        [scope.company_id],
                    )
                    return str(cursor.fetchone()[0])
        finally:
            close_old_connections()

    def disable_module() -> int:
        close_old_connections()
        try:
            local_client = APIClient()
            local_client.force_login(
                get_user_model().objects.get(pk=accounting_context["user"])
            )
            disable_started.set()
            response = local_client.post(
                f"{root}/admin/module-changes",
                {
                    "changes": [{"module_code": "sales", "mode": "disabled"}],
                    "reason": "Disable after accepted command",
                },
                format="json",
                HTTP_IDEMPOTENCY_KEY="policy-race-disable-sales",
                HTTP_IF_MATCH=f'"{enabled.json()["policy_revision"]}"',
            )
            return response.status_code
        finally:
            close_old_connections()

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        accepted_future = pool.submit(accepted_command)
        assert authorized.wait(timeout=5)
        disable_future = pool.submit(disable_module)
        assert disable_started.wait(timeout=5)
        _, pending = concurrent.futures.wait([disable_future], timeout=0.2)
        assert pending == {disable_future}
        release_command.set()
        assert accepted_future.result(timeout=5) == "enabled"
        assert disable_future.result(timeout=5) == 200

    with pytest.raises(PermissionDenied) as denied, transaction.atomic():
        bind_and_verify_company(scope)
        assert_company_write(scope.company_id, "sales", "company.modules.manage")
    assert denied.value.default_code == "MODULE_WRITE_DENIED"


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

    resumed = operator_client.post(
        f"/platform-api/v1/licenses/{license_id}/resume",
        {"reason": "Payment investigation cleared"},
        format="json",
        HTTP_IDEMPOTENCY_KEY="operator-resume-one",
    )
    assert resumed.status_code == 200, resumed.json()
    assert resumed.json()["status"] == "active"
    assert resumed.json()["entitlement_revision"] > first.json()["entitlement_revision"]

    revoked = operator_client.post(
        f"/platform-api/v1/licenses/{license_id}/revoke",
        {"reason": "Confirmed abuse"},
        format="json",
        HTTP_IDEMPOTENCY_KEY="operator-revoke-one",
    )
    assert revoked.status_code == 200, revoked.json()
    assert revoked.json()["status"] == "revoked"
    terminal = operator_client.post(
        f"/platform-api/v1/licenses/{license_id}/resume",
        {"reason": "Invalid attempt to revive a revoked license"},
        format="json",
        HTTP_IDEMPOTENCY_KEY="operator-resume-revoked",
    )
    assert terminal.status_code == 409
    assert terminal.json()["error"]["code"] == "LICENSE_REVOKED"
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
        assert cursor.fetchone()[0] == 3


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


@pytest.mark.api
@pytest.mark.p0
@pytest.mark.django_db(transaction=True)
def test_operator_can_revoke_an_unredeemed_license_without_rebinding(
    accounting_context: dict[str, object],
) -> None:
    operator = get_user_model().objects.create_superuser(
        email=f"unredeemed-revoker-{uuid.uuid4()}@example.com",
        password="test-operator-password",
    )
    client = APIClient()
    client.force_login(operator)
    issued = client.post(
        "/platform-api/v1/licenses",
        {
            "tenant_id": str(accounting_context["tenant"]),
            "plan_version_id": str(accounting_context["version"]),
            "reason": "Credential delivery pending",
        },
        format="json",
        HTTP_IDEMPOTENCY_KEY="issue-unredeemed-license",
    )
    assert issued.status_code == 201, issued.json()
    revoked = client.post(
        f"/platform-api/v1/licenses/{issued.json()['id']}/revoke",
        {"reason": "Credential delivery was compromised"},
        format="json",
        HTTP_IDEMPOTENCY_KEY="revoke-unredeemed-license",
    )
    assert revoked.status_code == 200, revoked.json()
    assert revoked.json()["status"] == "revoked"
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT license_id FROM licensing.tenant_product_bindings
            WHERE tenant_id=%s AND product_id=%s
            """,
            [accounting_context["tenant"], accounting_context["product"]],
        )
        assert str(cursor.fetchone()[0]) == str(accounting_context["license"])
