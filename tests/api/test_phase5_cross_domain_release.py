"""P5.12 release gates: real cross-domain flows and deployed runtime privileges."""

import importlib
import uuid
from decimal import Decimal as D
from pathlib import Path

import pytest
from django.contrib.auth import get_user_model
from django.db import DatabaseError, connection, transaction

from apps.accounting.services.accounting import _audit_and_outbox
from apps.inventory.cost_checkpoints import create_cost_checkpoint
from common.db.context import set_local_context
from tests.api.test_accounting_api import _balanced_entry_payload, _entry_url
from tests.api.test_manufacturing_drafts_api import _active, _order, _scope, _setup
from tests.api.test_purchase_bill_drafts_api import _bind_license, _change_module, _root
from tests.api.test_sales_returns_api import _command

pytestmark = [pytest.mark.api, pytest.mark.p0, pytest.mark.django_db(transaction=True)]


def test_runtime_guard_migration_round_trip(accounting_context):
    migration = importlib.import_module("apps.database.migrations.0033_runtime_release_guards")
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(migration.REVERSE)
        cursor.execute(
            "SELECT prosecdef FROM pg_proc WHERE oid="
            "'erp.assert_company_write(uuid,uuid,text,text)'::regprocedure"
        )
        assert cursor.fetchone() == (False,)
        cursor.execute(migration.SQL)
        cursor.execute(
            "SELECT prosecdef,proconfig FROM pg_proc WHERE oid="
            "'erp.assert_company_write(uuid,uuid,text,text)'::regprocedure"
        )
        definer, settings = cursor.fetchone()
        assert definer is True
        assert settings == ["search_path=pg_catalog, erp, identity, licensing, pg_temp"]
        transaction.set_rollback(True)


def _runtime_grants():
    with connection.cursor() as cursor:
        cursor.execute(Path("deploy/vps/grant-runtime.sql").read_text())
        cursor.execute(
            "SELECT rolsuper,rolbypassrls FROM pg_roles WHERE rolname='quickaccounts_runtime'"
        )
        assert cursor.fetchone() == (False, False)


def _configure(context):
    setup = _setup(context, seed_stock=False)
    _bind_license(
        context,
        (
            "module.inventory",
            "module.manufacturing",
            "module.sales",
            "module.purchasing",
            "module.payments",
        ),
    )
    for module in ("sales", "purchasing", "payments"):
        _change_module(setup["client"], context, module, "enabled")
    setup["root"] = _root(context)
    setup["accounts"] = {
        name: uuid.uuid4() for name in ("raw", "finished", "wip", "ar", "ap", "revenue", "cogs")
    }
    setup["partner"] = uuid.uuid4()
    setup["journals"] = {kind: uuid.uuid4() for kind in ("sales", "purchase")}
    with connection.cursor() as cursor:
        for kind, journal in setup["journals"].items():
            cursor.execute(
                "INSERT INTO erp.journals(id,company_id,code,name,journal_type) "
                "VALUES (%s,%s,%s,%s,%s)",
                [journal, context["company"], kind, kind, kind],
            )
        for name, kind, normal in [
            ("raw", "asset", "debit"),
            ("finished", "asset", "debit"),
            ("wip", "asset", "debit"),
            ("ar", "receivable", "debit"),
            ("ap", "payable", "credit"),
            ("revenue", "revenue", "credit"),
            ("cogs", "cost_of_sales", "debit"),
        ]:
            cursor.execute(
                "INSERT INTO erp.accounts(id,company_id,code,name,account_type,normal_balance) "
                "VALUES (%s,%s,%s,%s,%s,%s)",
                [setup["accounts"][name], context["company"], name, name, kind, normal],
            )
        cursor.execute(
            "INSERT INTO erp.business_partners"
            "(id,company_id,partner_code,display_name,partner_kind) "
            "VALUES (%s,%s,'CHAIN','Cross-domain partner','both')",
            [setup["partner"], context["company"]],
        )
        for event, role, account in [
            ("sales_invoice", "accounts_receivable", "ar"),
            ("purchase_bill", "accounts_payable", "ap"),
        ]:
            cursor.execute(
                "INSERT INTO erp.accounting_posting_rules"
                "(company_id,event_code,role_code,account_id) VALUES (%s,%s,%s,%s)",
                [context["company"], event, role, setup["accounts"][account]],
            )
        cursor.execute(
            "INSERT INTO erp.item_accounting_profiles"
            "(company_id,item_id,inventory_account_id) VALUES (%s,%s,%s)",
            [context["company"], setup["ids"]["material"], setup["accounts"]["raw"]],
        )
        cursor.execute(
            "INSERT INTO erp.item_accounting_profiles"
            "(company_id,item_id,inventory_account_id,revenue_account_id,cogs_account_id) "
            "VALUES (%s,%s,%s,%s,%s)",
            [
                context["company"],
                setup["ids"]["output"],
                setup["accounts"]["finished"],
                setup["accounts"]["revenue"],
                setup["accounts"]["cogs"],
            ],
        )
        for series, prefix in [
            ("SALES_INVOICE", "SI-"),
            ("SALES_JOURNAL", "SJ-"),
            ("PURCHASE_BILL", "PB-"),
            ("PURCHASE_JOURNAL", "PJ-"),
            ("PAYMENT_JOURNAL", "PAY-"),
        ]:
            cursor.execute(
                "INSERT INTO erp.document_sequences"
                "(company_id,fiscal_period_id,sequence_code,prefix) VALUES (%s,%s,%s,%s)",
                [context["company"], context["period"], series, prefix],
            )
    return setup


