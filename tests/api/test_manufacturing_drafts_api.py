import concurrent.futures
import datetime as dt
import threading
import uuid
from decimal import Decimal as D
from importlib import import_module
from unittest.mock import patch

import pytest
from django.db import DatabaseError, close_old_connections, connection, transaction

from apps.inventory.services import update_item
from apps.manufacturing.services import save_bom, save_order, transition_bom
from common.access.scopes import CompanyScope
from common.api.errors import APIError
from tests.api.test_purchase_bill_drafts_api import _bind_license, _change_module, _root
from tests.api.test_sales_returns_api import _command

pytestmark = [pytest.mark.api, pytest.mark.p0, pytest.mark.django_db(transaction=True)]


def _setup(context: dict, *, seed_stock: bool = True) -> dict:
    client = context["client"]
    _bind_license(context, ("module.inventory", "module.manufacturing"))
    _change_module(client, context, "inventory", "enabled")
    _change_module(client, context, "manufacturing", "enabled")
    ids = {name: uuid.uuid4() for name in ("category", "uom", "output", "material", "warehouse")}
    with connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO erp.uom_categories(id,code,name) VALUES (%s,%s,'Manufacturing quantity')",
            [ids["category"], f"MFG-{ids['category'].hex}"],
        )
        cursor.execute(
            "INSERT INTO erp.uoms(id,category_id,code,name,to_base_factor) "
            "VALUES (%s,%s,%s,'Each',1)",
            [ids["uom"], ids["category"], f"MFG-{ids['uom'].hex}"],
        )
        for name in ("output", "material"):
            cursor.execute(
                "INSERT INTO erp.items(id,company_id,sku,name,item_kind,base_uom_id) "
                "VALUES (%s,%s,%s,%s,'stock',%s)",
                [ids[name], context["company"], f"MFG-{ids[name].hex}", name, ids["uom"]],
            )
        cursor.execute(
            "INSERT INTO erp.warehouses(id,company_id,code,name) "
            "VALUES (%s,%s,'MFG','Manufacturing')",
            [ids["warehouse"], context["company"]],
        )
    if not seed_stock:
        return {"client": client, "ids": ids, "root": f"{_root(context)}/manufacturing"}
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("SELECT set_config('app.tenant_id',%s,true)", [str(context["tenant"])])
        cursor.execute(
            "INSERT INTO erp.stock_movements(company_id,event_key,occurred_at,warehouse_id,item_id,"
            "movement_kind,quantity_delta,unit_cost_company,value_delta_company) "
            "VALUES (%s,%s,clock_timestamp(),%s,%s,'receipt',100,1,100)",
            [context["company"], str(uuid.uuid4()), ids["warehouse"], ids["material"]],
        )
    return {"client": client, "ids": ids, "root": f"{_root(context)}/manufacturing"}


def _bom_payload(setup: dict, revision: str = "A", **extra) -> dict:
    return {
        "output_item_id": str(setup["ids"]["output"]),
        "revision": revision,
        "output_quantity": "2",
        "effective_from": "2026-09-01",
        "effective_to": "2026-09-30",
        "lines": [
            {
                "component_item_id": str(setup["ids"]["material"]),
                "quantity_per_output": "3",
                "scrap_percent": "10",
            }
        ],
        **extra,
    }


def _bom(setup: dict, **extra) -> dict:
    response = setup["client"].post(
        f"{setup['root']}/boms",
        _bom_payload(setup, **extra),
        format="json",
        HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
    )
    assert response.status_code == 201, response.json()
    return response.json()


def _activate(setup: dict, bom: dict, key: str | None = None):
    return _command(
        setup["client"], f"{setup['root']}/boms/{bom['id']}/activate", {}, bom["row_version"], key
    )


def _active(setup: dict, **extra) -> dict:
    response = _activate(setup, _bom(setup, **extra))
    assert response.status_code == 200, response.json()
    return response.json()


def _order_payload(setup: dict, bom: dict, **extra) -> dict:
    return {
        "production_no": f"PLAN-{uuid.uuid4()}",
        "bom_id": bom["id"],
        "warehouse_id": str(setup["ids"]["warehouse"]),
        "planned_quantity": "10",
        "planned_start": "2026-09-28",
        "planned_end": "2026-09-30",
        **extra,
    }


def _order(setup: dict, bom: dict, **extra) -> dict:
    response = setup["client"].post(
        f"{setup['root']}/orders",
        _order_payload(setup, bom, **extra),
        format="json",
        HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
    )
    assert response.status_code == 201, response.json()
    return response.json()


