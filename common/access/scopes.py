import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from django.conf import settings
from django.db import connection, transaction

from common.api.errors import APIError, PermissionDenied, ScopeNotFound
from common.db.context import set_local_context


@dataclass(frozen=True, slots=True)
class CompanyScope:
    tenant_id: uuid.UUID
    company_id: uuid.UUID
    user_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class TenantScope:
    tenant_id: uuid.UUID
    user_id: uuid.UUID


def _fetchone(sql: str, params: list[object]) -> tuple[object, ...] | None:
    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        return cursor.fetchone()


def bind_and_verify_company(scope: CompanyScope) -> None:
    set_local_context(tenant_id=scope.tenant_id, user_id=scope.user_id)
    row = _fetchone(
        """
        SELECT c.is_active
        FROM erp.companies c
        JOIN identity.company_memberships cm
          ON cm.tenant_id = c.tenant_id AND cm.company_id = c.id
        JOIN identity.tenant_memberships tm
          ON tm.tenant_id = c.tenant_id AND tm.user_id = cm.user_id
        WHERE c.tenant_id = %s AND c.id = %s AND cm.user_id = %s
          AND cm.is_active AND tm.is_active
        """,
        [scope.tenant_id, scope.company_id, scope.user_id],
    )
    if row is None:
        raise ScopeNotFound()
    if not row[0]:
        raise PermissionDenied("COMPANY_INACTIVE", "The company is inactive.")


def bind_and_verify_tenant(scope: TenantScope, *, administer: bool = False) -> str:
    set_local_context(tenant_id=scope.tenant_id, user_id=scope.user_id)
    row = _fetchone(
        """
        SELECT tm.tenant_role
        FROM identity.tenant_memberships tm
        JOIN identity.users u ON u.id=tm.user_id
        WHERE tm.tenant_id=%s AND tm.user_id=%s AND tm.is_active AND u.is_active
        """,
        [scope.tenant_id, scope.user_id],
    )
    if row is None:
        raise ScopeNotFound()
    role = str(row[0])
    if administer and role not in {"owner", "admin"}:
        raise PermissionDenied("TENANT_ADMIN_REQUIRED", "Tenant administration is required.")
    return role


def require_permission(company_id: uuid.UUID, permission: str) -> None:
    row = _fetchone(
        "SELECT identity.has_company_permission(%s, %s)",
        [company_id, permission],
    )
    if row is None or not row[0]:
        raise PermissionDenied()


def require_module_read(company_id: uuid.UUID, module: str) -> None:
    row = _fetchone(
        """
        SELECT mode FROM erp.company_module_settings
        WHERE company_id = %s AND module_code = %s
        """,
        [company_id, module],
    )
    if row is None or row[0] == "disabled":
        raise PermissionDenied("MODULE_DISABLED", "The accounting module is disabled.")


def resolve_product_id() -> uuid.UUID:
    row = _fetchone(
        "SELECT id FROM licensing.products WHERE product_code = %s AND is_active",
        [settings.ERP_PRODUCT_CODE],
    )
    if row is None:
        raise APIError(
            code="PRODUCT_NOT_CONFIGURED",
            message="The ERP product catalog is not configured.",
            status_code=503,
            retryable=False,
        )
    return row[0]  # type: ignore[return-value]


def assert_company_write(company_id: uuid.UUID, module: str, permission: str) -> None:
    product_id = resolve_product_id()
    try:
        _fetchone(
            "SELECT erp.assert_company_write(%s, %s, %s, %s)",
            [company_id, product_id, module, permission],
        )
    except Exception as exc:
        cause = getattr(exc, "__cause__", None)
        if getattr(exc, "sqlstate", None) == "42501" or getattr(
            cause, "sqlstate", None
        ) == "42501":
            diagnostic = getattr(cause, "diag", None) or getattr(exc, "diag", None)
            reason = str(getattr(diagnostic, "message_primary", "")).lower()
            if "no designated license" in reason:
                raise PermissionDenied(
                    "LICENSE_NOT_BOUND", "No designated product license is bound."
                ) from exc
            if "license expired" in reason:
                raise PermissionDenied(
                    "LICENSE_EXPIRED", "The product license has expired."
                ) from exc
            if "license inactive" in reason:
                raise PermissionDenied(
                    "LICENSE_INACTIVE", "The product license is not active."
                ) from exc
            if "module/dependency" in reason:
                raise PermissionDenied(
                    "MODULE_WRITE_DENIED",
                    "The module, dependency, or licensed feature is not writable.",
                ) from exc
            raise PermissionDenied(
                "WRITE_ACCESS_DENIED",
                "License, module, company, or action policy denies this write.",
            ) from exc
        raise


@contextmanager
def company_read_scope(
    scope: CompanyScope,
    *,
    permission: str,
    module: str = "accounting",
) -> Iterator[None]:
    with transaction.atomic():
        bind_and_verify_company(scope)
        require_module_read(scope.company_id, module)
        require_permission(scope.company_id, permission)
        yield
