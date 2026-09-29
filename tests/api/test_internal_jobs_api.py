import uuid

import pytest
from django.db import connection, transaction

from apps.jobs.services import relay_internal_events, run_jobs_once
from common.access.scopes import CompanyScope

pytestmark = [pytest.mark.api, pytest.mark.p0, pytest.mark.django_db(transaction=True)]


def _url(context: dict[str, object]) -> str:
    return f"/api/v1/tenants/{context['tenant']}/companies/{context['company']}/jobs"


def test_partner_import_export_and_idempotent_jobs(accounting_context: dict[str, object]) -> None:
    client = accounting_context["client"]
    url = _url(accounting_context)
    rows = [
        {"partner_code": "JOB-001", "display_name": "Imported", "partner_kind": "customer"},
        {"partner_code": "JOB-001", "display_name": "Duplicate", "partner_kind": "customer"},
        {"partner_code": "", "display_name": "Invalid"},
    ]
    result = client.post(url, {"job_type": "partner_import", "job_key": "import-1", "rows": rows}, format="json")
    assert result.status_code == 202, result.json()
    job_id = result.json()["id"]
    replay = client.post(url, {"job_type": "partner_import", "job_key": "import-1", "rows": rows}, format="json")
    assert replay.status_code == 202 and replay.json()["id"] == job_id
    conflict = client.post(url, {"job_type": "partner_import", "job_key": "import-1", "rows": rows[:1]}, format="json")
    assert conflict.status_code == 409
    assert run_jobs_once(accounting_context["tenant"], accounting_context["user"], "imports") == 1
    detail = client.get(f"{url}/{job_id}")
    assert detail.status_code == 200, detail.json()
    assert detail.json()["status"] == "succeeded"
    assert detail.json()["result_metadata"]["created_count"] == 1
    assert [row["status"] for row in detail.json()["result_metadata"]["outcomes"]] == [
        "created", "conflict", "invalid"
    ]
    with connection.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM erp.business_partners WHERE company_id=%s AND partner_code='JOB-001'", [accounting_context["company"]])
        assert cursor.fetchone()[0] == 1
    result = client.post(url, {"job_type": "partner_export", "job_key": "export-1"}, format="json")
    assert result.status_code == 202, result.json()
    export_id = result.json()["id"]
    assert run_jobs_once(accounting_context["tenant"], accounting_context["user"], "reports") == 1
    download = client.get(f"{url}/{export_id}/artifact")
    assert download.status_code == 200
    assert b"JOB-001,Imported,customer" in download.content
    assert run_jobs_once(accounting_context["tenant"], accounting_context["user"], "reports") == 0


def test_cancel_and_scope_protection(accounting_context: dict[str, object]) -> None:
    client = accounting_context["client"]
    url = _url(accounting_context)
    response = client.post(url, {"job_type": "partner_export", "job_key": "cancel-1"}, format="json")
    assert response.status_code == 202, response.json()
    job_id = response.json()["id"]
    cancelled = client.post(f"{url}/{job_id}/cancel", {}, format="json")
    assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"
    assert client.post(f"{url}/{job_id}/cancel", {}, format="json").status_code == 409
    assert client.get(f"{url}/{uuid.uuid4()}").status_code == 404
    assert run_jobs_once(accounting_context["tenant"], accounting_context["user"], "reports") == 0


def test_internal_relay_is_atomic_and_deduplicated(accounting_context: dict[str, object]) -> None:
    scope = CompanyScope(
        accounting_context["tenant"], accounting_context["company"], accounting_context["user"]
    )
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("SELECT set_config('app.tenant_id',%s,true)", [str(scope.tenant_id)])
        cursor.execute(
            "INSERT INTO erp.outbox_events(company_id,event_key,aggregate_type,aggregate_id,"
            "event_type,payload) VALUES (%s,%s,'test',%s,'test.created','{}'::jsonb) RETURNING id",
            [scope.company_id, str(uuid.uuid4()), uuid.uuid4()],
        )
        event_id = cursor.fetchone()[0]
    assert relay_internal_events(scope) == 1
    assert relay_internal_events(scope) == 0
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) FROM erp.internal_event_deliveries WHERE outbox_event_id=%s",
            [event_id],
        )
        assert cursor.fetchone()[0] == 1
        cursor.execute("SELECT delivered_at FROM erp.outbox_events WHERE id=%s", [event_id])
        assert cursor.fetchone()[0] is not None