def _counts(context: dict) -> dict:
    result = {}
    with connection.cursor() as cursor:
        for table in (
            "boms",
            "bom_lines",
            "production_orders",
            "production_order_requirements",
            "production_execution_batches",
            "production_material_issues",
            "production_outputs",
            "production_material_returns",
            "stock_movements",
            "inventory_reservations",
            "inventory_cost_layers",
            "inventory_cost_allocations",
            "journal_entries",
            "company_audit_events",
            "outbox_events",
            "api_command_receipts",
        ):
            cursor.execute(
                f"SELECT count(*) FROM erp.{table} WHERE company_id=%s", [context["company"]]
            )
            result[table] = cursor.fetchone()[0]
    return result


def _scope(context: dict) -> CompanyScope:
    return CompanyScope(context["tenant"], context["company"], context["user"])


def _patch(setup: dict, resource: str, document: dict, data: dict, key: str | None = None):
    return setup["client"].patch(
        f"{setup['root']}/{resource}/{document['id']}",
        data,
        format="json",
        HTTP_IF_MATCH=f'"{document["row_version"]}"',
        HTTP_IDEMPOTENCY_KEY=key or str(uuid.uuid4()),
    )


def test_mfg001_bom_revisions_activation_freeze_retirement_and_replay(
    accounting_context: dict,
) -> None:
    setup = _setup(accounting_context)
    payload = _bom_payload(setup)
    url = f"{setup['root']}/boms"
    created = setup["client"].post(url, payload, format="json", HTTP_IDEMPOTENCY_KEY="create-bom")
    assert created.status_code == 201, created.json()
    assert (
        setup["client"].post(url, payload, format="json", HTTP_IDEMPOTENCY_KEY="create-bom").json()
        == created.json()
    )
    bom = created.json()
    assert bom["revision"] == "A" and bom["row_version"] == 1
    assert "draft_edit_xid" not in bom
    changed = _patch(setup, "boms", bom, {"output_quantity": "4"}, "edit-bom")
    assert changed.status_code == 200, changed.json()
    assert changed.json()["row_version"] == 2
    assert changed.json()["lines"] == bom["lines"]
    assert _patch(setup, "boms", bom, {"output_quantity": "4"}, "edit-bom").json() == changed.json()
    active = _activate(setup, changed.json(), "approve-bom")
    assert active.status_code == 200, active.json()
    assert _activate(setup, changed.json(), "approve-bom").json() == active.json()
    assert active.json()["activated_at"] and active.json()["activated_by"] == str(
        accounting_context["user"]
    )
    assert _patch(setup, "boms", active.json(), {"revision": "different"}).status_code == 409
    for statement in (
        "UPDATE erp.boms SET output_quantity=99 WHERE id=%s",
        "DELETE FROM erp.boms WHERE id=%s",
        "UPDATE erp.bom_lines SET quantity_per_output=99 WHERE bom_id=%s",
        "DELETE FROM erp.bom_lines WHERE bom_id=%s",
    ):
        with pytest.raises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(statement, [bom["id"]])
    next_version = _bom(setup, revision="B")
    assert next_version["id"] != bom["id"]
    duplicate = setup["client"].post(
        url, payload, format="json", HTTP_IDEMPOTENCY_KEY="duplicate-bom"
    )
    assert duplicate.status_code == 409, duplicate.json()
    assert duplicate.json()["error"]["code"] == "MFG_BOM_REVISION_EXISTS"
    retired = _command(
        setup["client"],
        f"{url}/{bom['id']}/retire",
        {"reason": "Superseded"},
        active.json()["row_version"],
        "retire-bom",
    )
    assert retired.status_code == 200, retired.json()
    assert (
        retired.json()["status"] == "retired"
        and retired.json()["retirement_reason"] == "Superseded"
    )
    assert _activate(setup, retired.json()).status_code == 409


