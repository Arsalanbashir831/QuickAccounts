import io
import json

import pytest
from django.core.management import call_command

from apps.jobs.services import run_jobs_once
from tests.api.test_accounting_api import _balanced_entry_payload, _entry_url
from tests.api.test_purchase_bill_drafts_api import _bind_license, _change_module

pytestmark = [pytest.mark.api, pytest.mark.p0, pytest.mark.django_db(transaction=True)]


def _base(context: dict) -> str:
    return f"/api/v1/tenants/{context['tenant']}/companies/{context['company']}"


def test_bounded_trial_balance_and_financial_export(accounting_context: dict) -> None:
    client = accounting_context["client"]
    base = _base(accounting_context)
    query = "date_from=2026-09-01&date_to=2026-09-30&limit=1"
    first = client.get(f"{base}/reports/trial-balance?{query}")
    assert first.status_code == 200, first.json()
    assert "app;dur=" in first["Server-Timing"]
    assert len(first.json()["results"]) == 1
    cursor = first.json()["next_cursor"]
    assert cursor
    second = client.get(f"{base}/reports/trial-balance?{query}&cursor={cursor}")
    assert second.status_code == 200, second.json()
    assert second.json()["results"][0]["account_id"] != cursor
    invalid = client.get(f"{base}/reports/trial-balance?{query}&cursor=bad")
    assert invalid.status_code == 400

    report = {"type": "trial_balance", "date_from": "2026-09-01",
              "date_to": "2026-09-30"}
    job = client.post(f"{base}/jobs",
                      {"job_type": "financial_report_export", "job_key": "tb-1",
                       "report": report}, format="json")
    assert job.status_code == 202, job.json()
    replay = client.post(f"{base}/jobs",
                         {"job_type": "financial_report_export", "job_key": "tb-1",
                          "report": report}, format="json")
    assert replay.status_code == 202 and replay.json()["id"] == job.json()["id"]
    assert run_jobs_once(accounting_context["tenant"], accounting_context["user"],
                         "reports") == 1
    artifact = client.get(f"{base}/jobs/{job.json()['id']}/artifact")
    assert artifact.status_code == 200
    assert b"account_id,account_code" in artifact.content


def test_tax_component_report_and_export_filter_validation(accounting_context: dict) -> None:
    client = accounting_context["client"]
    base = _base(accounting_context)
    response = client.get(
        f"{base}/reports/tax-components?date_from=2026-09-01&date_to=2026-09-30"
    )
    assert response.status_code == 200, response.json()
    assert response.json()["results"] == []
    invalid = client.post(
        f"{base}/jobs",
        {"job_type": "financial_report_export", "job_key": "invalid",
         "report": {"type": "general_ledger", "date_from": "2026-09-30",
                    "date_to": "2026-09-01", "account_id": "bad"}},
        format="json",
    )
    assert invalid.status_code == 400


def test_general_ledger_cursor_preserves_running_balance(accounting_context: dict) -> None:
    client = accounting_context["client"]
    for number in (1, 2):
        created = client.post(_entry_url(accounting_context),
                              _balanced_entry_payload(accounting_context), format="json")
        assert created.status_code == 201, created.json()
        posted = client.post(
            f"{_entry_url(accounting_context)}/{created.json()['id']}/post", {},
            format="json", HTTP_IDEMPOTENCY_KEY=f"ledger-page-{number}",
            HTTP_IF_MATCH='"1"',
        )
        assert posted.status_code == 200, posted.json()
    base = _base(accounting_context)
    url = (f"{base}/reports/general-ledger?account_id={accounting_context['cash']}"
           "&date_from=2026-09-01&date_to=2026-09-30&limit=1")
    first = client.get(url)
    assert first.status_code == 200, first.json()
    assert first.json()["results"][0]["running_balance"] == "100.000000"
    cursor = first.json()["next_cursor"]
    assert cursor
    second = client.get(url + "&" + "&".join(f"{key}={value}" for key, value in cursor.items()))
    assert second.status_code == 200, second.json()
    assert second.json()["results"][0]["running_balance"] == "200.000000"
    assert second.json()["next_cursor"] is None


def test_operational_snapshot_is_bounded_and_tenant_scoped(accounting_context: dict) -> None:
    output = io.StringIO()
    call_command("report_operational_metrics", tenant_id=str(accounting_context["tenant"]),
                 actor_user_id=str(accounting_context["user"]), stdout=output)
    metrics = json.loads(output.getvalue())
    assert metrics["database_connections"] <= metrics["max_connections"]
    assert metrics["queued_jobs"] == 0
    assert metrics["replica_lag_seconds"] is None


def test_aging_and_inventory_exports_use_report_queue(accounting_context: dict) -> None:
    client = accounting_context["client"]
    base = _base(accounting_context)
    _bind_license(accounting_context, ("module.inventory", "module.payments"))
    for module in ("inventory", "payments"):
        _change_module(client, accounting_context, module, "enabled")
    for kind in ("receivable_aging", "payable_aging", "inventory_valuation"):
        job = client.post(
            f"{base}/jobs",
            {"job_type": "financial_report_export", "job_key": kind,
             "report": {"type": kind, "date_from": "2026-09-01",
                        "date_to": "2026-09-30"}},
            format="json",
        )
        assert job.status_code == 202, job.json()
    assert run_jobs_once(accounting_context["tenant"], accounting_context["user"],
                         "reports") == 3
    listing = client.get(f"{base}/jobs")
    assert listing.status_code == 200, listing.json()
    for job in listing.json()["results"]:
        artifact = client.get(f"{base}/jobs/{job['id']}/artifact")
        assert artifact.status_code == 200, artifact.json() if artifact.status_code != 200 else ""