def _draft(setup, route, body):
    response = setup["client"].post(
        f"{setup['root']}/{route}", body, format="json", HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4())
    )
    assert response.status_code == 201, response.json()
    document = response.json()
    response = _command(
        setup["client"],
        f"{setup['root']}/{route}/{document['id']}/calculate",
        {},
        document["row_version"],
    )
    assert response.status_code == 200, response.json()
    return response.json()


def _post(setup, route, document, body, key):
    url = f"{setup['root']}/{route}/{document['id']}/post"
    response = _command(setup["client"], url, body, document["row_version"], key)
    assert response.status_code == 200, response.json()
    assert (
        _command(setup["client"], url, body, document["row_version"], key).json() == response.json()
    )
    return response.json()


def _settle(context, setup, document, direction):
    response = setup["client"].post(
        f"{setup['root']}/payments",
        {
            "payment_no": str(uuid.uuid4()),
            "direction": direction,
            "partner_id": str(setup["partner"]),
            "cash_account_id": str(context["cash"]),
            "payment_date": "2026-09-28",
            "currency_code": "USD",
            "amount": document["gross_total"],
            "payment_method": "bank",
        },
        format="json",
        HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
    )
    assert response.status_code == 201, response.json()
    payment = response.json()
    response = setup["client"].put(
        f"{setup['root']}/payments/{payment['id']}/allocations",
        {
            "allocations": [
                {
                    "document_id": document["id"],
                    "applied_document_amount": document["gross_total"],
                    "applied_payment_amount": document["gross_total"],
                    "allocation_exchange_rate": "1",
                }
            ]
        },
        format="json",
        HTTP_IF_MATCH=f'"{payment["row_version"]}"',
    )
    assert response.status_code == 200, response.json()
    return _post(
        setup,
        "payments",
        response.json(),
        {"fiscal_period_id": str(context["period"]), "journal_id": str(context["journal"])},
        f"settle-{direction}",
    )


