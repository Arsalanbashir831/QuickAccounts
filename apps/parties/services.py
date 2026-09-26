import uuid
from typing import Any, cast

from django.db import IntegrityError, connection, transaction

from apps.parties.selectors import (
    partner_address_detail,
    partner_detail,
    partner_tax_registration_detail,
)
from common.access.scopes import CompanyScope, assert_company_write, bind_and_verify_company
from common.api.errors import APIError, Conflict, PreconditionFailed, ScopeNotFound


def _constraint_name(exc: IntegrityError) -> str | None:
    diagnostic = getattr(getattr(exc, "__cause__", None), "diag", None)
    return cast(str | None, getattr(diagnostic, "constraint_name", None))


def _sqlstate(exc: IntegrityError) -> str | None:
    cause = getattr(exc, "__cause__", None)
    return cast(str | None, getattr(cause, "sqlstate", None))


def _write_conflict(exc: IntegrityError) -> Conflict:
    constraint = _constraint_name(exc)
    if constraint == "business_partners_company_id_partner_code_key":
        return Conflict(
            "PARTNER_CODE_EXISTS",
            "A business partner with this code already exists in the company.",
            {"field": "partner_code"},
        )
    return Conflict(
        "PARTNER_WRITE_CONFLICT",
        "The business partner could not be saved because it conflicts with existing data.",
        {"constraint": constraint},
    )


def _set_request_id(request_id: str | None) -> None:
    with connection.cursor() as cursor:
        cursor.execute("SELECT set_config('app.request_id', %s, true)", [request_id or ""])


def create_partner(
    scope: CompanyScope,
    data: dict[str, Any],
    *,
    request_id: str | None,
) -> dict[str, Any]:
    try:
        with transaction.atomic(durable=True):
            bind_and_verify_company(scope)
            assert_company_write(scope.company_id, "core", "party.manage")
            _set_request_id(request_id)
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO erp.business_partners(
                        company_id, partner_code, display_name, legal_name,
                        partner_kind, email, phone, is_active
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                    RETURNING id
                    """,
                    [
                        scope.company_id,
                        data["partner_code"],
                        data["display_name"],
                        data.get("legal_name"),
                        data["partner_kind"],
                        data.get("email"),
                        data.get("phone"),
                        data["is_active"],
                    ],
                )
                partner_id = cast(uuid.UUID, cursor.fetchone()[0])
            result = partner_detail(scope.company_id, partner_id)
            assert result is not None
            return result
    except IntegrityError as exc:
        raise _write_conflict(exc) from exc


def update_partner(
    scope: CompanyScope,
    partner_id: uuid.UUID,
    data: dict[str, Any],
    *,
    expected_revision: int,
    request_id: str | None,
) -> dict[str, Any]:
    allowed = {
        "partner_code",
        "display_name",
        "legal_name",
        "partner_kind",
        "email",
        "phone",
        "is_active",
    }
    fields = [field for field in data if field in allowed]
    assignments = ", ".join(f"{field} = %s" for field in fields)
    try:
        with transaction.atomic(durable=True):
            bind_and_verify_company(scope)
            assert_company_write(scope.company_id, "core", "party.manage")
            _set_request_id(request_id)
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""
                    UPDATE erp.business_partners
                    SET {assignments}
                    WHERE company_id = %s AND id = %s AND row_version = %s
                    RETURNING row_version
                    """,
                    [
                        *(data[field] for field in fields),
                        scope.company_id,
                        partner_id,
                        expected_revision,
                    ],
                )
                updated = cursor.fetchone()
                if updated is None:
                    cursor.execute(
                        """
                        SELECT row_version FROM erp.business_partners
                        WHERE company_id = %s AND id = %s
                        """,
                        [scope.company_id, partner_id],
                    )
                    current = cursor.fetchone()
                    if current is None:
                        raise ScopeNotFound()
                    raise PreconditionFailed(int(current[0]))
            result = partner_detail(scope.company_id, partner_id)
            assert result is not None
            return result
    except IntegrityError as exc:
        raise _write_conflict(exc) from exc


