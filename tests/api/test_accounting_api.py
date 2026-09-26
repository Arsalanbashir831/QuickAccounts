import concurrent.futures
import time
import uuid

import psycopg
import pytest
from django.conf import settings as django_settings
from django.contrib.auth import get_user_model
from django.db import DatabaseError, close_old_connections, connection, transaction
from rest_framework.test import APIClient

from apps.accounting.services.accounting import post_entry
from common.access.scopes import CompanyScope


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


def _entry_url(context: dict[str, object]) -> str:
    return f"/api/v1/tenants/{context['tenant']}/companies/{context['company']}/accounting/entries"


def _balanced_entry_payload(context: dict[str, object]) -> dict[str, object]:
    return {
        "journal_id": str(context["journal"]),
        "fiscal_period_id": str(context["period"]),
        "entry_date": "2026-09-26",
        "description": "Opening entry",
        "lines": [
            {
                "account_id": str(context["cash"]),
                "transaction_currency": "USD",
                "exchange_rate": "1.0000000000",
                "transaction_debit": "100.000000",
                "transaction_credit": "0.000000",
                "debit_amount": "100.000000",
                "credit_amount": "0.000000",
            },
            {
                "account_id": str(context["equity"]),
                "transaction_currency": "USD",
                "exchange_rate": "1.0000000000",
                "transaction_debit": "0.000000",
                "transaction_credit": "100.000000",
                "debit_amount": "0.000000",
                "credit_amount": "100.000000",
            },
        ],
    }


