import concurrent.futures
import datetime as dt
import uuid
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.db import DatabaseError, close_old_connections, connection, transaction
from rest_framework.test import APIClient

from apps.payments.services import post_payment
from common.access.scopes import CompanyScope
from common.api.errors import APIError
from tests.api.test_purchase_bill_drafts_api import (
    _bind_license,
    _change_module,
    _root,
    _seed_posting_configuration,
    _seed_purchase_facts,
)
from tests.api.test_purchase_bill_posting_api import _create_calculated_bill
from tests.api.test_sales_invoice_drafts_api import _seed_sales_facts
from tests.api.test_sales_invoice_posting_api import (
    _create_calculated_invoice,
)
from tests.api.test_sales_invoice_posting_api import (
    _seed_posting_configuration as _sales_configuration,
)

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.api, pytest.mark.p0]


def _setup(context: dict[str, object], direction: str = "receipt") -> dict[str, object]:
    client = context["client"]
    assert isinstance(client, APIClient)
    _bind_license(context, ("module.payments", "module.sales", "module.purchasing"))
    for module in ("sales", "purchasing", "payments"):
        _change_module(client, context, module, "enabled")
    if direction == "receipt":
        ids = _seed_sales_facts(context)
        posting = _sales_configuration(context, ids)
        draft = _create_calculated_invoice(client, context, ids, f"DRAFT-{uuid.uuid4()}")
        route = "sales/invoices"
    else:
        ids = _seed_purchase_facts(context)
        posting = _seed_posting_configuration(context, ids)
        draft = _create_calculated_bill(client, context, ids, f"DRAFT-{uuid.uuid4()}")
        route = "purchasing/bills"
    source = client.post(
        f"{_root(context)}/{route}/{draft['id']}/post",
        {"fiscal_period_id": str(context["period"]), "journal_id": str(posting["journal"])},
        format="json",
        HTTP_IF_MATCH='"2"',
        HTTP_IDEMPOTENCY_KEY=f"source-{uuid.uuid4()}",
    )
    assert source.status_code == 200, source.json()
    with connection.cursor() as c:
        c.execute("SELECT (clock_timestamp() AT TIME ZONE 'UTC')::date")
        cutoff = max(c.fetchone()[0], dt.date(2026, 9, 27))
        reversal_date = cutoff + dt.timedelta(days=1)
        c.execute(
            "UPDATE erp.fiscal_periods SET ends_on=greatest(ends_on,%s) WHERE id=%s",
            [reversal_date, context["period"]],
        )
        c.execute(
            "INSERT INTO erp.document_sequences(company_id,fiscal_period_id,sequence_code,"
            "prefix,next_value,padding_length) VALUES (%s,%s,'PAYMENT_JOURNAL','PAY-',1,6)",
            [context["company"], context["period"]],
        )
    return {
        "client": client,
        "ids": ids,
        "source": source.json(),
        "direction": direction,
        "cutoff": cutoff.isoformat(),
        "reversal_date": reversal_date.isoformat(),
    }


def _create(context: dict[str, object], setup: dict[str, object], amount: str = "60") -> dict:
    response = setup["client"].post(
        f"{_root(context)}/payments",
        {
            "payment_no": f"PAY-{uuid.uuid4()}",
            "direction": setup["direction"],
            "partner_id": str(setup["ids"]["partner"]),
            "cash_account_id": str(context["cash"]),
            "payment_date": "2026-09-27",
            "currency_code": "USD",
            "amount": amount,
            "payment_method": "bank",
        },
        format="json",
        HTTP_IDEMPOTENCY_KEY=f"create-{uuid.uuid4()}",
    )
    assert response.status_code == 201, response.json()
    return response.json()