def _lock_partner(company_id: uuid.UUID, partner_id: uuid.UUID) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT 1 FROM erp.business_partners
            WHERE company_id = %s AND id = %s
            FOR KEY SHARE
            """,
            [company_id, partner_id],
        )
        if cursor.fetchone() is None:
            raise ScopeNotFound()


def _address_write_conflict(exc: IntegrityError) -> Conflict:
    constraint = _constraint_name(exc)
    if constraint == "ux_partner_addresses_default_kind":
        return Conflict(
            "DEFAULT_ADDRESS_EXISTS",
            "This partner already has a default address for the selected address kind.",
            {"field": "is_default"},
        )
    return Conflict(
        "PARTNER_ADDRESS_CONFLICT",
        "The partner address could not be saved because it conflicts with existing data.",
        {"constraint": constraint},
    )


def create_partner_address(
    scope: CompanyScope,
    partner_id: uuid.UUID,
    data: dict[str, Any],
    *,
    request_id: str | None,
) -> dict[str, Any]:
    try:
        with transaction.atomic(durable=True):
            bind_and_verify_company(scope)
            assert_company_write(scope.company_id, "core", "party.manage")
            _lock_partner(scope.company_id, partner_id)
            _set_request_id(request_id)
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO erp.partner_addresses(
                        company_id, partner_id, address_kind, line_1, line_2,
                        city, region, postal_code, country_code, is_default
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    RETURNING id
                    """,
                    [
                        scope.company_id,
                        partner_id,
                        data["address_kind"],
                        data["line_1"],
                        data.get("line_2"),
                        data.get("city"),
                        data.get("region"),
                        data.get("postal_code"),
                        data.get("country_code"),
                        data["is_default"],
                    ],
                )
                address_id = cast(uuid.UUID, cursor.fetchone()[0])
            result = partner_address_detail(scope.company_id, partner_id, address_id)
            assert result is not None
            return result
    except IntegrityError as exc:
        raise _address_write_conflict(exc) from exc


def update_partner_address(
    scope: CompanyScope,
    partner_id: uuid.UUID,
    address_id: uuid.UUID,
    data: dict[str, Any],
    *,
    expected_revision: int,
    request_id: str | None,
) -> dict[str, Any]:
    allowed = {
        "address_kind",
        "line_1",
        "line_2",
        "city",
        "region",
        "postal_code",
        "country_code",
        "is_default",
    }
    fields = [field for field in data if field in allowed]
    assignments = ", ".join(f"{field} = %s" for field in fields)
    try:
        with transaction.atomic(durable=True):
            bind_and_verify_company(scope)
            assert_company_write(scope.company_id, "core", "party.manage")
            _lock_partner(scope.company_id, partner_id)
            _set_request_id(request_id)
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""
                    UPDATE erp.partner_addresses
                    SET {assignments}
                    WHERE company_id = %s AND partner_id = %s AND id = %s
                      AND row_version = %s
                    RETURNING row_version
                    """,
                    [
                        *(data[field] for field in fields),
                        scope.company_id,
                        partner_id,
                        address_id,
                        expected_revision,
                    ],
                )
                if cursor.fetchone() is None:
                    cursor.execute(
                        """
                        SELECT row_version FROM erp.partner_addresses
                        WHERE company_id = %s AND partner_id = %s AND id = %s
                        """,
                        [scope.company_id, partner_id, address_id],
                    )
                    current = cursor.fetchone()
                    if current is None:
                        raise ScopeNotFound()
                    raise PreconditionFailed(int(current[0]))
            result = partner_address_detail(scope.company_id, partner_id, address_id)
            assert result is not None
            return result
    except IntegrityError as exc:
        raise _address_write_conflict(exc) from exc


def _validate_tax_registration_catalog(jurisdiction_id: uuid.UUID, tax_type_id: uuid.UUID) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT 1
            FROM erp.tax_jurisdictions j
            JOIN erp.tax_types t ON t.jurisdiction_id = j.id
            WHERE j.id = %s AND t.id = %s AND j.is_active AND t.is_active
            """,
            [jurisdiction_id, tax_type_id],
        )
        if cursor.fetchone() is None:
            raise APIError(
                code="INVALID_TAX_REGISTRATION_TYPE",
                message="The tax type is not active in the selected jurisdiction.",
            )


def create_partner_tax_registration(
    scope: CompanyScope,
    partner_id: uuid.UUID,
    data: dict[str, Any],
    *,
    request_id: str | None,
) -> dict[str, Any]:
    try:
        with transaction.atomic(durable=True):
            bind_and_verify_company(scope)
            assert_company_write(scope.company_id, "tax_calculation", "tax.configuration.manage")
            _lock_partner(scope.company_id, partner_id)
            _validate_tax_registration_catalog(data["jurisdiction_id"], data["tax_type_id"])
            _set_request_id(request_id)
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO erp.partner_tax_registrations(
                        company_id, partner_id, jurisdiction_id, tax_type_id,
                        registration_number, valid_from, valid_to, is_verified, is_active
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    RETURNING id
                    """,
                    [
                        scope.company_id,
                        partner_id,
                        data["jurisdiction_id"],
                        data["tax_type_id"],
                        data["registration_number"],
                        data["valid_from"],
                        data.get("valid_to"),
                        data["is_verified"],
                        data["is_active"],
                    ],
                )
                registration_id = cast(uuid.UUID, cursor.fetchone()[0])
            result = partner_tax_registration_detail(scope.company_id, partner_id, registration_id)
            assert result is not None
            return result
    except IntegrityError as exc:
        if _sqlstate(exc) == "23505":
            raise Conflict(
                "TAX_REGISTRATION_EXISTS",
                "This partner tax registration already exists.",
                {"constraint": _constraint_name(exc)},
            ) from exc
        raise Conflict(
            "TAX_REGISTRATION_CONFLICT",
            "The partner tax registration conflicts with existing data.",
            {"constraint": _constraint_name(exc)},
        ) from exc