@pytest.mark.api
@pytest.mark.p0
@pytest.mark.django_db(transaction=True)
def test_manual_journal_post_is_idempotent(accounting_context: dict[str, object]) -> None:
    client = accounting_context["client"]
    assert isinstance(client, APIClient)
    create = client.post(
        _entry_url(accounting_context),
        {
            "journal_id": str(accounting_context["journal"]),
            "fiscal_period_id": str(accounting_context["period"]),
            "entry_date": "2026-09-26",
            "description": "Opening entry",
            "lines": [
                {
                    "account_id": str(accounting_context["cash"]),
                    "transaction_currency": "USD",
                    "exchange_rate": "1.0000000000",
                    "transaction_debit": "100.000000",
                    "transaction_credit": "0.000000",
                    "debit_amount": "100.000000",
                    "credit_amount": "0.000000",
                },
                {
                    "account_id": str(accounting_context["equity"]),
                    "transaction_currency": "USD",
                    "exchange_rate": "1.0000000000",
                    "transaction_debit": "0.000000",
                    "transaction_credit": "100.000000",
                    "debit_amount": "0.000000",
                    "credit_amount": "100.000000",
                },
            ],
        },
        format="json",
    )
    assert create.status_code == 201, create.json()
    entry_id = create.json()["id"]
    post_url = f"{_entry_url(accounting_context)}/{entry_id}/post"
    headers = {"HTTP_IDEMPOTENCY_KEY": "post-001", "HTTP_IF_MATCH": '"1"'}
    first = client.post(post_url, {}, format="json", **headers)
    second = client.post(post_url, {}, format="json", **headers)
    assert first.status_code == 200, first.json()
    assert second.status_code == 200, second.json()
    assert first.json() == second.json()
    assert first.json()["status"] == "posted"
    assert first.json()["entry_number"] == "GJ-000001"
    with pytest.raises(DatabaseError), transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE erp.journal_entries SET description='forbidden' WHERE id=%s",
                [entry_id],
            )
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) FROM erp.journal_entries WHERE company_id=%s AND status='posted'",
            [accounting_context["company"]],
        )
        assert cursor.fetchone()[0] == 1
        cursor.execute(
            "SELECT count(*) FROM erp.api_command_receipts "
            "WHERE company_id=%s AND status='completed'",
            [accounting_context["company"]],
        )
        assert cursor.fetchone()[0] == 1
        cursor.execute(
            "SELECT count(*) FROM erp.outbox_events WHERE company_id=%s",
            [accounting_context["company"]],
        )
        assert cursor.fetchone()[0] == 1

    mismatch = client.post(
        post_url,
        {},
        format="json",
        HTTP_IDEMPOTENCY_KEY="post-001",
        HTTP_IF_MATCH='"2"',
    )
    assert mismatch.status_code == 409
    assert mismatch.json()["error"]["code"] == "IDEMPOTENCY_PAYLOAD_MISMATCH"

    reverse_url = f"{_entry_url(accounting_context)}/{entry_id}/reverse"
    reverse_payload = {
        "fiscal_period_id": str(accounting_context["period"]),
        "entry_date": "2026-09-26",
        "reason": "Correct the opening entry",
    }
    reversal = client.post(
        reverse_url,
        reverse_payload,
        format="json",
        HTTP_IDEMPOTENCY_KEY="reverse-001",
    )
    replay = client.post(
        reverse_url,
        reverse_payload,
        format="json",
        HTTP_IDEMPOTENCY_KEY="reverse-001",
    )
    assert reversal.status_code == 201, reversal.json()
    assert replay.status_code == 201
    assert replay.json() == reversal.json()
    assert reversal.json()["reversal_of_entry_id"] == entry_id
    assert reversal.json()["lines"][0]["credit_amount"] == "100.000000"

    report_prefix = (
        f"/api/v1/tenants/{accounting_context['tenant']}"
        f"/companies/{accounting_context['company']}/reports"
    )
    trial = client.get(f"{report_prefix}/trial-balance?date_from=2026-09-01&date_to=2026-09-30")
    activity = client.get(
        f"{report_prefix}/period-activity?period_id={accounting_context['period']}"
    )
    general_ledger = client.get(
        f"{report_prefix}/general-ledger?account_id={accounting_context['cash']}"
        "&date_from=2026-09-01&date_to=2026-09-30"
    )
    assert trial.status_code == 200, trial.json()
    assert activity.status_code == 200, activity.json()
    assert general_ledger.status_code == 200, general_ledger.json()
    cash = next(row for row in trial.json()["results"] if row["account_code"] == "1000")
    assert cash["closing_net_debit"] == "0.000000"
    assert general_ledger.json()["results"][-1]["running_balance"] == "0.000000"

    period_url = f"{_entry_url(accounting_context).removesuffix('/entries')}/periods"
    close = client.post(
        f"{period_url}/{accounting_context['period']}/close",
        {"reason": "September reconciliation complete"},
        format="json",
        HTTP_IDEMPOTENCY_KEY="close-001",
        HTTP_IF_MATCH='"1"',
    )
    close_replay = client.post(
        f"{period_url}/{accounting_context['period']}/close",
        {"reason": "September reconciliation complete"},
        format="json",
        HTTP_IDEMPOTENCY_KEY="close-001",
        HTTP_IF_MATCH='"1"',
    )
    assert close.status_code == 200, close.json()
    assert close.json()["state"] == "closed"
    assert close_replay.json() == close.json()
    reopen = client.post(
        f"{period_url}/{accounting_context['period']}/reopen",
        {"reason": "Approved correction window"},
        format="json",
        HTTP_IDEMPOTENCY_KEY="reopen-001",
        HTTP_IF_MATCH=f'"{close.json()["row_version"]}"',
    )
    assert reopen.status_code == 200, reopen.json()
    assert reopen.json()["state"] == "open"


@pytest.mark.api
@pytest.mark.p0
@pytest.mark.django_db(transaction=True)
def test_post_requires_concurrency_and_idempotency_headers(
    accounting_context: dict[str, object],
) -> None:
    client = accounting_context["client"]
    assert isinstance(client, APIClient)
    response = client.post(
        f"{_entry_url(accounting_context)}/{uuid.uuid4()}/post",
        {},
        format="json",
    )
    assert response.status_code == 428
    assert response.json()["error"]["code"] == "PRECONDITION_REQUIRED"
    assert response["X-Request-ID"]