def _allocate(
    context: dict[str, object],
    setup: dict[str, object],
    payment: dict,
    amount: str = "60",
    document_id: str | None = None,
) -> object:
    return setup["client"].put(
        f"{_root(context)}/payments/{payment['id']}/allocations",
        {
            "allocations": [
                {
                    "document_id": document_id or setup["source"]["id"],
                    "applied_document_amount": amount,
                    "applied_payment_amount": amount,
                    "allocation_exchange_rate": "1",
                }
            ]
        },
        format="json",
        HTTP_IF_MATCH=f'"{payment["row_version"]}"',
    )


def _post(
    context: dict[str, object], setup: dict[str, object], payment: dict, key: str | None = None
) -> object:
    return setup["client"].post(
        f"{_root(context)}/payments/{payment['id']}/post",
        {"fiscal_period_id": str(context["period"]), "journal_id": str(context["journal"])},
        format="json",
        HTTP_IF_MATCH=f'"{payment["row_version"]}"',
        HTTP_IDEMPOTENCY_KEY=key or f"post-{uuid.uuid4()}",
    )


@pytest.mark.parametrize("direction", ["receipt", "disbursement"])
def test_pay_001_002_post_and_idempotency(accounting_context: dict, direction: str) -> None:
    setup = _setup(accounting_context, direction)
    payment = _create(accounting_context, setup)
    allocated = _allocate(accounting_context, setup, payment)
    assert allocated.status_code == 200, allocated.json()
    payment = allocated.json()
    key = f"posting-{uuid.uuid4()}"
    posted = _post(accounting_context, setup, payment, key)
    assert posted.status_code == 200, posted.json()
    replay = _post(accounting_context, setup, payment, key)
    assert replay.json() == posted.json()
    assert posted.json()["status"] == "posted"
    with connection.cursor() as c:
        c.execute(
            "SELECT sum(debit_amount),sum(credit_amount) FROM erp.journal_lines "
            "WHERE journal_entry_id=%s",
            [posted.json()["journal_entry_id"]],
        )
        assert c.fetchone() == (60, 60)
        c.execute(
            "SELECT count(*) FROM erp.outbox_events WHERE aggregate_id=%s "
            "AND event_type='payment.posted'",
            [payment["id"]],
        )
        assert c.fetchone() == (1,)
        c.execute(
            "SELECT count(*) FROM erp.company_audit_events WHERE object_id=%s "
            "AND action='payment.posted'",
            [payment["id"]],
        )
        assert c.fetchone() == (1,)
    mismatch = setup["client"].post(
        f"{_root(accounting_context)}/payments/{payment['id']}/post",
        {"fiscal_period_id": str(uuid.uuid4()), "journal_id": str(accounting_context["journal"])},
        format="json",
        HTTP_IF_MATCH=f'"{payment["row_version"]}"',
        HTTP_IDEMPOTENCY_KEY=key,
    )
    assert mismatch.status_code == 409
    assert mismatch.json()["error"]["code"] == "IDEMPOTENCY_PAYLOAD_MISMATCH"


def test_pay_004_allocation_validation_and_revisions(accounting_context: dict) -> None:
    setup = _setup(accounting_context)
    payment = _create(accounting_context, setup)
    invalid = _allocate(accounting_context, setup, payment, "61")
    assert invalid.status_code == 409
    assert invalid.json()["error"]["code"] == "ALLOCATION_EXCEEDS_PAYMENT"
    invalid = _allocate(accounting_context, setup, payment, document_id=str(uuid.uuid4()))
    assert invalid.status_code == 409
    assert (
        setup["client"]
        .get(f"{_root(accounting_context)}/payments/{payment['id']}/allocations")
        .json()["results"]
        == []
    )
    valid = _allocate(accounting_context, setup, payment)
    assert valid.status_code == 200, valid.json()
    stale = _allocate(accounting_context, setup, payment)
    assert stale.status_code == 412


