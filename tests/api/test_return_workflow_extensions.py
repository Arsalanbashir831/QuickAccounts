import concurrent.futures
import datetime as dt
from decimal import Decimal
from importlib import import_module

import pytest
from django.contrib.auth import get_user_model
from django.db import DatabaseError, close_old_connections, connection, transaction

from apps.sales.return_services import apply_return_credit
from common.access.scopes import CompanyScope
from common.api.errors import APIError
from tests.api.test_payments_api import _setup
from tests.api.test_purchase_bill_drafts_api import _root
from tests.api.test_sales_invoice_posting_api import _create_calculated_invoice
from tests.api.test_sales_returns_api import (
    _command,
    _draft,
    _paid,
    _posting,
    _quality_approved,
    _stock_setup,
)

pytestmark = [pytest.mark.api, pytest.mark.p0, pytest.mark.django_db(transaction=True)]


def test_repair_migration_reverse_forward(accounting_context):
    migration = import_module("apps.database.migrations.0034_return_repairs_and_credit_targets")
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(migration.REVERSE)
        cursor.execute("SELECT to_regclass('erp.return_repair_jobs')")
        assert cursor.fetchone() == (None,)
        cursor.execute(migration.SQL)
        cursor.execute("SELECT to_regclass('erp.return_repair_jobs')")
        assert cursor.fetchone()[0] is not None
        transaction.set_rollback(True)


def test_repair_qc_commands_work_with_deployed_runtime_role(accounting_context):
    from tests.api.test_phase5_cross_domain_release import _runtime_grants

    setup, document, warehouse, _ = _segregated(accounting_context)
    _runtime_grants()
    setup["client"].force_authenticate(get_user_model().objects.get(pk=accounting_context["user"]))
    try:
        with connection.cursor() as cursor:
            cursor.execute("SET ROLE quickaccounts_runtime")
        job = _quality_approved(accounting_context, setup, document, warehouse["id"])
        assert job["status"] == "qc_passed"
        assert job["qc_by"] == str(accounting_context["user"])
    finally:
        with connection.cursor() as cursor:
            cursor.execute("RESET ROLE")


def _segregated(context):
    setup = _stock_setup(context)
    document = _draft(context, setup, physical=True)
    warehouse = (
        setup["client"]
        .post(
            f"{_root(context)}/inventory/warehouses",
            {"code": "REPAIR", "name": "Repair warehouse", "stock_category": "damaged"},
            format="json",
        )
        .json()
    )
    url = f"{_root(context)}/sales/returns/{document['id']}"
    response = _command(
        setup["client"],
        f"{url}/inspect",
        {
            "lines": [
                {
                    "line_id": document["lines"][0]["id"],
                    "received_quantity": "1",
                    "condition": "damaged",
                    "disposition": "damaged",
                    "warehouse_id": warehouse["id"],
                }
            ]
        },
        document["row_version"],
    )
    assert response.status_code == 200, response.json()
    response = _command(
        setup["client"], f"{url}/post", _posting(context), response.json()["row_version"]
    )
    assert response.status_code == 200, response.json()
    return setup, response.json(), warehouse, url


def test_repair_qc_release_is_required_and_quantity_bounded(accounting_context):
    setup, document, warehouse, url = _segregated(accounting_context)
    body = _posting(accounting_context) | {
        "line_id": document["lines"][0]["id"],
        "from_warehouse_id": warehouse["id"],
        "to_warehouse_id": str(setup["warehouse"]),
        "quantity": "1",
        "action_date": str(dt.datetime.now(dt.UTC).date()),
        "reason": "Restock after QC",
    }
    response = _command(setup["client"], f"{url}/stock-dispositions", body, document["row_version"])
    assert response.status_code == 409, response.json()
    job = _quality_approved(accounting_context, setup, document, warehouse["id"])
    body["repair_job_id"] = job["id"]
    response = _command(
        setup["client"], f"{url}/stock-dispositions", body, document["row_version"], "qc-release"
    )
    assert response.status_code == 200, response.json()
    assert Decimal(response.json()["historical_cost"]) == 100
    assert response.json()["journal_entry_id"] is None
    assert (
        _command(
            setup["client"],
            f"{url}/stock-dispositions",
            body,
            document["row_version"],
            "qc-release",
        ).json()
        == response.json()
    )
    assert (
        _command(
            setup["client"], f"{url}/stock-dispositions", body, document["row_version"]
        ).status_code
        == 409
    )
    detail = (
        setup["client"].get(f"{_root(accounting_context)}/inventory/repairs/{job['id']}").json()
    )
    assert Decimal(detail["released_quantity"]) == 1
    with pytest.raises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "UPDATE erp.return_repair_jobs SET status='in_progress' WHERE id=%s", [job["id"]]
        )