def test_mfg002_planned_order_exact_snapshots_updates_cancellation_and_no_posting(
    accounting_context: dict,
) -> None:
    setup = _setup(accounting_context)
    before = _counts(accounting_context)
    active = _active(setup)
    payload = _order_payload(setup, active)
    created = setup["client"].post(
        f"{setup['root']}/orders", payload, format="json", HTTP_IDEMPOTENCY_KEY="create-plan"
    )
    assert created.status_code == 201, created.json()
    assert (
        setup["client"]
        .post(f"{setup['root']}/orders", payload, format="json", HTTP_IDEMPOTENCY_KEY="create-plan")
        .json()
        == created.json()
    )
    order = created.json()
    assert order["status"] == "planned" and D(order["completed_quantity"]) == 0
    assert order["bom_revision_snapshot"] == "A"
    assert order["bom_row_version_snapshot"] == active["row_version"]
    assert D(order["bom_output_quantity_snapshot"]) == 2
    assert order["output_uom_id_snapshot"] == str(setup["ids"]["uom"])
    assert D(order["requirements"][0]["required_quantity"]) == D("16.5")
    assert order["availability_is_reserved"] is False and "planning_edit_xid" not in order
    edited = _patch(setup, "orders", order, {"planned_quantity": "20"}, "edit-plan")
    assert edited.status_code == 200, edited.json()
    assert D(edited.json()["requirements"][0]["required_quantity"]) == 33
    assert edited.json()["row_version"] == 2
    assert (
        _patch(setup, "orders", order, {"planned_quantity": "20"}, "edit-plan").json()
        == edited.json()
    )
    duplicate = setup["client"].post(
        f"{setup['root']}/orders", payload, format="json", HTTP_IDEMPOTENCY_KEY="duplicate-plan"
    )
    assert duplicate.status_code == 409, duplicate.json()
    assert duplicate.json()["error"]["code"] == "MFG_PRODUCTION_NUMBER_EXISTS"
    cancelled = _command(
        setup["client"],
        f"{setup['root']}/orders/{order['id']}/cancel",
        {"reason": "Plan withdrawn"},
        edited.json()["row_version"],
        "cancel-plan",
    )
    assert cancelled.status_code == 200, cancelled.json()
    assert cancelled.json()["requirements"] == edited.json()["requirements"]
    assert (
        _command(
            setup["client"],
            f"{setup['root']}/orders/{order['id']}/cancel",
            {"reason": "Plan withdrawn"},
            edited.json()["row_version"],
            "cancel-plan",
        ).json()
        == cancelled.json()
    )
    assert _patch(setup, "orders", cancelled.json(), {"planned_quantity": "1"}).status_code == 409
    after = _counts(accounting_context)
    for table in (
        "stock_movements",
        "inventory_reservations",
        "inventory_cost_layers",
        "inventory_cost_allocations",
        "journal_entries",
    ):
        assert after[table] == before[table]


@pytest.mark.parametrize(
    "invalid",
    [
        "empty",
        "too_many",
        "duplicate",
        "self",
        "inactive",
        "service",
        "dates",
        "foreign",
        "readonly",
    ],
)
def test_invalid_bom_drafts_roll_back(accounting_context: dict, invalid: str) -> None:
    setup = _setup(accounting_context)
    payload = _bom_payload(setup)
    if invalid == "empty":
        payload["lines"] = []
    elif invalid == "too_many":
        payload["lines"] *= 101
    elif invalid == "duplicate":
        payload["lines"] *= 2
    elif invalid == "self":
        payload["lines"][0]["component_item_id"] = str(setup["ids"]["output"])
    elif invalid in {"inactive", "service"}:
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE erp.items SET "
                + ("is_active=false" if invalid == "inactive" else "item_kind='service'")
                + " WHERE id=%s",
                [setup["ids"]["material"]],
            )
    elif invalid == "dates":
        payload["effective_to"] = "2026-08-01"
    elif invalid == "foreign":
        payload["lines"][0]["component_item_id"] = str(uuid.uuid4())
    elif invalid == "readonly":
        payload["status"] = "active"
    before = _counts(accounting_context)
    response = setup["client"].post(
        f"{setup['root']}/boms", payload, format="json", HTTP_IDEMPOTENCY_KEY="invalid-bom"
    )
    assert response.status_code in {400, 409}, response.json()
    assert _counts(accounting_context) == before