def test_payment_rollback_after_journal_failure(accounting_context: dict) -> None:
    setup = _setup(accounting_context)
    payment = _create(accounting_context, setup)
    scope = CompanyScope(
        tenant_id=accounting_context["tenant"],
        company_id=accounting_context["company"],
        user_id=accounting_context["user"],
    )
    with patch("apps.payments.services._line", side_effect=DatabaseError("forced journal failure")):
        with pytest.raises(APIError):
            post_payment(
                scope,
                uuid.UUID(payment["id"]),
                fiscal_period_id=accounting_context["period"],
                journal_id=accounting_context["journal"],
                expected_revision=payment["row_version"],
                idempotency_key="rollback",
            )
    with connection.cursor() as c:
        c.execute("SELECT status,journal_entry_id FROM erp.payments WHERE id=%s", [payment["id"]])
        assert c.fetchone() == ("draft", None)
        c.execute("SELECT count(*) FROM erp.journal_entries WHERE source_id=%s", [payment["id"]])
        assert c.fetchone() == (0,)
        c.execute(
            "SELECT count(*) FROM erp.api_command_receipts WHERE resource_key=%s "
            "AND operation='payment.post'",
            [payment["id"]],
        )
        assert c.fetchone() == (0,)


@pytest.mark.concurrency
@pytest.mark.parametrize("direction", ["receipt", "disbursement"])
def test_pay_003_two_payments_cannot_overallocate(accounting_context: dict, direction: str) -> None:
    setup = _setup(accounting_context, direction)
    payments = []
    for _ in range(2):
        p = _create(accounting_context, setup, "70")
        allocated = _allocate(accounting_context, setup, p, "70")
        assert allocated.status_code == 200, allocated.json()
        payments.append(allocated.json())

    def worker(p: dict) -> str:
        close_old_connections()
        try:
            post_payment(
                CompanyScope(
                    tenant_id=accounting_context["tenant"],
                    company_id=accounting_context["company"],
                    user_id=accounting_context["user"],
                ),
                uuid.UUID(p["id"]),
                fiscal_period_id=accounting_context["period"],
                journal_id=accounting_context["journal"],
                expected_revision=p["row_version"],
                idempotency_key=f"race-{p['id']}",
            )
            return "posted"
        except APIError as exc:
            return exc.default_code
        finally:
            connection.close()

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(worker, payments))
    assert sorted(results) == ["ALLOCATION_EXCEEDS_REMAINING", "posted"]


@pytest.mark.parametrize("direction", ["receipt", "disbursement"])
def test_pay_005_006_reversal_and_historical_aging(
    accounting_context: dict, direction: str
) -> None:
    setup = _setup(accounting_context, direction)
    payment = _create(accounting_context, setup)
    allocated = _allocate(accounting_context, setup, payment)
    assert allocated.status_code == 200, allocated.json()
    posted = _post(accounting_context, setup, allocated.json())
    assert posted.status_code == 200, posted.json()
    payment = posted.json()
    root = _root(accounting_context)
    report = "receivable-aging" if direction == "receipt" else "payable-aging"
    before = setup["client"].get(f"{root}/reports/{report}?cutoff={setup['cutoff']}")
    assert before.status_code == 200, before.json()
    gross = Decimal(setup["source"]["gross_total"])
    assert Decimal(before.json()["results"][0]["open_amount"]) == gross - 60
    reverse_payload = {
        "reversal_date": setup["reversal_date"],
        "reason": "Bank rejected transfer",
        "fiscal_period_id": str(accounting_context["period"]),
        "journal_id": str(accounting_context["journal"]),
    }
    headers = {
        "HTTP_IF_MATCH": f'"{payment["row_version"]}"',
        "HTTP_IDEMPOTENCY_KEY": "reverse-test",
    }
    reversal = setup["client"].post(
        f"{root}/payments/{payment['id']}/reverse", reverse_payload, format="json", **headers
    )
    assert reversal.status_code == 200, reversal.json()
    replay = setup["client"].post(
        f"{root}/payments/{payment['id']}/reverse", reverse_payload, format="json", **headers
    )
    assert reversal.json() == replay.json()
    historical = setup["client"].get(f"{root}/reports/{report}?cutoff={setup['cutoff']}")
    assert historical.json() == before.json()
    after = setup["client"].get(f"{root}/reports/{report}?cutoff={setup['reversal_date']}")
    assert Decimal(after.json()["results"][0]["open_amount"]) == gross
    with connection.cursor() as c:
        c.execute(
            "SELECT count(*) FROM erp.payment_allocation_reversals WHERE reversal_payment_id=%s",
            [reversal.json()["id"]],
        )
        assert c.fetchone() == (1,)
        c.execute(
            "SELECT sum(debit_amount),sum(credit_amount) FROM erp.journal_lines "
            "WHERE journal_entry_id IN (%s,%s)",
            [payment["journal_entry_id"], reversal.json()["journal_entry_id"]],
        )
        assert c.fetchone() == (120, 120)
    # Both public commands and direct SQL protect posted settlement facts.
    assert (
        setup["client"]
        .patch(
            f"{root}/payments/{payment['id']}",
            {"amount": "1"},
            format="json",
            HTTP_IF_MATCH=f'"{payment["row_version"]}"',
        )
        .status_code
        == 409
    )
    table = "ar_receipt_allocations" if direction == "receipt" else "ap_disbursement_allocations"
    with pytest.raises(DatabaseError), transaction.atomic():
        with connection.cursor() as c:
            c.execute(f"DELETE FROM erp.{table} WHERE payment_id=%s", [payment["id"]])