def test_damaged_repair_cannot_skip_completion_or_use_stale_revision(accounting_context):
    setup, document, warehouse, _ = _segregated(accounting_context)
    root = f"{_root(accounting_context)}/inventory/repairs"
    response = _command(
        setup["client"],
        root,
        {
            "line_id": document["lines"][0]["id"],
            "warehouse_id": warehouse["id"],
            "quantity": "1",
            "diagnosis": "Broken connector",
        },
        document["row_version"],
    )
    assert response.status_code == 201, response.json()
    job = response.json()
    assert (
        _command(
            setup["client"],
            f"{root}/{job['id']}",
            {"status": "qc_passed", "qc_notes": "Skipped repair"},
            1,
        ).status_code
        == 409
    )
    response = _command(setup["client"], f"{root}/{job['id']}", {"status": "waiting_parts"}, 1)
    assert response.status_code == 200, response.json()
    assert (
        _command(setup["client"], f"{root}/{job['id']}", {"status": "in_progress"}, 1).status_code
        == 412
    )


def test_return_credit_applies_to_future_invoice_with_replay(accounting_context):
    setup = _setup(accounting_context)
    _paid(accounting_context, setup)
    document = _draft(accounting_context, setup, amount="22")
    root = _root(accounting_context)
    url = f"{root}/sales/returns/{document['id']}"
    response = _command(setup["client"], f"{url}/post", _posting(accounting_context), 1)
    assert response.status_code == 200, response.json()
    document = response.json()
    target = _create_calculated_invoice(
        setup["client"], accounting_context, setup["ids"], invoice_no="FUTURE-CREDIT"
    )
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT journal_id FROM erp.journal_entries WHERE id=%s",
            [setup["source"]["journal_entry_id"]],
        )
        journal = str(cursor.fetchone()[0])
    response = _command(
        setup["client"],
        f"{root}/sales/invoices/{target['id']}/post",
        _posting(accounting_context) | {"journal_id": journal},
        target["row_version"],
    )
    assert response.status_code == 200, response.json()
    body = {"target_invoice_id": target["id"], "effective_date": "2026-09-28"}
    applied = _command(
        setup["client"], f"{url}/apply-credit", body, document["row_version"], "future-credit"
    )
    assert applied.status_code == 200, applied.json()
    assert Decimal(applied.json()["remaining_credit"]) == 0
    assert applied.json()["credit_applications"][0]["sales_invoice_id"] == target["id"]
    assert (
        _command(
            setup["client"], f"{url}/apply-credit", body, document["row_version"], "future-credit"
        ).json()
        == applied.json()
    )


def test_future_credit_concurrency_cannot_double_spend(accounting_context):
    setup = _setup(accounting_context)
    _paid(accounting_context, setup)
    document = _draft(accounting_context, setup, amount="22")
    url = f"{_root(accounting_context)}/sales/returns/{document['id']}"
    response = _command(setup["client"], f"{url}/post", _posting(accounting_context), 1)
    assert response.status_code == 200, response.json()
    document = response.json()
    scope = CompanyScope(
        tenant_id=accounting_context["tenant"],
        company_id=accounting_context["company"],
        user_id=accounting_context["user"],
    )
    # Two independent commands compete for one retained customer credit.
    target = _create_calculated_invoice(
        setup["client"], accounting_context, setup["ids"], "RACE-TARGET"
    )
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT journal_id FROM erp.journal_entries WHERE id=%s",
            [setup["source"]["journal_entry_id"]],
        )
        journal = str(cursor.fetchone()[0])
    response = _command(
        setup["client"],
        f"{_root(accounting_context)}/sales/invoices/{target['id']}/post",
        _posting(accounting_context) | {"journal_id": journal},
        target["row_version"],
    )
    assert response.status_code == 200, response.json()

    def worker(index):
        close_old_connections()
        try:
            apply_return_credit(
                scope,
                document["id"],
                revision=document["row_version"],
                target_invoice_id=target["id"],
                key=f"credit-race-{index}",
                request_id=None,
            )
            return "posted"
        except APIError as exc:
            return exc.default_code
        finally:
            close_old_connections()

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(worker, range(2)))
    assert sorted(results) == ["RETURN_CREDIT_NOT_APPLICABLE", "posted"]
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT sum(amount) FROM erp.sales_return_credit_applications WHERE "
            "company_id=%s AND sales_return_id=%s",
            [accounting_context["company"], document["id"]],
        )
        assert cursor.fetchone() == (Decimal(22),)
    assert (
        _command(
            setup["client"],
            f"{url}/apply-credit",
            {"target_invoice_id": target["id"]},
            document["row_version"],
        ).status_code
        == 409
    )
