from django.http import HttpRequest

from common.api.errors import APIError, PreconditionRequired


def require_idempotency_key(request: HttpRequest) -> str:
    key = request.headers.get("Idempotency-Key", "").strip()
    if not key:
        raise PreconditionRequired("Idempotency-Key")
    if len(key) > 128:
        raise APIError(
            code="INVALID_IDEMPOTENCY_KEY",
            message="Idempotency-Key must not exceed 128 characters.",
            details={"header": "Idempotency-Key"},
        )
    return key


def require_revision(request: HttpRequest) -> int:
    value = request.headers.get("If-Match", "").strip().strip("W/").strip('"')
    if not value:
        raise PreconditionRequired("If-Match")
    try:
        revision = int(value)
    except ValueError as exc:
        raise APIError(
            code="INVALID_REVISION",
            message="If-Match must contain an integer revision.",
            details={"header": "If-Match"},
        ) from exc
    if revision < 1:
        raise APIError(code="INVALID_REVISION", message="Revision must be positive.")
    return revision