@pytest.mark.parametrize("direction", ["receipt", "disbursement"])
def test_pay_001_withholding_snapshots_reconcile(accounting_context: dict, direction: str) -> None:
    setup = _setup(accounting_context, direction)
    gross = Decimal(setup["source"]["gross_total"])
    payment = _create(accounting_context, setup, str(gross - 10))
    # Use a synthetic payment-stage withholding fixture, separate from invoice tax facts.
    tax_account = uuid.uuid4()
    ids = setup["ids"]
    with connection.cursor() as c:
        c.execute(
            "INSERT INTO erp.accounts(id,company_id,code,name,account_type,normal_balance) "
            "VALUES (%s,%s,%s,'Withholding',%s,%s)",
            [
                tax_account,
                accounting_context["company"],
                f"WHT-{tax_account.hex[:8]}",
                "asset" if direction == "receipt" else "liability",
                "debit" if direction == "receipt" else "credit",
            ],
        )
        # Create an independent component so posted invoice tax facts retain their original catalog.
        tax_type, schedule, version, code, component = [uuid.uuid4() for _ in range(5)]
        c.execute(
            "INSERT INTO erp.tax_types(id,jurisdiction_id,code,name,tax_family,"
            "calculation_stage,tax_direction) VALUES (%s,%s,%s,'Withholding',"
            "'withholding','payment','withheld')",
            [tax_type, ids["jurisdiction"], f"WHT-{tax_type}"],
        )
        c.execute(
            "INSERT INTO erp.tax_rate_schedules(id,jurisdiction_id,tax_type_id,"
            "schedule_code,name) VALUES (%s,%s,%s,%s,'Ten percent')",
            [schedule, ids["jurisdiction"], tax_type, f"WHT-{schedule}"],
        )
        c.execute(
            "INSERT INTO erp.tax_rate_versions(id,jurisdiction_id,rate_schedule_id,"
            "version_code,valid_from,calculation_method,rate) "
            "VALUES (%s,%s,%s,'fixture-1','2026-01-01','percentage',10)",
            [version, ids["jurisdiction"], schedule],
        )
        c.execute(
            "INSERT INTO erp.tax_codes(id,company_id,tax_jurisdiction_id,code,name,tax_scope) "
            "VALUES (%s,%s,%s,%s,'Withholding','withholding')",
            [code, accounting_context["company"], ids["jurisdiction"], f"WHT-{code}"],
        )
        c.execute(
            "INSERT INTO erp.tax_code_components(id,company_id,tax_jurisdiction_id,"
            "tax_code_id,rate_schedule_id,sequence_no,withholding_account_id) "
            "VALUES (%s,%s,%s,%s,%s,1,%s)",
            [
                component,
                accounting_context["company"],
                ids["jurisdiction"],
                code,
                schedule,
                tax_account,
            ],
        )
    withheld = setup["client"].put(
        f"{_root(accounting_context)}/payments/{payment['id']}/withholding",
        {
            "components": [
                {
                    "tax_code_id": str(code),
                    "taxable_base_amount": "100",
                    "tax_treatment": "adjustable",
                }
            ]
        },
        format="json",
        HTTP_IF_MATCH=f'"{payment["row_version"]}"',
    )
    assert withheld.status_code == 200, withheld.json()
    assert Decimal(withheld.json()["withholding_total"]) == 10
    component = withheld.json()["tax_components"][0]
    assert component["withholding_account_id_snapshot"] == str(tax_account)
    assert component["rate_version_code_snapshot"] == "fixture-1"
    allocated = _allocate(accounting_context, setup, withheld.json(), str(gross))
    assert allocated.status_code == 200, allocated.json()
    denied = _post(accounting_context, setup, allocated.json())
    assert denied.status_code == 409, denied.json()
    assert denied.json()["error"]["code"] == "TAX_REGISTRATION_REQUIRED"
    with connection.cursor() as c:
        c.execute(
            "INSERT INTO erp.company_tax_registrations(company_id,jurisdiction_id,tax_type_id,"
            "registration_number,valid_from) VALUES (%s,%s,%s,'WHT-COMPANY','2026-01-01')",
            [accounting_context["company"], ids["jurisdiction"], tax_type],
        )
        c.execute(
            "INSERT INTO erp.partner_tax_registrations(company_id,partner_id,jurisdiction_id,"
            "tax_type_id,registration_number,valid_from,is_verified) "
            "VALUES (%s,%s,%s,%s,'WHT-PARTNER','2026-01-01',true)",
            [accounting_context["company"], ids["partner"], ids["jurisdiction"], tax_type],
        )
    posted = _post(accounting_context, setup, allocated.json())
    assert posted.status_code == 200, posted.json()
    with connection.cursor() as c:
        c.execute(
            "SELECT sum(debit_amount),sum(credit_amount) FROM erp.journal_lines "
            "WHERE journal_entry_id=%s",
            [posted.json()["journal_entry_id"]],
        )
        assert c.fetchone() == (gross, gross)
    with pytest.raises(DatabaseError), transaction.atomic():
        with connection.cursor() as c:
            c.execute(
                "UPDATE erp.payment_tax_components SET tax_amount=0 WHERE payment_id=%s",
                [payment["id"]],
            )
    with connection.cursor() as c:
        c.execute("UPDATE erp.tax_rate_versions SET valid_to='2026-09-28' WHERE id=%s", [version])
    reversal = setup["client"].post(
        f"{_root(accounting_context)}/payments/{payment['id']}/reverse",
        {
            "reversal_date": setup["reversal_date"],
            "reason": "Reverse withholding settlement",
            "fiscal_period_id": str(accounting_context["period"]),
            "journal_id": str(accounting_context["journal"]),
        },
        format="json",
        HTTP_IF_MATCH=f'"{posted.json()["row_version"]}"',
        HTTP_IDEMPOTENCY_KEY="withheld-reverse",
    )
    assert reversal.status_code == 200, reversal.json()
    assert Decimal(reversal.json()["withholding_total"]) == 10
    assert (
        reversal.json()["tax_components"][0]["polarity_snapshot"] != component["polarity_snapshot"]
    )
    with connection.cursor() as c:
        c.execute(
            "SELECT sum(tax_amount) FROM erp.v_tax_transaction_components_v2 "
            "WHERE source_document_id IN (%s,%s)",
            [payment["id"], reversal.json()["id"]],
        )
        assert c.fetchone() == (0,)