@pytest.mark.api
@pytest.mark.p0
@pytest.mark.django_db(transaction=True)
def test_cross_tenant_company_scope_is_hidden(accounting_context: dict[str, object]) -> None:
    client = accounting_context["client"]
    assert isinstance(client, APIClient)
    response = client.get(
        f"/api/v1/tenants/{accounting_context['tenant']}/companies/{uuid.uuid4()}"
        "/accounting/accounts"
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "SCOPE_NOT_FOUND"


@pytest.mark.api
@pytest.mark.p0
@pytest.mark.django_db(transaction=True)
def test_unbalanced_entry_and_period_close_blockers_are_atomic(
    accounting_context: dict[str, object],
) -> None:
    client = accounting_context["client"]
    assert isinstance(client, APIClient)
    payload = _balanced_entry_payload(accounting_context)
    lines = payload["lines"]
    assert isinstance(lines, list) and isinstance(lines[1], dict)
    lines[1]["transaction_credit"] = "90.000000"
    lines[1]["credit_amount"] = "90.000000"
    create = client.post(_entry_url(accounting_context), payload, format="json")
    assert create.status_code == 201, create.json()
    entry_id = create.json()["id"]
    post = client.post(
        f"{_entry_url(accounting_context)}/{entry_id}/post",
        {},
        format="json",
        HTTP_IDEMPOTENCY_KEY="unbalanced-post",
        HTTP_IF_MATCH='"1"',
    )
    assert post.status_code == 409
    assert post.json()["error"]["code"] == "TRANSACTION_CURRENCY_UNBALANCED"

    period_url = f"{_entry_url(accounting_context).removesuffix('/entries')}/periods"
    close = client.post(
        f"{period_url}/{accounting_context['period']}/close",
        {"reason": "Attempt close"},
        format="json",
        HTTP_IDEMPOTENCY_KEY="blocked-close",
        HTTP_IF_MATCH='"1"',
    )
    assert close.status_code == 409
    assert close.json()["error"]["code"] == "PERIOD_RECONCILIATION_BLOCKED"
    assert close.json()["error"]["details"]["blockers"][0]["code"] == "DRAFT_JOURNALS"
    with connection.cursor() as cursor:
        cursor.execute("SELECT status FROM erp.journal_entries WHERE id=%s", [entry_id])
        assert cursor.fetchone()[0] == "draft"
        cursor.execute(
            "SELECT state FROM erp.fiscal_periods WHERE id=%s",
            [accounting_context["period"]],
        )
        assert cursor.fetchone()[0] == "open"


@pytest.mark.api
@pytest.mark.p0
@pytest.mark.django_db(transaction=True)
def test_draft_revision_rejects_stale_update(accounting_context: dict[str, object]) -> None:
    client = accounting_context["client"]
    assert isinstance(client, APIClient)
    create = client.post(
        _entry_url(accounting_context),
        _balanced_entry_payload(accounting_context),
        format="json",
    )
    entry_url = f"{_entry_url(accounting_context)}/{create.json()['id']}"
    first = client.patch(
        entry_url,
        {"description": "First edit"},
        format="json",
        HTTP_IF_MATCH='"1"',
    )
    stale = client.patch(
        entry_url,
        {"description": "Stale overwrite"},
        format="json",
        HTTP_IF_MATCH='"1"',
    )
    assert first.status_code == 200, first.json()
    assert stale.status_code == 412
    assert stale.json()["error"]["code"] == "REVISION_MISMATCH"


@pytest.mark.api
@pytest.mark.p0
@pytest.mark.django_db(transaction=True)
def test_posting_into_closed_period_has_no_side_effect(
    accounting_context: dict[str, object],
) -> None:
    client = accounting_context["client"]
    assert isinstance(client, APIClient)
    with connection.cursor() as cursor:
        cursor.execute(
            "UPDATE erp.fiscal_periods SET state='closed' WHERE id=%s",
            [accounting_context["period"]],
        )
    create = client.post(
        _entry_url(accounting_context),
        _balanced_entry_payload(accounting_context),
        format="json",
    )
    assert create.status_code == 201
    entry_id = create.json()["id"]
    post = client.post(
        f"{_entry_url(accounting_context)}/{entry_id}/post",
        {},
        format="json",
        HTTP_IDEMPOTENCY_KEY="closed-period-post",
        HTTP_IF_MATCH='"1"',
    )
    assert post.status_code == 409
    assert post.json()["error"]["code"] == "PERIOD_NOT_OPEN"
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT status,entry_number FROM erp.journal_entries WHERE id=%s",
            [entry_id],
        )
        assert cursor.fetchone()[0] == "draft"
        cursor.execute(
            "SELECT count(*) FROM erp.api_command_receipts WHERE idempotency_key=%s",
            ["closed-period-post"],
        )
        assert cursor.fetchone()[0] == 0


