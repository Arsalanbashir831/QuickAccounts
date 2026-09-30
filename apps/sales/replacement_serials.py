"""Immutable links between returned and shipped replacement serials."""

import uuid
from typing import Any, cast

from django.db import transaction

from apps.payments.services import _claim, _context, _json, _rows, _run
from apps.sales.return_services import _effect, _finish, _locked, _one
from common.access.scopes import CompanyScope, assert_company_write, bind_and_verify_company
from common.api.errors import Conflict


def link_replacement_serial(
    scope: CompanyScope,
    return_id: uuid.UUID,
    data: dict[str, Any],
    *,
    revision: int,
    key: str,
    request_id: str | None,
) -> dict[str, Any]:
    with transaction.atomic(durable=True):
        bind_and_verify_company(scope)
        receipt, replay = _claim(
            scope, "sales.return.replacement_serial", str(return_id),
            key, data | {"revision": revision},
        )
        assert_company_write(scope.company_id, "sales", "sales.return.post")
        if replay is not None:
            return replay
        _context(request_id)
        _locked(scope, return_id, revision, posted=True)
        replacement = _one(
            "SELECT * FROM erp.sales_return_replacements WHERE company_id=%s "
            "AND sales_return_id=%s FOR UPDATE",
            [scope.company_id, return_id],
        )
        # Lock physical identities in stable order before the database provenance guard.
        selected = _rows(
            "SELECT id FROM erp.inventory_serials WHERE company_id=%s "
            "AND id IN (%s,%s) ORDER BY id FOR UPDATE",
            [scope.company_id, data["original_serial_id"], data["replacement_serial_id"]],
        )
        if len(selected) != 2:
            raise Conflict(
                "REPLACEMENT_SERIAL_INVALID", "Two distinct registered serials are required."
            )
        source = _one(
            "SELECT id FROM erp.sales_return_serials WHERE company_id=%s "
            "AND sales_return_id=%s AND serial_id=%s AND status='posted' FOR UPDATE",
            [scope.company_id, return_id, data["original_serial_id"]],
        )
        linked = _one(
            "INSERT INTO erp.replacement_serial_links(company_id,replacement_id,"
            "source_return_serial_id,replacement_serial_id,replacement_invoice_line_id) "
            "VALUES (%s,%s,%s,%s,%s) RETURNING *",
            [
                scope.company_id, replacement["id"], source["id"],
                data["replacement_serial_id"], data["replacement_invoice_line_id"],
            ],
        )
        for serial_id, action in (
            (data["original_serial_id"], "replaced_out"),
            (data["replacement_serial_id"], "replacement_issued"),
        ):
            _run(
                "INSERT INTO erp.serial_movements(company_id,serial_id,movement_kind,"
                "quantity_delta,source_type,source_id,source_line_id,occurred_at,"
                "actor_user_id) VALUES (%s,%s,%s,0,'sales_return_replacement',%s,%s,"
                "clock_timestamp(),identity.current_user_id())",
                [
                    scope.company_id, serial_id, action, replacement["id"],
                    data["replacement_invoice_line_id"],
                ],
            )
        _effect(
            scope, linked["id"], "sales.return.replacement_serial.linked",
            aggregate_type="replacement_serial_link",
        )
        result = cast(dict[str, Any], _json(linked))
        _finish(receipt, result, 201, result_type="replacement_serial_link")
        return result


def list_replacement_serials(
    company_id: uuid.UUID, return_id: uuid.UUID, after: uuid.UUID | None, limit: int
) -> dict[str, Any]:
    rows = _rows(
        "SELECT l.*,r.serial_id AS original_serial_id FROM erp.replacement_serial_links l "
        "JOIN erp.sales_return_replacements p ON p.company_id=l.company_id "
        "AND p.id=l.replacement_id "
        "JOIN erp.sales_return_serials r ON r.company_id=l.company_id "
        "AND r.id=l.source_return_serial_id WHERE l.company_id=%s "
        "AND p.sales_return_id=%s AND (%s::uuid IS NULL OR l.id>%s::uuid) "
        "ORDER BY l.id LIMIT %s",
        [company_id, return_id, after, after, limit + 1],
    )
    return {
        "results": _json(rows[:limit]),
        "next_cursor": str(rows[limit - 1]["id"]) if len(rows) > limit else None,
    }