@pytest.mark.parametrize(
    "invalid",
    [
        "draft",
        "retired",
        "future",
        "expired",
        "shortage",
        "rounding",
        "warehouse",
        "dates",
        "readonly",
    ],
)
def test_mfg002_invalid_order_plans_have_no_effects(accounting_context: dict, invalid: str) -> None:
    setup = _setup(accounting_context)
    extras = {}
    if invalid == "future":
        extras = {"effective_from": "2026-10-01", "effective_to": None}
    elif invalid == "expired":
        extras = {"effective_from": "2026-01-01", "effective_to": "2026-08-31"}
    elif invalid == "rounding":
        extras = {
            "output_quantity": "3",
            "lines": [
                {"component_item_id": str(setup["ids"]["material"]), "quantity_per_output": "1"}
            ],
        }
    bom = _bom(setup, **extras) if invalid == "draft" else _active(setup, **extras)
    if invalid == "retired":
        retired = _command(
            setup["client"],
            f"{setup['root']}/boms/{bom['id']}/retire",
            {"reason": "Retired"},
            bom["row_version"],
        )
        assert retired.status_code == 200, retired.json()
    payload = _order_payload(setup, bom)
    if invalid == "shortage":
        payload["planned_quantity"] = "100"
    elif invalid == "rounding":
        payload["planned_quantity"] = "1"
    elif invalid == "warehouse":
        payload["warehouse_id"] = str(uuid.uuid4())
    elif invalid == "dates":
        payload["planned_end"] = "2026-09-01"
    elif invalid == "readonly":
        payload["completed_quantity"] = "1"
    before = _counts(accounting_context)
    response = setup["client"].post(
        f"{setup['root']}/orders", payload, format="json", HTTP_IDEMPOTENCY_KEY="invalid-plan"
    )
    assert response.status_code in {400, 409}, response.json()
    if invalid == "rounding":
        assert response.json()["error"]["code"] == "MFG_QUANTITY_ROUNDING_REQUIRED"
    if invalid == "shortage":
        assert response.json()["error"]["code"] == "MFG_MATERIAL_UNAVAILABLE"
    assert _counts(accounting_context) == before


def test_mfg007_stale_bom_and_order_revisions_and_strict_headers(accounting_context: dict) -> None:
    setup = _setup(accounting_context)
    draft = _bom(setup)
    changed = _patch(setup, "boms", draft, {"revision": "A1"})
    assert changed.status_code == 200, changed.json()
    assert _patch(setup, "boms", draft, {"output_quantity": "5"}).status_code == 412
    assert _activate(setup, draft).status_code == 412
    active = _activate(setup, changed.json()).json()
    order = _order(setup, active)
    edited = _patch(setup, "orders", order, {"planned_start": "2026-09-29"})
    assert edited.status_code == 200, edited.json()
    assert _patch(setup, "orders", order, {"planned_quantity": "5"}).status_code == 412
    assert (
        setup["client"]
        .patch(
            f"{setup['root']}/orders/{order['id']}",
            {"planned_quantity": "5"},
            format="json",
            HTTP_IDEMPOTENCY_KEY="missing-revision",
        )
        .status_code
        == 428
    )
    assert (
        setup["client"]
        .post(f"{setup['root']}/boms", _bom_payload(setup, "B"), format="json")
        .status_code
        == 428
    )
    changed_payload = _bom_payload(setup, "CHANGED")
    replay_key = "different-body"
    assert (
        setup["client"]
        .post(
            f"{setup['root']}/boms",
            _bom_payload(setup, "B"),
            format="json",
            HTTP_IDEMPOTENCY_KEY=replay_key,
        )
        .status_code
        == 201
    )
    assert (
        setup["client"]
        .post(
            f"{setup['root']}/boms", changed_payload, format="json", HTTP_IDEMPOTENCY_KEY=replay_key
        )
        .status_code
        == 409
    )


@pytest.mark.parametrize("operation", ["create", "update"])
@pytest.mark.parametrize("failure", ["missing", "raised"])
def test_plan_requirement_failure_rolls_back_aggregate_and_receipt(
    accounting_context: dict, operation: str, failure: str
) -> None:
    setup = _setup(accounting_context)
    bom = _active(setup)
    order = _order(setup, bom) if operation == "update" else None
    payload = {"planned_quantity": "20"} if order else _order_payload(setup, bom)
    before = _counts(accounting_context)
    kwargs = (
        {"return_value": None}
        if failure == "missing"
        else {"side_effect": DatabaseError("injected")}
    )
    with patch("apps.manufacturing.services._retain_requirements", **kwargs):
        if order:
            response = _patch(setup, "orders", order, payload, "failed-plan")
        else:
            response = setup["client"].post(
                f"{setup['root']}/orders",
                payload,
                format="json",
                HTTP_IDEMPOTENCY_KEY="failed-plan",
            )
    assert response.status_code == 409, response.json()
    assert _counts(accounting_context) == before
    if order:
        assert setup["client"].get(f"{setup['root']}/orders/{order['id']}").json() == order
        assert _patch(setup, "orders", order, payload, "failed-plan").status_code == 200
    else:
        assert (
            setup["client"]
            .post(
                f"{setup['root']}/orders",
                payload,
                format="json",
                HTTP_IDEMPOTENCY_KEY="failed-plan",
            )
            .status_code
            == 201
        )


