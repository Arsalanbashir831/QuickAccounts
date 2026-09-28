import uuid
from decimal import Decimal
from typing import Any

from apps.payments.services import _run
from apps.sales.return_services import _effect, _one
from common.access.scopes import CompanyScope


def locked_cost_policy(scope: CompanyScope) -> dict[str, Any]:
    """Called inside an already authorized inventory posting transaction."""
    created = _one(
        "WITH added AS (INSERT INTO erp.inventory_cost_policies(company_id) VALUES (%s) "
        "ON CONFLICT(company_id,version) DO NOTHING RETURNING id) "
        "SELECT (SELECT id FROM added) AS id",
        [scope.company_id],
    )["id"]
    if created:
        _effect(
            scope, created, "inventory.cost_policy.created", aggregate_type="inventory_cost_policy"
        )
    return _one(
        "SELECT * FROM erp.inventory_cost_policies WHERE company_id=%s AND version=1 FOR SHARE",
        [scope.company_id],
    )


def record_cost_basis(
    scope: CompanyScope,
    movement_id: uuid.UUID,
    policy_id: uuid.UUID,
    basis: dict[str, Decimal],
    *,
    currency_code: str,
    currency_precision: int,
) -> None:
    _run(
        "INSERT INTO erp.inventory_cost_basis_snapshots(company_id,movement_id,policy_id,"
        "currency_code,currency_precision,basis_quantity,basis_value_company,reserved_quantity,"
        "issue_quantity,issue_value_company,unit_cost_company) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        [
            scope.company_id,
            movement_id,
            policy_id,
            currency_code,
            currency_precision,
            basis["basis_quantity"],
            basis["basis_value_company"],
            basis["reserved_quantity"],
            basis["issue_quantity"],
            basis["issue_value_company"],
            basis["unit_cost_company"],
        ],
    )