@pytest.mark.parametrize("role", ["owner", "runtime"])
def test_purchase_production_sale_settlement_reconcile_end_to_end(accounting_context, role):
    setup = _configure(accounting_context)
    client = setup["client"]
    if role == "runtime":
        _runtime_grants()
        # Verify business commands independently of the separate session-storage gate.
        client.force_authenticate(get_user_model().objects.get(pk=accounting_context["user"]))
        with connection.cursor() as cursor:
            cursor.execute("SET ROLE quickaccounts_runtime")
    try:
        posting = {
            "fiscal_period_id": str(accounting_context["period"]),
            "journal_id": str(accounting_context["journal"]),
        }
        bill = _draft(
            setup,
            "purchasing/bills",
            {
                "bill_no": "CHAIN-BILL",
                "supplier_id": str(setup["partner"]),
                "bill_date": "2026-09-27",
                "currency_code": "USD",
                "lines": [
                    {
                        "item_id": str(setup["ids"]["material"]),
                        "description": "Raw materials",
                        "quantity": "100",
                        "unit_cost": "1",
                    }
                ],
            },
        )
        bill = _post(
            setup,
            "purchasing/bills",
            bill,
            posting
            | {
                "warehouse_id": str(setup["ids"]["warehouse"]),
                "journal_id": str(setup["journals"]["purchase"]),
            },
            "chain-bill",
        )
        create_cost_checkpoint(
            _scope(accounting_context),
            {
                "warehouse_id": setup["ids"]["warehouse"],
                "item_id": setup["ids"]["material"],
                "reason": "Reviewed chain stock",
            },
            key="raw-checkpoint",
        )
        manufacturing = setup | {"root": f"{setup['root']}/manufacturing"}
        order = _order(manufacturing, _active(manufacturing))
        order_url = f"{manufacturing['root']}/orders/{order['id']}"
        response = _command(
            client,
            f"{order_url}/release",
            {"wip_account_id": str(setup["accounts"]["wip"])},
            order["row_version"],
        )
        assert response.status_code == 200, response.json()
        order = response.json()
        response = _command(
            client,
            f"{order_url}/material-issues",
            posting
            | {
                "posting_date": "2026-09-28",
                "reservation_ids": [r["id"] for r in order["reservations"]],
            },
            order["row_version"],
        )
        assert response.status_code == 200, response.json()
        order = response.json()
        response = _command(
            client,
            f"{order_url}/outputs",
            posting | {"posting_date": "2026-09-28", "quantity": "10"},
            order["row_version"],
        )
        assert response.status_code == 200, response.json()
        order = response.json()
        assert (
            _command(client, f"{order_url}/complete", {}, order["row_version"]).status_code == 200
        )
        create_cost_checkpoint(
            _scope(accounting_context),
            {
                "warehouse_id": setup["ids"]["warehouse"],
                "item_id": setup["ids"]["output"],
                "reason": "Reviewed output stock",
            },
            key="output-checkpoint",
        )
        invoice = _draft(
            setup,
            "sales/invoices",
            {
                "invoice_no": "CHAIN-INVOICE",
                "partner_id": str(setup["partner"]),
                "issue_date": "2026-09-28",
                "currency_code": "USD",
                "lines": [
                    {
                        "item_id": str(setup["ids"]["output"]),
                        "description": "Finished goods",
                        "quantity": "4",
                        "unit_price": "5",
                    }
                ],
            },
        )
        invoice = _post(
            setup,
            "sales/invoices",
            invoice,
            posting
            | {
                "warehouse_id": str(setup["ids"]["warehouse"]),
                "journal_id": str(setup["journals"]["sales"]),
            },
            "chain-sale",
        )
        _settle(accounting_context, setup, bill, "disbursement")
        _settle(accounting_context, setup, invoice, "receipt")
        for report in ("open-receivables", "open-payables"):
            response = client.get(f"{setup['root']}/reports/{report}")
            assert response.status_code == 200, response.json()
            assert response.json()["results"] == []
        response = client.get(
            f"{setup['root']}/reports/inventory-valuation-reconciliation", {"as_of": "2026-09-28"}
        )
        assert response.status_code == 200, response.json()
        assert response.json()["matches"] is True, response.json()
        assert D(response.json()["ledger_value_company"]) == D("93.4")
        response = client.get(f"{setup['root']}/inventory/cost-reconciliation")
        assert response.status_code == 200 and response.json()["matches"] is True, response.json()
        response = client.get(
            f"{setup['root']}/reports/trial-balance",
            {"date_from": "2026-09-01", "date_to": "2026-09-30"},
        )
        assert response.status_code == 200, response.json()
        balances = {
            row["account_id"]: D(row["closing_net_debit"]) for row in response.json()["results"]
        }
        expected = {
            "raw": "83.5",
            "finished": "9.9",
            "wip": "0",
            "ar": "0",
            "ap": "0",
            "revenue": "-20",
            "cogs": "6.6",
        }
        assert all(
            balances[str(setup["accounts"][name])] == D(value) for name, value in expected.items()
        )
        assert balances[str(accounting_context["cash"])] == -D(80)
        assert sum(balances.values()) == 0
    finally:
        if role == "runtime":
            with connection.cursor() as cursor:
                cursor.execute("RESET ROLE")


def test_deployed_runtime_can_read_django_sessions(accounting_context):
    _runtime_grants()
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT has_table_privilege('quickaccounts_runtime','public.django_session','SELECT'),"
            "has_table_privilege('quickaccounts_runtime','public.django_session','INSERT'),"
            "has_table_privilege('quickaccounts_runtime','public.django_session','UPDATE'),"
            "has_table_privilege('quickaccounts_runtime','public.django_session','DELETE')"
        )
        assert cursor.fetchone() == (True, True, True, True), (
            "Runtime session-backed authentication is not provisioned"
        )


def test_deployed_runtime_can_post_manual_journal_without_direct_audit_grants(accounting_context):
    client = accounting_context["client"]
    response = client.post(
        _entry_url(accounting_context), _balanced_entry_payload(accounting_context), format="json"
    )
    assert response.status_code == 201, response.json()
    entry = response.json()
    _runtime_grants()
    client.force_authenticate(get_user_model().objects.get(pk=accounting_context["user"]))
    try:
        with connection.cursor() as cursor:
            cursor.execute("SET ROLE quickaccounts_runtime")
        response = _command(
            client,
            f"{_entry_url(accounting_context)}/{entry['id']}/post",
            {},
            entry["row_version"],
            "runtime-manual",
        )
        assert response.status_code == 200, response.json()
    finally:
        with connection.cursor() as cursor:
            cursor.execute("RESET ROLE")


