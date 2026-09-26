import base64
import datetime as dt
import hashlib
import json
import uuid
from typing import Any, cast

from django.db import connection

from common.api.errors import APIError


def _json_value(value: object) -> object:
    if isinstance(value, (uuid.UUID, dt.date, dt.datetime)):
        return str(value)
    return value


def _rows(cursor: Any) -> list[dict[str, Any]]:
    names = [column.name for column in cursor.description]
    return [
        {name: _json_value(value) for name, value in zip(names, row, strict=True)}
        for row in cursor.fetchall()
    ]


def _filter_fingerprint(*, q: str | None, kind: str | None, is_active: bool | None) -> str:
    payload = json.dumps(
        {"is_active": is_active, "kind": kind, "q": q},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(payload).hexdigest()[:16]


def encode_partner_cursor(
    display_name: str,
    partner_id: str,
    *,
    q: str | None,
    kind: str | None,
    is_active: bool | None,
) -> str:
    payload = json.dumps(
        [display_name, partner_id, _filter_fingerprint(q=q, kind=kind, is_active=is_active)],
        separators=(",", ":"),
    ).encode()
    return base64.urlsafe_b64encode(payload).decode().rstrip("=")


def decode_partner_cursor(
    value: str,
    *,
    q: str | None,
    kind: str | None,
    is_active: bool | None,
) -> tuple[str, uuid.UUID]:
    try:
        padded = value + "=" * (-len(value) % 4)
        decoded = json.loads(base64.urlsafe_b64decode(padded))
        display_name, partner_id, fingerprint = decoded
        parsed_id = uuid.UUID(partner_id)
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise APIError(code="INVALID_CURSOR", message="The pagination cursor is invalid.") from exc
    expected = _filter_fingerprint(q=q, kind=kind, is_active=is_active)
    if not isinstance(display_name, str) or fingerprint != expected:
        raise APIError(
            code="INVALID_CURSOR",
            message="The pagination cursor does not match the active filters.",
        )
    return display_name, parsed_id


def partner_detail(company_id: uuid.UUID, partner_id: uuid.UUID) -> dict[str, Any] | None:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT id, partner_code, display_name, legal_name, partner_kind,
                   email, phone, is_active, row_version, created_at, updated_at
            FROM erp.business_partners
            WHERE company_id = %s AND id = %s
            """,
            [company_id, partner_id],
        )
        rows = _rows(cursor)
    return rows[0] if rows else None


def list_partners(
    company_id: uuid.UUID,
    *,
    limit: int,
    cursor: str | None,
    q: str | None,
    kind: str | None,
    is_active: bool | None,
) -> tuple[list[dict[str, Any]], str | None]:
    where = ["company_id = %s"]
    params: list[object] = [company_id]
    if q:
        where.append(
            "(starts_with(lower(display_name), lower(%s)) "
            "OR starts_with(lower(partner_code), lower(%s)))"
        )
        params.extend([q, q])
    if kind:
        where.append("partner_kind = %s")
        params.append(kind)
    if is_active is not None:
        where.append("is_active = %s")
        params.append(is_active)
    if cursor:
        display_name, partner_id = decode_partner_cursor(
            cursor, q=q, kind=kind, is_active=is_active
        )
        where.append("(display_name, id) > (%s, %s)")
        params.extend([display_name, partner_id])
    params.append(limit + 1)
    with connection.cursor() as db_cursor:
        db_cursor.execute(
            f"""
            SELECT id, partner_code, display_name, legal_name, partner_kind,
                   email, phone, is_active, row_version, created_at, updated_at
            FROM erp.business_partners
            WHERE {" AND ".join(where)}
            ORDER BY display_name, id
            LIMIT %s
            """,
            params,
        )
        rows = _rows(db_cursor)
    page = rows[:limit]
    next_cursor = None
    if len(rows) > limit and page:
        next_cursor = encode_partner_cursor(
            cast(str, page[-1]["display_name"]),
            cast(str, page[-1]["id"]),
            q=q,
            kind=kind,
            is_active=is_active,
        )
    return page, next_cursor


def _encode_nested_cursor(values: list[str]) -> str:
    payload = json.dumps(values, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(payload).decode().rstrip("=")


def _decode_nested_cursor(value: str, *, expected_size: int) -> list[str]:
    try:
        padded = value + "=" * (-len(value) % 4)
        decoded = json.loads(base64.urlsafe_b64decode(padded))
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise APIError(code="INVALID_CURSOR", message="The pagination cursor is invalid.") from exc
    if (
        not isinstance(decoded, list)
        or len(decoded) != expected_size
        or not all(isinstance(item, str) for item in decoded)
    ):
        raise APIError(code="INVALID_CURSOR", message="The pagination cursor is invalid.")
    return cast(list[str], decoded)


def partner_address_detail(
    company_id: uuid.UUID,
    partner_id: uuid.UUID,
    address_id: uuid.UUID,
) -> dict[str, Any] | None:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT id, partner_id, address_kind, line_1, line_2, city, region,
                   postal_code, country_code, is_default, row_version,
                   created_at, updated_at
            FROM erp.partner_addresses
            WHERE company_id = %s AND partner_id = %s AND id = %s
            """,
            [company_id, partner_id, address_id],
        )
        rows = _rows(cursor)
    return rows[0] if rows else None