def test_failed_activation_outbox_rolls_back_approval_metadata(accounting_context: dict) -> None:
    setup = _setup(accounting_context)
    bom = _bom(setup)
    before = _counts(accounting_context)
    with patch("apps.manufacturing.services._event", side_effect=DatabaseError("injected")):
        response = _activate(setup, bom, "failed-approval")
    assert response.status_code == 409, response.json()
    assert _counts(accounting_context) == before
    assert setup["client"].get(f"{setup['root']}/boms/{bom['id']}").json() == bom
    assert _activate(setup, bom, "failed-approval").status_code == 200


def test_retirement_preserves_planned_snapshots_and_item_semantics(
    accounting_context: dict,
) -> None:
    setup = _setup(accounting_context)
    bom = _active(setup)
    order = _order(setup, bom)
    with (
        pytest.raises(DatabaseError, match="MFG_ITEM_REFERENCED"),
        transaction.atomic(),
        connection.cursor() as cursor,
    ):
        cursor.execute(
            "UPDATE erp.items SET is_active=false WHERE id=%s", [setup["ids"]["material"]]
        )
    retired = _command(
        setup["client"],
        f"{setup['root']}/boms/{bom['id']}/retire",
        {"reason": "New revision"},
        bom["row_version"],
    )
    assert retired.status_code == 200, retired.json()
    assert setup["client"].get(f"{setup['root']}/orders/{order['id']}").json() == order
    assert _patch(setup, "orders", order, {"planned_quantity": "11"}).status_code == 409
    cancelled = _command(
        setup["client"],
        f"{setup['root']}/orders/{order['id']}/cancel",
        {"reason": "Use new BOM"},
        order["row_version"],
    )
    assert cancelled.status_code == 200, cancelled.json()
    with connection.cursor() as cursor:
        cursor.execute(
            "UPDATE erp.items SET is_active=false WHERE id=%s", [setup["ids"]["material"]]
        )
    assert (
        setup["client"].get(f"{setup['root']}/orders/{order['id']}").json()["requirements"]
        == order["requirements"]
    )


def test_database_cannot_bypass_planning_aggregate_or_execute_production(
    accounting_context: dict,
) -> None:
    setup = _setup(accounting_context)
    draft = _bom(setup)
    with (
        pytest.raises(DatabaseError, match="revisioned draft"),
        transaction.atomic(),
        connection.cursor() as cursor,
    ):
        cursor.execute(
            "UPDATE erp.bom_lines SET quantity_per_output=5 WHERE bom_id=%s", [draft["id"]]
        )
    active = _activate(setup, draft).json()
    order = _order(setup, active)
    for sql, params in (
        (
            "UPDATE erp.production_order_requirements SET required_quantity=1 "
            "WHERE production_order_id=%s",
            [order["id"]],
        ),
        ("DELETE FROM erp.production_orders WHERE id=%s", [order["id"]]),
        ("UPDATE erp.production_orders SET status='released' WHERE id=%s", [order["id"]]),
    ):
        with pytest.raises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(sql, params)
    with (
        pytest.raises(DatabaseError, match="MFG_ORDER_STATE_INVALID"),
        transaction.atomic(),
        connection.cursor() as cursor,
    ):
        cursor.execute(
            "INSERT INTO erp.production_outputs(company_id,production_order_id,stock_movement_id,"
            "output_item_id,quantity_completed,cost_company) VALUES (%s,%s,%s,%s,1,1)",
            [accounting_context["company"], order["id"], uuid.uuid4(), setup["ids"]["output"]],
        )
    with (
        pytest.raises(DatabaseError, match="Cannot reverse retained manufacturing"),
        transaction.atomic(),
        connection.cursor() as cursor,
    ):
        cursor.execute(import_module("apps.database.migrations.0031_manufacturing_drafts").REVERSE)
    assert (
        setup["client"]
        .post(f"{setup['root']}/orders/{order['id']}/release", {}, format="json")
        .status_code
        == 400
    )