def test_runtime_entitlement_write_gate_can_lock_protected_rows(accounting_context):
    _runtime_grants()
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("SET LOCAL ROLE quickaccounts_runtime")
        set_local_context(
            tenant_id=accounting_context["tenant"], user_id=accounting_context["user"]
        )
        cursor.execute(
            "SELECT erp.assert_company_write(%s,%s,'accounting','accounting.entry.post')",
            [accounting_context["company"], accounting_context["product"]],
        )
        assert cursor.fetchone()[0] is not None


def test_runtime_manual_audit_uses_guarded_append_without_direct_table_permission(
    accounting_context,
):
    client = accounting_context["client"]
    response = client.post(
        _entry_url(accounting_context), _balanced_entry_payload(accounting_context), format="json"
    )
    assert response.status_code == 201, response.json()
    entry = response.json()
    response = _command(
        client,
        f"{_entry_url(accounting_context)}/{entry['id']}/post",
        {},
        entry["row_version"],
        "guarded-audit-entry",
    )
    assert response.status_code == 200, response.json()
    _runtime_grants()
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("SET LOCAL ROLE quickaccounts_runtime")
        set_local_context(
            tenant_id=accounting_context["tenant"], user_id=accounting_context["user"]
        )
        _audit_and_outbox(
            _scope(accounting_context),
            action="accounting.entry.posted",
            entry_id=uuid.UUID(entry["id"]),
            request_id="release-audit-probe",
            payload={"verification": True},
            event_key_suffix="release-probe",
        )
        transaction.set_rollback(True)


def test_runtime_session_crud_and_protected_table_privileges(accounting_context):
    _runtime_grants()
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("SET LOCAL ROLE quickaccounts_runtime")
        cursor.execute(
            "SELECT has_table_privilege(current_user,'erp.company_audit_events','INSERT'),"
            "has_table_privilege(current_user,'erp.company_policy_state','UPDATE'),"
            "has_table_privilege(current_user,'licensing.tenant_product_bindings','UPDATE')"
        )
        assert cursor.fetchone() == (False, False, False)
        cursor.execute(
            "INSERT INTO public.django_session(session_key,session_data,expire_date) "
            "VALUES ('release-probe','initial',clock_timestamp()+interval '1 hour')"
        )
        cursor.execute(
            "UPDATE public.django_session SET session_data='updated' "
            "WHERE session_key='release-probe' RETURNING session_data"
        )
        assert cursor.fetchone() == ("updated",)
        cursor.execute(
            "SELECT session_data FROM public.django_session WHERE session_key=%s", ["release-probe"]
        )
        assert cursor.fetchone() == ("updated",)
        cursor.execute("DELETE FROM public.django_session WHERE session_key='release-probe'")
        assert cursor.rowcount == 1
        transaction.set_rollback(True)


@pytest.mark.parametrize("denial", ["wrong_tenant", "wrong_user", "unknown_action", "draft"])
def test_guarded_audit_rejects_invalid_context_or_object(accounting_context, denial):
    client = accounting_context["client"]
    response = client.post(
        _entry_url(accounting_context), _balanced_entry_payload(accounting_context), format="json"
    )
    assert response.status_code == 201, response.json()
    _runtime_grants()
    with pytest.raises(DatabaseError, match="permission denied|permission|posted entry"):
        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute("SET LOCAL ROLE quickaccounts_runtime")
            set_local_context(
                tenant_id=uuid.uuid4()
                if denial == "wrong_tenant"
                else accounting_context["tenant"],
                user_id=uuid.uuid4() if denial == "wrong_user" else accounting_context["user"],
            )
            cursor.execute(
                "SELECT erp.append_accounting_audit(%s,%s,'journal_entry',%s,'{}',NULL)",
                [
                    accounting_context["company"],
                    "invalid.action" if denial == "unknown_action" else "accounting.entry.posted",
                    response.json()["id"],
                ],
            )


@pytest.mark.parametrize("denial", ["wrong_tenant", "wrong_user", "missing_company"])
def test_runtime_write_gate_still_rejects_unauthorized_context(accounting_context, denial):
    _runtime_grants()
    with pytest.raises(DatabaseError, match="accessible|permission denied"):
        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute("SET LOCAL ROLE quickaccounts_runtime")
            set_local_context(
                tenant_id=uuid.uuid4()
                if denial == "wrong_tenant"
                else accounting_context["tenant"],
                user_id=uuid.uuid4() if denial == "wrong_user" else accounting_context["user"],
            )
            cursor.execute(
                "SELECT erp.assert_company_write(%s,%s,'accounting',%s)",
                [
                    uuid.uuid4() if denial == "missing_company" else accounting_context["company"],
                    accounting_context["product"],
                    "accounting.entry.post",
                ],
            )