def list_partner_addresses(
    company_id: uuid.UUID,
    partner_id: uuid.UUID,
    *,
    limit: int,
    cursor: str | None,
    kind: str | None,
) -> tuple[list[dict[str, Any]], str | None]:
    where = ["company_id = %s", "partner_id = %s"]
    params: list[object] = [company_id, partner_id]
    if kind:
        where.append("address_kind = %s")
        params.append(kind)
    if cursor:
        created_at, address_id, fingerprint = _decode_nested_cursor(cursor, expected_size=3)
        try:
            dt.datetime.fromisoformat(created_at)
            parsed_id = uuid.UUID(address_id)
        except ValueError as exc:
            raise APIError(
                code="INVALID_CURSOR", message="The pagination cursor is invalid."
            ) from exc
        expected = hashlib.sha256((kind or "").encode()).hexdigest()[:16]
        if fingerprint != expected:
            raise APIError(
                code="INVALID_CURSOR",
                message="The pagination cursor does not match the active filters.",
            )
        where.append("(created_at, id) > (%s, %s)")
        params.extend([created_at, parsed_id])
    params.append(limit + 1)
    with connection.cursor() as db_cursor:
        db_cursor.execute(
            f"""
            SELECT id, partner_id, address_kind, line_1, line_2, city, region,
                   postal_code, country_code, is_default, row_version,
                   created_at, updated_at
            FROM erp.partner_addresses
            WHERE {" AND ".join(where)}
            ORDER BY created_at, id
            LIMIT %s
            """,
            params,
        )
        rows = _rows(db_cursor)
    page = rows[:limit]
    next_cursor = None
    if len(rows) > limit and page:
        fingerprint = hashlib.sha256((kind or "").encode()).hexdigest()[:16]
        next_cursor = _encode_nested_cursor(
            [
                cast(str, page[-1]["created_at"]),
                cast(str, page[-1]["id"]),
                fingerprint,
            ]
        )
    return page, next_cursor


def partner_tax_registration_detail(
    company_id: uuid.UUID,
    partner_id: uuid.UUID,
    registration_id: uuid.UUID,
) -> dict[str, Any] | None:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT r.id, r.partner_id, r.jurisdiction_id,
                   j.jurisdiction_code, j.name AS jurisdiction_name,
                   r.tax_type_id, t.code AS tax_type_code, t.name AS tax_type_name,
                   t.tax_family, r.registration_number, r.valid_from, r.valid_to,
                   r.is_verified, r.is_active, r.created_at
            FROM erp.partner_tax_registrations r
            JOIN erp.tax_jurisdictions j ON j.id = r.jurisdiction_id
            JOIN erp.tax_types t
              ON t.jurisdiction_id = r.jurisdiction_id AND t.id = r.tax_type_id
            WHERE r.company_id = %s AND r.partner_id = %s AND r.id = %s
            """,
            [company_id, partner_id, registration_id],
        )
        rows = _rows(cursor)
    return rows[0] if rows else None


def list_partner_tax_registrations(
    company_id: uuid.UUID,
    partner_id: uuid.UUID,
    *,
    limit: int,
    cursor: str | None,
    is_active: bool | None,
) -> tuple[list[dict[str, Any]], str | None]:
    where = ["r.company_id = %s", "r.partner_id = %s"]
    params: list[object] = [company_id, partner_id]
    if is_active is not None:
        where.append("r.is_active = %s")
        params.append(is_active)
    if cursor:
        valid_from, registration_id, fingerprint = _decode_nested_cursor(cursor, expected_size=3)
        try:
            dt.date.fromisoformat(valid_from)
            parsed_id = uuid.UUID(registration_id)
        except ValueError as exc:
            raise APIError(
                code="INVALID_CURSOR", message="The pagination cursor is invalid."
            ) from exc
        expected = hashlib.sha256(str(is_active).encode()).hexdigest()[:16]
        if fingerprint != expected:
            raise APIError(
                code="INVALID_CURSOR",
                message="The pagination cursor does not match the active filters.",
            )
        where.append("(r.valid_from, r.id) < (%s, %s)")
        params.extend([valid_from, parsed_id])
    params.append(limit + 1)
    with connection.cursor() as db_cursor:
        db_cursor.execute(
            f"""
            SELECT r.id, r.partner_id, r.jurisdiction_id,
                   j.jurisdiction_code, j.name AS jurisdiction_name,
                   r.tax_type_id, t.code AS tax_type_code, t.name AS tax_type_name,
                   t.tax_family, r.registration_number, r.valid_from, r.valid_to,
                   r.is_verified, r.is_active, r.created_at
            FROM erp.partner_tax_registrations r
            JOIN erp.tax_jurisdictions j ON j.id = r.jurisdiction_id
            JOIN erp.tax_types t
              ON t.jurisdiction_id = r.jurisdiction_id AND t.id = r.tax_type_id
            WHERE {" AND ".join(where)}
            ORDER BY r.valid_from DESC, r.id DESC
            LIMIT %s
            """,
            params,
        )
        rows = _rows(db_cursor)
    page = rows[:limit]
    next_cursor = None
    if len(rows) > limit and page:
        fingerprint = hashlib.sha256(str(is_active).encode()).hexdigest()[:16]
        next_cursor = _encode_nested_cursor(
            [
                cast(str, page[-1]["valid_from"]),
                cast(str, page[-1]["id"]),
                fingerprint,
            ]
        )
    return page, next_cursor