def test_manufacturing_list_filters_pagination_and_scope(accounting_context: dict) -> None:
    setup = _setup(accounting_context)
    first = _bom(setup)
    second = _bom(setup, revision="B")
    page = setup["client"].get(f"{setup['root']}/boms", {"limit": 1})
    assert page.status_code == 200, page.json()
    assert len(page.json()["results"]) == 1 and page.json()["next_cursor"]
    next_page = setup["client"].get(
        f"{setup['root']}/boms", {"limit": 1, "cursor": page.json()["next_cursor"]}
    )
    assert {page.json()["results"][0]["id"], next_page.json()["results"][0]["id"]} == {
        first["id"],
        second["id"],
    }
    assert next_page.json()["next_cursor"] is None
    assert (
        setup["client"].get(f"{setup['root']}/boms", {"status": "active"}).json()["results"] == []
    )
    for query in ({"limit": 201}, {"cursor": "bad"}, {"status": "bad"}, {"output_item_id": "bad"}):
        assert setup["client"].get(f"{setup['root']}/boms", query).status_code == 400
    assert setup["client"].get(f"{setup['root']}/boms/{uuid.uuid4()}").status_code == 404
    assert (
        setup["client"]
        .get(
            f"/api/v1/tenants/{accounting_context['tenant']}/companies/{uuid.uuid4()}/manufacturing/boms"
        )
        .status_code
        == 404
    )


@pytest.mark.parametrize("gate", ["read_only", "disabled", "permission", "license"])
def test_manufacturing_read_write_license_and_permission_gates(
    accounting_context: dict, gate: str
) -> None:
    setup = _setup(accounting_context)
    bom = _active(setup)
    if gate in {"read_only", "disabled"}:
        _change_module(setup["client"], accounting_context, "manufacturing", gate)
    elif gate == "permission":
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE identity.tenant_memberships SET tenant_role='member' "
                "WHERE tenant_id=%s AND user_id=%s",
                [accounting_context["tenant"], accounting_context["user"]],
            )
    else:
        _bind_license(accounting_context, ("module.inventory",))
    before = _counts(accounting_context)
    read = setup["client"].get(f"{setup['root']}/boms/{bom['id']}")
    assert read.status_code == (200 if gate in {"read_only", "license"} else 403), read.json()
    create = setup["client"].post(
        f"{setup['root']}/orders",
        _order_payload(setup, bom),
        format="json",
        HTTP_IDEMPOTENCY_KEY="denied-plan",
    )
    assert create.status_code == 403, create.json()
    assert _counts(accounting_context) == before


@pytest.mark.security
def test_manufacturing_runtime_role_rls_for_boms_orders_and_requirements(
    accounting_context: dict,
) -> None:
    setup = _setup(accounting_context)
    _order(setup, _active(setup))
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("GRANT USAGE ON SCHEMA erp TO quickaccounts_runtime")
        cursor.execute("GRANT SELECT ON ALL TABLES IN SCHEMA erp TO quickaccounts_runtime")
        cursor.execute(
            "SELECT set_config('app.tenant_id',%s,true)", [str(accounting_context["tenant"])]
        )
        cursor.execute("SET LOCAL ROLE quickaccounts_runtime")
        for table in ("boms", "bom_lines", "production_orders", "production_order_requirements"):
            cursor.execute(
                f"SELECT count(*) FROM erp.{table} WHERE company_id=%s",
                [accounting_context["company"]],
            )
            assert cursor.fetchone()[0] == 1
        cursor.execute("SELECT set_config('app.tenant_id',%s,true)", [str(uuid.uuid4())])
        for table in ("boms", "bom_lines", "production_orders", "production_order_requirements"):
            cursor.execute(
                f"SELECT count(*) FROM erp.{table} WHERE company_id=%s",
                [accounting_context["company"]],
            )
            assert cursor.fetchone()[0] == 0
        transaction.set_rollback(True)


@pytest.mark.concurrency
def test_concurrent_bom_activation_rejects_cross_revision_cycle(accounting_context: dict) -> None:
    setup = _setup(accounting_context)
    first = _bom(setup)
    second = _bom(
        setup,
        output_item_id=str(setup["ids"]["material"]),
        lines=[{"component_item_id": str(setup["ids"]["output"]), "quantity_per_output": "1"}],
    )
    barrier = threading.Barrier(2)

    def activate(bom: dict):
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            result = transition_bom(
                _scope(accounting_context),
                uuid.UUID(bom["id"]),
                "activate",
                revision=1,
                key=str(uuid.uuid4()),
            )
            return result["status"]
        except DatabaseError as exc:
            assert "MFG_BOM_CYCLE" in str(exc)
            return "cycle_rejected"
        finally:
            close_old_connections()

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(activate, (first, second)))
    assert sorted(results) == ["active", "cycle_rejected"]
    listed = setup["client"].get(f"{setup['root']}/boms").json()["results"]
    assert sorted(bom["status"] for bom in listed) == ["active", "draft"]