@pytest.mark.api
@pytest.mark.p1
@pytest.mark.django_db(transaction=True)
def test_multi_currency_rounding_and_evidence(accounting_context: dict[str, object]) -> None:
    client = accounting_context["client"]
    assert isinstance(client, APIClient)
    with connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO erp.currencies(code,name,minor_units) VALUES ('BHD','Bahraini Dinar',3) "
            "ON CONFLICT DO NOTHING"
        )
    payload = _balanced_entry_payload(accounting_context)
    lines = payload["lines"]
    assert isinstance(lines, list)
    for index, line in enumerate(lines):
        assert isinstance(line, dict)
        line["transaction_currency"] = "BHD"
        line["exchange_rate"] = "0.3760000000"
        line["transaction_debit"] = "10.000000" if index == 0 else "0.000000"
        line["transaction_credit"] = "0.000000" if index == 0 else "10.000000"
        line["debit_amount"] = "3.760000" if index == 0 else "0.000000"
        line["credit_amount"] = "0.000000" if index == 0 else "3.760000"
    create = client.post(_entry_url(accounting_context), payload, format="json")
    entry_id = create.json()["id"]
    post = client.post(
        f"{_entry_url(accounting_context)}/{entry_id}/post",
        {},
        format="json",
        HTTP_IDEMPOTENCY_KEY="bhd-post",
        HTTP_IF_MATCH='"1"',
    )
    assert post.status_code == 200, post.json()
    assert post.json()["lines"][0]["exchange_rate"] == "0.3760000000"
    assert post.json()["lines"][0]["debit_amount"] == "3.760000"


@pytest.mark.api
@pytest.mark.p0
@pytest.mark.django_db(transaction=True)
def test_source_linked_journal_cannot_be_reversed_directly(
    accounting_context: dict[str, object],
) -> None:
    client = accounting_context["client"]
    assert isinstance(client, APIClient)
    create = client.post(
        _entry_url(accounting_context),
        _balanced_entry_payload(accounting_context),
        format="json",
    )
    entry_id = create.json()["id"]
    with connection.cursor() as cursor:
        cursor.execute(
            "UPDATE erp.journal_entries SET source_type='sales_invoice',source_id=%s WHERE id=%s",
            [uuid.uuid4(), entry_id],
        )
        cursor.execute(
            "SELECT row_version FROM erp.journal_entries WHERE id=%s",
            [entry_id],
        )
        revision = cursor.fetchone()[0]
    post = client.post(
        f"{_entry_url(accounting_context)}/{entry_id}/post",
        {},
        format="json",
        HTTP_IDEMPOTENCY_KEY="source-post",
        HTTP_IF_MATCH=f'"{revision}"',
    )
    assert post.status_code == 200, post.json()
    reverse = client.post(
        f"{_entry_url(accounting_context)}/{entry_id}/reverse",
        {
            "fiscal_period_id": str(accounting_context["period"]),
            "entry_date": "2026-09-26",
            "reason": "Must use source correction",
        },
        format="json",
        HTTP_IDEMPOTENCY_KEY="source-reverse",
    )
    assert reverse.status_code == 409
    assert reverse.json()["error"]["code"] == "SOURCE_CORRECTION_REQUIRED"


