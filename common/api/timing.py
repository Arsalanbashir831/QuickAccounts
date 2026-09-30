"""Emit per-request latency without coupling the API to a metrics vendor."""

import logging
import time
from collections.abc import Callable

from django.http import HttpRequest, HttpResponse

logger = logging.getLogger("quickaccounts.request_metrics")


class RequestTimingMiddleware:
    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        start = time.monotonic()
        response = self.get_response(request)
        duration_ms = (time.monotonic() - start) * 1000
        response["Server-Timing"] = f"app;dur={duration_ms:.2f}"
        logger.info("request_latency_ms=%0.2f status=%s method=%s path=%s",
                    duration_ms, response.status_code, request.method,
                    request.resolver_match.route if request.resolver_match else "unresolved")
        return response
