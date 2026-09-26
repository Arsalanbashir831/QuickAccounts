from typing import Any

from rest_framework.exceptions import APIException


class APIError(APIException):
    status_code = 400
    default_code = "REQUEST_ERROR"
    default_detail = "The request could not be completed."

    def __init__(
        self,
        *,
        code: str,
        message: str,
        status_code: int = 400,
        details: Any = None,
        retryable: bool = False,
    ) -> None:
        self.status_code = status_code
        self.default_code = code
        self.retryable = retryable
        super().__init__(detail=details if details is not None else {}, code=code)
        self.message = message


class ScopeNotFound(APIError):
    def __init__(self) -> None:
        super().__init__(
            code="SCOPE_NOT_FOUND",
            message="The requested tenant or company is not accessible.",
            status_code=404,
        )


class PermissionDenied(APIError):
    def __init__(self, code: str = "PERMISSION_DENIED", message: str = "Permission denied."):
        super().__init__(code=code, message=message, status_code=403)


class Conflict(APIError):
    def __init__(self, code: str, message: str, details: Any = None) -> None:
        super().__init__(code=code, message=message, status_code=409, details=details)


class PreconditionRequired(APIError):
    def __init__(self, header: str) -> None:
        super().__init__(
            code="PRECONDITION_REQUIRED",
            message=f"The {header} header is required.",
            status_code=428,
            details={"header": header},
        )


class PreconditionFailed(APIError):
    def __init__(self, current_revision: int | None = None) -> None:
        details = {} if current_revision is None else {"current_revision": current_revision}
        super().__init__(
            code="REVISION_MISMATCH",
            message="The resource changed since it was read.",
            status_code=412,
            details=details,
        )