@pytest.mark.concurrency
def test_concurrent_bom_edit_and_activation_use_one_revision(accounting_context: dict) -> None:
    setup = _setup(accounting_context)
    draft = _bom(setup)
    barrier = threading.Barrier(2)

    def run(action: str):
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            if action == "activate":
                result = transition_bom(
                    _scope(accounting_context),
                    uuid.UUID(draft["id"]),
                    action,
                    revision=1,
                    key=str(uuid.uuid4()),
                )
            else:
                result = save_bom(
                    _scope(accounting_context),
                    {"output_quantity": D(4)},
                    bom_id=uuid.UUID(draft["id"]),
                    revision=1,
                    key=str(uuid.uuid4()),
                )
            assert result["row_version"] == 2
            return 200
        except APIError as exc:
            return exc.status_code
        finally:
            close_old_connections()

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(run, ("activate", "edit")))
    assert sorted(results) == [200, 412]


@pytest.mark.concurrency
def test_concurrent_order_edits_keep_one_complete_requirement_snapshot(
    accounting_context: dict,
) -> None:
    setup = _setup(accounting_context)
    order = _order(setup, _active(setup))
    barrier = threading.Barrier(2)

    def run(quantity: str):
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            save_order(
                _scope(accounting_context),
                {"planned_quantity": D(quantity)},
                order_id=uuid.UUID(order["id"]),
                revision=1,
                key=str(uuid.uuid4()),
            )
            return 200
        except APIError as exc:
            return exc.status_code
        finally:
            close_old_connections()

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        assert sorted(executor.map(run, ("6", "8"))) == [200, 412]
    current = setup["client"].get(f"{setup['root']}/orders/{order['id']}").json()
    assert current["row_version"] == 2 and len(current["requirements"]) == 1
    assert D(current["requirements"][0]["required_quantity"]) == D(current["planned_quantity"]) * D(
        "1.65"
    )


def test_mfg002_planning_availability_excludes_reserved_material(accounting_context: dict) -> None:
    setup = _setup(accounting_context)
    bom = _active(setup)
    root = _root(accounting_context)
    stock = setup["client"].post(
        f"{root}/inventory/documents",
        {
            "document_no": "RESERVE-MATERIAL",
            "document_kind": "adjustment_out",
            "reason": "Availability planning test",
            "document_date": "2026-09-28",
            "lines": [
                {
                    "item_id": str(setup["ids"]["material"]),
                    "quantity": "90",
                    "from_warehouse_id": str(setup["ids"]["warehouse"]),
                }
            ],
        },
        format="json",
        HTTP_IDEMPOTENCY_KEY="material-reservation-source",
    )
    assert stock.status_code == 201, stock.json()
    reserved = setup["client"].post(
        f"{root}/inventory/reservations",
        {
            "line_id": stock.json()["lines"][0]["id"],
            "quantity": "90",
            "document_revision": 1,
            "reservation_key": "material-reservation",
        },
        format="json",
        HTTP_IDEMPOTENCY_KEY="material-reservation",
    )
    assert reserved.status_code == 201, reserved.json()
    payload = _order_payload(setup, bom)
    before = _counts(accounting_context)
    denied = setup["client"].post(
        f"{setup['root']}/orders", payload, format="json", HTTP_IDEMPOTENCY_KEY="reserved-plan"
    )
    assert denied.status_code == 409, denied.json()
    assert denied.json()["error"]["code"] == "MFG_MATERIAL_UNAVAILABLE"
    assert _counts(accounting_context) == before
    released = _command(
        setup["client"],
        f"{root}/inventory/reservations/{reserved.json()['id']}/release",
        {},
        reserved.json()["row_version"],
    )
    assert released.status_code == 200, released.json()
    assert (
        setup["client"]
        .post(
            f"{setup['root']}/orders", payload, format="json", HTTP_IDEMPOTENCY_KEY="reserved-plan"
        )
        .status_code
        == 201
    )


