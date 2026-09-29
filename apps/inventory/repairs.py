"""Returned-stock repairs and QC; estimates never create accounting effects."""

import uuid
from typing import Any, cast

from django.db import transaction

from apps.payments.services import _claim, _context, _json, _rows, _run
from apps.sales.return_services import _effect, _finish, _locked, _one
from common.access.scopes import CompanyScope, assert_company_write, bind_and_verify_company
from common.api.errors import PreconditionFailed


def repair_detail(company_id: uuid.UUID, job_id: uuid.UUID) -> dict[str, Any]:
    job = _one(
        "SELECT * FROM erp.return_repair_jobs WHERE company_id=%s AND id=%s", [company_id, job_id]
    )
    job["released_quantity"] = _one(
        "SELECT coalesce(sum(quantity),0) quantity FROM erp.sales_return_stock_actions "
        "WHERE company_id=%s AND repair_job_id=%s",
        [company_id, job_id],
    )["quantity"]
    return cast(dict[str, Any], _json(job))


def create_repair(
    scope: CompanyScope, data: dict[str, Any], *, revision: int, key: str, request_id: str | None
) -> dict[str, Any]:
    with transaction.atomic(durable=True):
        bind_and_verify_company(scope)
        receipt, replay = _claim(
            scope,
            "inventory.repair.create",
            str(data["line_id"]),
            key,
            data | {"revision": revision},
        )
        assert_company_write(scope.company_id, "inventory", "inventory.repair.manage")
        if replay is not None:
            return replay
        _context(request_id)
        line = _one(
            "SELECT sales_return_id FROM erp.sales_return_lines WHERE company_id=%s AND id=%s",
            [scope.company_id, data["line_id"]],
        )
        _locked(scope, line["sales_return_id"], revision, posted=True)
        job = _one(
            "INSERT INTO erp.return_repair_jobs(company_id,sales_return_line_id,warehouse_id,"
            "quantity,diagnosis,estimated_cost) VALUES (%s,%s,%s,%s,%s,%s) RETURNING id",
            [
                scope.company_id,
                data["line_id"],
                data["warehouse_id"],
                data["quantity"],
                data["diagnosis"],
                data.get("estimated_cost", 0),
            ],
        )
        result = repair_detail(scope.company_id, job["id"])
        _effect(scope, job["id"], "inventory.repair.created", aggregate_type="return_repair_job")
        _finish(receipt, result, 201, result_type="return_repair_job")
        return result


def update_repair(
    scope: CompanyScope,
    job_id: uuid.UUID,
    data: dict[str, Any],
    *,
    revision: int,
    key: str,
    request_id: str | None,
) -> dict[str, Any]:
    with transaction.atomic(durable=True):
        bind_and_verify_company(scope)
        receipt, replay = _claim(
            scope, "inventory.repair.update", str(job_id), key, data | {"revision": revision}
        )
        assert_company_write(scope.company_id, "inventory", "inventory.repair.manage")
        if data["status"] in {"qc_passed", "qc_failed"}:
            assert_company_write(scope.company_id, "inventory", "inventory.quality.approve")
        if replay is not None:
            return replay
        _context(request_id)
        source = _one(
            "SELECT l.sales_return_id FROM erp.return_repair_jobs j JOIN "
            "erp.sales_return_lines l ON l.company_id=j.company_id AND "
            "l.id=j.sales_return_line_id WHERE j.company_id=%s AND j.id=%s",
            [scope.company_id, job_id],
        )
        # Source-first order matches dispositions and prevents QC/release lock inversions.
        return_revision = _one(
            "SELECT row_version FROM erp.sales_returns WHERE company_id=%s AND id=%s",
            [scope.company_id, source["sales_return_id"]],
        )
        _locked(scope, source["sales_return_id"], return_revision["row_version"], posted=True)
        job = _one(
            "SELECT * FROM erp.return_repair_jobs WHERE company_id=%s AND id=%s FOR UPDATE",
            [scope.company_id, job_id],
        )
        if job["row_version"] != revision:
            raise PreconditionFailed(job["row_version"])
        _run(
            "UPDATE erp.return_repair_jobs SET status=%s,diagnosis=%s,estimated_cost=%s,"
            "qc_notes=%s WHERE company_id=%s AND id=%s",
            [
                data["status"],
                data.get("diagnosis", job["diagnosis"]),
                data.get("estimated_cost", job["estimated_cost"]),
                data.get("qc_notes"),
                scope.company_id,
                job_id,
            ],
        )
        result = repair_detail(scope.company_id, job_id)
        _effect(
            scope,
            job_id,
            f"inventory.repair.updated.{result['row_version']}",
            aggregate_type="return_repair_job",
        )
        _finish(receipt, result, result_type="return_repair_job")
        return result


def list_repairs(
    company_id: uuid.UUID, after: uuid.UUID | None, limit: int
) -> list[dict[str, Any]]:
    return cast(
        list[dict[str, Any]],
        _json(
            _rows(
                "SELECT * FROM erp.return_repair_jobs WHERE company_id=%s AND "
                "(%s::uuid IS NULL OR id>%s) ORDER BY id LIMIT %s",
                [company_id, after, after, limit],
            )
        ),
    )