def test_payment_denials_leave_no_posting_effects(accounting_context: dict) -> None:
    setup = _setup(accounting_context)
    payment = _create(accounting_context, setup)
    _change_module(setup["client"], accounting_context, "payments", "read_only")
    denied = _post(accounting_context, setup, payment)
    assert denied.status_code == 403, denied.json()
    _change_module(setup["client"], accounting_context, "payments", "enabled")
    with connection.cursor() as c:
        c.execute(
            "UPDATE identity.tenant_memberships SET tenant_role='member' "
            "WHERE tenant_id=%s AND user_id=%s",
            [accounting_context["tenant"], accounting_context["user"]],
        )
    denied = _post(accounting_context, setup, payment)
    assert denied.status_code == 403, denied.json()
    with connection.cursor() as c:
        c.execute("SELECT status,journal_entry_id FROM erp.payments WHERE id=%s", [payment["id"]])
        assert c.fetchone() == ("draft", None)
        c.execute("SELECT count(*) FROM erp.journal_entries WHERE source_id=%s", [payment["id"]])
        assert c.fetchone() == (0,)
        c.execute(
            "SELECT count(*) FROM erp.api_command_receipts WHERE resource_key=%s "
            "AND operation='payment.post'",
            [payment["id"]],
        )
        assert c.fetchone() == (0,)