@pytest.mark.api
@pytest.mark.p0
@pytest.mark.django_db(transaction=True)
def test_required_dimensions_are_enforced_and_preserved(
    accounting_context: dict[str, object],
) -> None:
    client = accounting_context["client"]
    assert isinstance(client, APIClient)
    dimension_type_id = uuid.uuid4()
    dimension_value_id = uuid.uuid4()
    with connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO erp.dimension_types(id,company_id,code,name,is_required)
            VALUES (%s,%s,'DEPT','Department',true)
            """,
            [dimension_type_id, accounting_context["company"]],
        )
        cursor.execute(
            """
            INSERT INTO erp.dimension_values(id,company_id,dimension_type_id,code,name)
            VALUES (%s,%s,%s,'FIN','Finance')
            """,
            [dimension_value_id, accounting_context["company"], dimension_type_id],
        )
    payload = _balanced_entry_payload(accounting_context)
    create = client.post(_entry_url(accounting_context), payload, format="json")
    entry_id = create.json()["id"]
    post_url = f"{_entry_url(accounting_context)}/{entry_id}/post"
    missing = client.post(
        post_url,
        {},
        format="json",
        HTTP_IDEMPOTENCY_KEY="missing-dimension",
        HTTP_IF_MATCH='"1"',
    )
    assert missing.status_code == 409
    lines = payload["lines"]
    assert isinstance(lines, list)
    for line in lines:
        assert isinstance(line, dict)
        line["dimensions"] = [
            {
                "dimension_type_id": str(dimension_type_id),
                "dimension_value_id": str(dimension_value_id),
            }
        ]
    updated = client.patch(
        f"{_entry_url(accounting_context)}/{entry_id}",
        {"lines": lines},
        format="json",
        HTTP_IF_MATCH='"1"',
    )
    assert updated.status_code == 200, updated.json()
    posted = client.post(
        post_url,
        {},
        format="json",
        HTTP_IDEMPOTENCY_KEY="with-dimension",
        HTTP_IF_MATCH=f'"{updated.json()["row_version"]}"',
    )
    assert posted.status_code == 200, posted.json()
    assert posted.json()["lines"][0]["dimensions"][0]["dimension_value_id"] == str(
        dimension_value_id
    )


def _service_post(
    context: dict[str, object], entry_id: str, idempotency_key: str
) -> dict[str, object]:
    close_old_connections()
    try:
        scope = CompanyScope(
            uuid.UUID(str(context["tenant"])),
            uuid.UUID(str(context["company"])),
            uuid.UUID(str(context["user"])),
        )
        body, _ = post_entry(
            scope,
            entry_id=uuid.UUID(entry_id),
            expected_revision=1,
            idempotency_key=idempotency_key,
            request_id=idempotency_key,
        )
        return body
    finally:
        close_old_connections()


@pytest.mark.concurrency
@pytest.mark.p0
@pytest.mark.django_db(transaction=True)
def test_two_journals_post_concurrently_with_unique_numbers(
    accounting_context: dict[str, object],
) -> None:
    client = accounting_context["client"]
    assert isinstance(client, APIClient)
    first = client.post(
        _entry_url(accounting_context),
        _balanced_entry_payload(accounting_context),
        format="json",
    )
    second = client.post(
        _entry_url(accounting_context),
        _balanced_entry_payload(accounting_context),
        format="json",
    )
    assert first.status_code == second.status_code == 201
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(_service_post, accounting_context, first.json()["id"], "parallel-1"),
            executor.submit(_service_post, accounting_context, second.json()["id"], "parallel-2"),
        ]
        results = [future.result(timeout=10) for future in futures]
    assert {result["status"] for result in results} == {"posted"}
    assert len({result["entry_number"] for result in results}) == 2


def _direct_connection() -> psycopg.Connection:
    config = django_settings.DATABASES["default"]
    return psycopg.connect(
        dbname=config["NAME"],
        user=config["USER"],
        password=config["PASSWORD"],
        host=config["HOST"],
        port=config["PORT"],
    )


def _close_period_after_lock(company_id: object, period_id: object) -> str:
    with _direct_connection() as closer:
        with closer.cursor() as cursor:
            cursor.execute(
                "SELECT state FROM erp.fiscal_periods WHERE company_id=%s AND id=%s FOR UPDATE",
                [company_id, period_id],
            )
            cursor.execute("SELECT erp.close_fiscal_period(%s,%s)", [company_id, period_id])
            cursor.execute(
                "SELECT state FROM erp.fiscal_periods WHERE company_id=%s AND id=%s",
                [company_id, period_id],
            )
            return cursor.fetchone()[0]


@pytest.mark.concurrency
@pytest.mark.p0
@pytest.mark.django_db(transaction=True)
def test_period_close_waits_for_active_posting(accounting_context: dict[str, object]) -> None:
    client = accounting_context["client"]
    assert isinstance(client, APIClient)
    create = client.post(
        _entry_url(accounting_context),
        _balanced_entry_payload(accounting_context),
        format="json",
    )
    entry_id = create.json()["id"]
    company_id = accounting_context["company"]
    period_id = accounting_context["period"]
    with _direct_connection() as poster:
        with poster.cursor() as cursor:
            cursor.execute("BEGIN")
            cursor.execute(
                "SELECT state FROM erp.fiscal_periods WHERE company_id=%s AND id=%s FOR SHARE",
                [company_id, period_id],
            )
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                close_future = executor.submit(_close_period_after_lock, company_id, period_id)
                time.sleep(0.2)
                assert not close_future.done()
                cursor.execute("SELECT erp.post_journal_entry(%s,%s)", [company_id, entry_id])
                poster.commit()
                assert close_future.result(timeout=10) == "closed"
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT e.status,p.state FROM erp.journal_entries e "
            "JOIN erp.fiscal_periods p ON p.id=e.fiscal_period_id WHERE e.id=%s",
            [entry_id],
        )
        assert cursor.fetchone() == ("posted", "closed")
