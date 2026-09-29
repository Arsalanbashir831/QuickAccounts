"""Explicit approval and settlement proof for physical supplier claims."""

import uuid
from typing import Any, cast

from django.db import transaction

from apps.payments.services import _claim, _context, _json, _rows, _run
from apps.sales.return_services import _effect, _finish, _one
from common.access.scopes import CompanyScope, assert_company_write, bind_and_verify_company
from common.api.errors import Conflict, PreconditionFailed


def claim_detail(company: uuid.UUID, claim_id: uuid.UUID) -> dict[str, Any]:
    return cast(
        dict[str, Any],
        _json(
            _one(
                "SELECT c.*,l.purchase_bill_id,l.item_id,l.quantity,b.supplier_id "
                "FROM erp.supplier_return_claims c JOIN erp.purchase_bill_lines l "
                "ON l.company_id=c.company_id AND l.id=c.source_bill_line_id "
                "JOIN erp.purchase_bills b ON b.company_id=l.company_id "
                "AND b.id=l.purchase_bill_id "
                "WHERE c.company_id=%s AND c.id=%s",
                [company, claim_id],
            )
        ),
    )


def list_claims(company: uuid.UUID, after: uuid.UUID | None, limit: int) -> list[dict[str, Any]]:
    return cast(
        list[dict[str, Any]],
        _json(
            _rows(
                "SELECT * FROM erp.supplier_return_claims WHERE company_id=%s "
                "AND (%s::uuid IS NULL OR id>%s) ORDER BY id LIMIT %s",
                [company, after, after, limit],
            )
        ),
    )


def create_claim(
    scope: CompanyScope, data: dict[str, Any], *, key: str, request_id: str | None
) -> dict[str, Any]:
    with transaction.atomic(durable=True):
        bind_and_verify_company(scope)
        receipt, replay = _claim(scope, "purchasing.claim.create", data["claim_no"], key, data)
        assert_company_write(scope.company_id, "purchasing", "purchasing.bill.edit_draft")
        if replay is not None:
            return replay
        _context(request_id)
        source = _one(
            "SELECT purchase_bill_id FROM erp.purchase_bill_lines WHERE company_id=%s AND id=%s",
            [scope.company_id, data["source_bill_line_id"]],
        )
        _one(
            "SELECT id FROM erp.purchase_bills WHERE company_id=%s AND id=%s FOR UPDATE",
            [scope.company_id, source["purchase_bill_id"]],
        )
        if data.get("disposal_case_id"):
            assert_company_write(scope.company_id, "inventory", "inventory.post")
            _one(
                "SELECT id FROM erp.stock_disposal_cases WHERE company_id=%s AND id=%s FOR UPDATE",
                [scope.company_id, data["disposal_case_id"]],
            )
        row = _one(
            "INSERT INTO erp.supplier_return_claims(company_id,claim_no,"
            "source_bill_line_id,warehouse_id,disposal_case_id,reason) "
            "VALUES (%s,%s,%s,%s,%s,%s) RETURNING id",
            [
                scope.company_id,
                data["claim_no"],
                data["source_bill_line_id"],
                data["warehouse_id"],
                data.get("disposal_case_id"),
                data["reason"],
            ],
        )
        result = claim_detail(scope.company_id, row["id"])
        _effect(
            scope, row["id"], "purchasing.claim.created", aggregate_type="supplier_return_claim"
        )
        _finish(receipt, result, 201, result_type="supplier_return_claim")
        return result


def command_claim(
    scope: CompanyScope,
    claim_id: uuid.UUID,
    data: dict[str, Any],
    *,
    revision: int,
    key: str,
    request_id: str | None,
) -> dict[str, Any]:
    with transaction.atomic(durable=True):
        bind_and_verify_company(scope)
        receipt, replay = _claim(
            scope, "purchasing.claim.command", str(claim_id), key, data | {"revision": revision}
        )
        assert_company_write(scope.company_id, "purchasing", "purchasing.bill.edit_draft")
        if replay is not None:
            return replay
        _context(request_id)
        preliminary = _one(
            "SELECT source_bill_line_id,disposal_case_id FROM "
            "erp.supplier_return_claims WHERE company_id=%s AND id=%s",
            [scope.company_id, claim_id],
        )
        source = _one(
            "SELECT purchase_bill_id FROM erp.purchase_bill_lines WHERE company_id=%s AND id=%s",
            [scope.company_id, preliminary["source_bill_line_id"]],
        )
        _one(
            "SELECT id FROM erp.purchase_bills WHERE company_id=%s AND id=%s FOR UPDATE",
            [scope.company_id, source["purchase_bill_id"]],
        )
        if data["action"] == "settle":
            _one(
                "SELECT id FROM erp.purchase_bills WHERE company_id=%s AND id=%s FOR UPDATE",
                [scope.company_id, data["supplier_credit_id"]],
            )
        if preliminary["disposal_case_id"]:
            assert_company_write(scope.company_id, "inventory", "inventory.post")
            _one(
                "SELECT id FROM erp.stock_disposal_cases WHERE company_id=%s AND id=%s FOR UPDATE",
                [scope.company_id, preliminary["disposal_case_id"]],
            )
        claim = _one(
            "SELECT * FROM erp.supplier_return_claims WHERE company_id=%s AND id=%s FOR UPDATE",
            [scope.company_id, claim_id],
        )
        if claim["row_version"] != revision:
            raise PreconditionFailed(claim["row_version"])
        if data["action"] == "approve":
            assert_company_write(scope.company_id, "purchasing", "purchasing.claim.approve")
            if claim["status"] != "draft" or not data.get("approve_supplier_claim"):
                raise Conflict("CLAIM_APPROVAL_REQUIRED", "Explicit draft claim approval required.")
            _run(
                "UPDATE erp.supplier_return_claims SET status='approved' "
                "WHERE company_id=%s AND id=%s",
                [scope.company_id, claim_id],
            )
        elif data["action"] == "reject":
            if claim["status"] not in ("draft", "approved"):
                raise Conflict("CLAIM_NOT_OPEN", "Only open claims can be rejected.")
            _run(
                "UPDATE erp.supplier_return_claims SET status='rejected' "
                "WHERE company_id=%s AND id=%s",
                [scope.company_id, claim_id],
            )
        else:
            if claim["status"] != "approved":
                raise Conflict("CLAIM_NOT_APPROVED", "Approve the claim before settlement.")
            _run(
                "UPDATE erp.supplier_return_claims SET status='settled',supplier_credit_id=%s "
                "WHERE company_id=%s AND id=%s",
                [data["supplier_credit_id"], scope.company_id, claim_id],
            )
            if claim["disposal_case_id"]:
                _run(
                    "UPDATE erp.stock_disposal_cases SET status='executed' "
                    "WHERE company_id=%s AND id=%s",
                    [scope.company_id, claim["disposal_case_id"]],
                )
        result = claim_detail(scope.company_id, claim_id)
        _effect(
            scope,
            claim_id,
            f"purchasing.claim.{data['action']}.{result['row_version']}",
            aggregate_type="supplier_return_claim",
        )
        _finish(receipt, result, result_type="supplier_return_claim")
        return result