def test_payment_scope_precision_draft_patch_and_closed_period(accounting_context: dict) -> None:
    setup = _setup(accounting_context)
    payment = _create(accounting_context, setup)
    root = _root(accounting_context)
    response = setup["client"].patch(
        f"{root}/payments/{payment['id']}",
        {"external_reference": "BANK-123"},
        format="json",
        HTTP_IF_MATCH=f'"{payment["row_version"]}"',
    )
    assert response.status_code == 200, response.json()
    payment = response.json()
    assert payment["external_reference"] == "BANK-123"
    assert setup["client"].get(f"{root}/payments/{uuid.uuid4()}").status_code == 404
    assert setup["client"].get(f"{root}/payments?limit=abc").status_code == 400
    wrong_company = root.replace(str(accounting_context["company"]), str(uuid.uuid4()))
    assert setup["client"].get(f"{wrong_company}/payments/{payment['id']}").status_code == 404
    with connection.cursor() as c:
        c.execute(
            "UPDATE erp.fiscal_periods SET state='closed' WHERE id=%s",
            [accounting_context["period"]],
        )
    denied = _post(accounting_context, setup, payment)
    assert denied.status_code == 409, denied.json()
    assert denied.json()["error"]["code"] == "PERIOD_NOT_OPEN"


@pytest.mark.parametrize("invalid", ["partner", "currency", "conversion", "direction"])
def test_pay_004_incompatible_targets(accounting_context: dict, invalid: str) -> None:
    setup = _setup(accounting_context)
    payment = _create(accounting_context, setup)
    if invalid in {"partner", "direction"}:
        with connection.cursor() as c:
            partner = uuid.uuid4()
            c.execute(
                "INSERT INTO erp.business_partners(id,company_id,partner_code,"
                "display_name,partner_kind) VALUES (%s,%s,%s,'Other partner','both')",
                [partner, accounting_context["company"], f"P-{partner}"],
            )
        change = {"partner_id": str(partner)}
        if invalid == "direction":
            change["direction"] = "disbursement"
    else:
        change = {"currency_code": "EUR", "exchange_rate": "1"}
        with connection.cursor() as c:
            c.execute(
                "INSERT INTO erp.currencies(code,name,minor_units) "
                "VALUES ('EUR','Euro',2) ON CONFLICT DO NOTHING"
            )
        if invalid == "conversion":
            change = {}
    if change:
        changed = setup["client"].patch(
            f"{_root(accounting_context)}/payments/{payment['id']}",
            change,
            format="json",
            HTTP_IF_MATCH=f'"{payment["row_version"]}"',
        )
        assert changed.status_code == 200, changed.json()
        payment = changed.json()
    payload = {
        "allocations": [
            {
                "document_id": setup["source"]["id"],
                "applied_document_amount": "60",
                "applied_payment_amount": "60",
                "allocation_exchange_rate": "2" if invalid == "conversion" else "1",
            }
        ]
    }
    denied = setup["client"].put(
        f"{_root(accounting_context)}/payments/{payment['id']}/allocations",
        payload,
        format="json",
        HTTP_IF_MATCH=f'"{payment["row_version"]}"',
    )
    assert denied.status_code == 409, denied.json()
    assert (
        setup["client"]
        .get(f"{_root(accounting_context)}/payments/{payment['id']}/allocations")
        .json()["results"]
        == []
    )