def test_runtime_manufacturing_audit_needs_no_direct_audit_dml_grant(
    accounting_context: dict,
) -> None:
    setup = _setup(accounting_context)
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("GRANT USAGE ON SCHEMA erp,identity TO quickaccounts_runtime")
        cursor.execute("GRANT SELECT ON ALL TABLES IN SCHEMA erp TO quickaccounts_runtime")
        cursor.execute(
            "GRANT INSERT,UPDATE ON erp.boms,erp.bom_lines,erp.items TO quickaccounts_runtime"
        )
        cursor.execute(
            "REVOKE INSERT,UPDATE,DELETE ON erp.company_audit_events FROM quickaccounts_runtime"
        )
        cursor.execute(
            "SELECT set_config('app.tenant_id',%s,true)", [str(accounting_context["tenant"])]
        )
        cursor.execute(
            "SELECT set_config('app.user_id',%s,true)", [str(accounting_context["user"])]
        )
        cursor.execute("SET LOCAL ROLE quickaccounts_runtime")
        cursor.execute(
            "INSERT INTO erp.boms(company_id,output_item_id,revision,output_quantity) "
            "VALUES (%s,%s,'RUNTIME',1) RETURNING id",
            [accounting_context["company"], setup["ids"]["output"]],
        )
        bom_id = cursor.fetchone()[0]
        cursor.execute(
            "INSERT INTO erp.bom_lines(company_id,bom_id,line_no,component_item_id,"
            "quantity_per_output) "
            "VALUES (%s,%s,1,%s,1)",
            [accounting_context["company"], bom_id, setup["ids"]["material"]],
        )
        cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
        cursor.execute(
            "SELECT count(*) FROM erp.company_audit_events WHERE company_id=%s AND object_id=%s",
            [accounting_context["company"], str(bom_id)],
        )
        assert cursor.fetchone()[0] == 1
        transaction.set_rollback(True)


@pytest.mark.concurrency
def test_retirement_and_order_selection_serialize_without_changing_existing_plan(
    accounting_context: dict,
) -> None:
    setup = _setup(accounting_context)
    bom = _active(setup)
    barrier = threading.Barrier(2)
    payload = _order_payload(setup, bom)
    payload["bom_id"] = uuid.UUID(payload["bom_id"])
    payload["warehouse_id"] = setup["ids"]["warehouse"]
    payload["planned_quantity"] = D(payload["planned_quantity"])
    for field in ("planned_start", "planned_end"):
        payload[field] = dt.date.fromisoformat(payload[field])

    def run(action: str):
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            if action == "retire":
                transition_bom(
                    _scope(accounting_context),
                    uuid.UUID(bom["id"]),
                    action,
                    revision=bom["row_version"],
                    key="racing-retirement",
                    reason="Superseded",
                )
                return 200
            save_order(_scope(accounting_context), payload, key="racing-plan")
            return 201
        except APIError as exc:
            assert exc.default_code == "MFG_BOM_NOT_SELECTABLE"
            return exc.status_code
        finally:
            close_old_connections()

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(run, ("retire", "plan")))
    assert results[0] == 200 and results[1] in {201, 409}
    orders = setup["client"].get(f"{setup['root']}/orders").json()["results"]
    assert len(orders) == (1 if results[1] == 201 else 0)
    if orders:
        retained = setup["client"].get(f"{setup['root']}/orders/{orders[0]['id']}").json()
        assert retained["bom_row_version_snapshot"] == bom["row_version"]
        assert D(retained["requirements"][0]["required_quantity"]) == D("16.5")


@pytest.mark.concurrency
def test_activation_and_component_deactivation_cannot_leave_an_invalid_active_bom(
    accounting_context: dict,
) -> None:
    setup = _setup(accounting_context)
    bom = _bom(setup)
    barrier = threading.Barrier(2)

    def run(action: str):
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            if action == "activate":
                transition_bom(
                    _scope(accounting_context),
                    uuid.UUID(bom["id"]),
                    action,
                    revision=1,
                    key="activation-versus-item",
                )
            else:
                update_item(
                    _scope(accounting_context),
                    setup["ids"]["material"],
                    {"is_active": False},
                    expected_revision=1,
                    request_id=None,
                )
            return 200
        except DatabaseError as exc:
            assert any(
                code in str(exc) for code in ("MFG_ITEM_REFERENCED", "MFG_BOM_ITEMS_INVALID")
            )
            return 409
        finally:
            close_old_connections()

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        assert sorted(executor.map(run, ("activate", "deactivate"))) == [200, 409]
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT b.status,i.is_active FROM erp.boms b JOIN erp.items i "
            "ON i.company_id=b.company_id AND i.id=%s WHERE b.id=%s",
            [setup["ids"]["material"], bom["id"]],
        )
        assert cursor.fetchone() in (("active", True), ("draft", False))
