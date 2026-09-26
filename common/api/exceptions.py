from typing import Any

from rest_framework.response import Response
from rest_framework.views import exception_handler

from common.api.errors import APIError


def api_exception_handler(exc: Exception, context: dict[str, Any]) -> Response | None:
    response = exception_handler(exc, context)
    if response is None:
        return None
    request = context.get("request")
    request_id = getattr(request, "request_id", None)
    details = response.data
    if isinstance(exc, APIError):
        message = exc.message
        retryable = exc.retryable
        code = exc.default_code
    else:
        message = "The request could not be completed."
        retryable = response.status_code >= 500
        code = getattr(exc, "default_code", "request_error")
    response.data = {
        "error": {
            "code": code,
            "message": message,
            "details": details,
            "request_id": request_id,
            "retryable": retryable,
        }
    }
    return response