def test_database_guard_rejects_overallocation_even_without_service_check(
    accounting_context: dict,
) -> None:
    setup = _setup(accounting_context)
    payments = []
    for _ in range(2):
        payment = _create(accounting_context, setup, "70")
        allocation = _allocate(accounting_context, setup, payment, "70")
        assert allocation.status_code == 200, allocation.json()
        payments.append(allocation.json())
    posted = _post(accounting_context, setup, payments[0])
    assert posted.status_code == 200, posted.json()
    with patch("apps.payments.services._validate_allocations"):
        denied = _post(accounting_context, setup, payments[1])
    assert denied.status_code == 409, denied.json()
    assert denied.json()["error"]["code"] == "PAYMENT_POSTING_CONFLICT"
    with connection.cursor() as c:
        c.execute(
            "SELECT status,journal_entry_id FROM erp.payments WHERE id=%s", [payments[1]["id"]]
        )
        assert c.fetchone() == ("draft", None)
        c.execute(
            "SELECT count(*) FROM erp.journal_entries WHERE source_id=%s", [payments[1]["id"]]
        )
        assert c.fetchone() == (0,)


@pytest.mark.security
def test_payment_rls_with_non_owner_runtime_role(accounting_context: dict) -> None:
    setup = _setup(accounting_context)
    payment = _create(accounting_context, setup)
    allocated = _allocate(accounting_context, setup, payment)
    assert allocated.status_code == 200, allocated.json()
    posted = _post(accounting_context, setup, allocated.json())
    assert posted.status_code == 200, posted.json()
    reversal = setup["client"].post(
        f"{_root(accounting_context)}/payments/{payment['id']}/reverse",
        {
            "reversal_date": setup["reversal_date"],
            "reason": "RLS fixture",
            "fiscal_period_id": str(accounting_context["period"]),
            "journal_id": str(accounting_context["journal"]),
        },
        format="json",
        HTTP_IF_MATCH=f'"{posted.json()["row_version"]}"',
        HTTP_IDEMPOTENCY_KEY="rls-reversal",
    )
    assert reversal.status_code == 200, reversal.json()
    # Temporary read grants and SET LOCAL ROLE are both rolled back at the end.
    with transaction.atomic():
        with connection.cursor() as c:
            c.execute(
                "SELECT rolbypassrls,rolsuper FROM pg_roles WHERE rolname='quickaccounts_runtime'"
            )
            assert c.fetchone() == (False, False)
            c.execute("GRANT USAGE ON SCHEMA erp TO quickaccounts_runtime")
            c.execute(
                "GRANT SELECT ON erp.companies,erp.payments,erp.payment_allocation_reversals "
                "TO quickaccounts_runtime"
            )
            c.execute("SET LOCAL ROLE quickaccounts_runtime")
            c.execute(
                "SELECT set_config('app.tenant_id',%s,true)", [str(accounting_context["tenant"])]
            )
            c.execute("SELECT count(*) FROM erp.payments WHERE id=%s", [payment["id"]])
            assert c.fetchone() == (1,)
            c.execute(
                "SELECT count(*) FROM erp.payment_allocation_reversals "
                "WHERE reversal_payment_id=%s",
                [reversal.json()["id"]],
            )
            assert c.fetchone() == (1,)
            c.execute("SELECT set_config('app.tenant_id',%s,true)", [str(uuid.uuid4())])
            c.execute("SELECT count(*) FROM erp.payments WHERE id=%s", [payment["id"]])
            assert c.fetchone() == (0,)
            c.execute("SELECT count(*) FROM erp.payment_allocation_reversals")
            assert c.fetchone() == (0,)
        transaction.set_rollback(True)


@pytest.mark.concurrency
def test_concurrent_keys_post_exactly_one_payment_journal(accounting_context: dict) -> None:
    setup = _setup(accounting_context)
    payment = _create(accounting_context, setup)

    def worker(key: str) -> str:
        close_old_connections()
        try:
            post_payment(
                CompanyScope(
                    tenant_id=accounting_context["tenant"],
                    company_id=accounting_context["company"],
                    user_id=accounting_context["user"],
                ),
                uuid.UUID(payment["id"]),
                fiscal_period_id=accounting_context["period"],
                journal_id=accounting_context["journal"],
                expected_revision=payment["row_version"],
                idempotency_key=key,
            )
            return "posted"
        except APIError as exc:
            return exc.default_code
        finally:
            connection.close()

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(worker, ["same-payment-1", "same-payment-2"]))
    assert sorted(results) == ["PAYMENT_NOT_DRAFT", "posted"]
    with connection.cursor() as c:
        c.execute("SELECT count(*) FROM erp.journal_entries WHERE source_id=%s", [payment["id"]])
        assert c.fetchone() == (1,)


@pytest.mark.parametrize("direction", ["receipt", "disbursement"])
def test_open_items_preserve_unallocated_overpayment_credit(
    accounting_context: dict,
    direction: str,
) -> None:
    setup = _setup(accounting_context, direction)
    gross = Decimal(setup["source"]["gross_total"])
    payment = _create(accounting_context, setup, "150")
    allocated = _allocate(accounting_context, setup, payment, str(gross))
    assert allocated.status_code == 200, allocated.json()
    posted = _post(accounting_context, setup, allocated.json())
    assert posted.status_code == 200, posted.json()
    assert Decimal(posted.json()["unallocated_total"]) == 150 - gross
    report = "open-receivables" if direction == "receipt" else "open-payables"
    rows = (
        setup["client"]
        .get(f"{_root(accounting_context)}/reports/{report}?cutoff={setup['cutoff']}")
        .json()["results"]
    )
    assert len(rows) == 1
    assert rows[0]["item_kind"] == "unallocated_payment"
    assert Decimal(rows[0]["open_amount"]) == gross - 150


def test_payment_creation_receipt_replays_original_and_rejects_payload_changes(
    accounting_context: dict,
) -> None:
    setup = _setup(accounting_context)
    payload = {
        "payment_no": "PAY-IDEMPOTENT",
        "direction": "receipt",
        "partner_id": str(setup["ids"]["partner"]),
        "cash_account_id": str(accounting_context["cash"]),
        "payment_date": "2026-09-27",
        "currency_code": "USD",
        "amount": "60",
        "payment_method": "bank",
    }
    url = f"{_root(accounting_context)}/payments"
    headers = {"HTTP_IDEMPOTENCY_KEY": "create-replay"}
    first = setup["client"].post(url, payload, format="json", **headers)
    replay = setup["client"].post(url, payload, format="json", **headers)
    assert first.status_code == replay.status_code == 201
    assert first.json() == replay.json()
    changed = setup["client"].post(url, payload | {"amount": "61"}, format="json", **headers)
    assert changed.status_code == 409
    assert changed.json()["error"]["code"] == "IDEMPOTENCY_PAYLOAD_MISMATCH"
    with connection.cursor() as c:
        c.execute(
            "SELECT response_status FROM erp.api_command_receipts WHERE "
            "company_id=%s AND operation='payment.create' AND idempotency_key='create-replay'",
            [accounting_context["company"]],
        )
        assert c.fetchone() == (201,)
