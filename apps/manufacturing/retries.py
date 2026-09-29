"""Retry only rolled-back PostgreSQL lock failures with the original receipt key."""

import random
import time
from collections.abc import Callable
from functools import wraps

from django.db import OperationalError


def retry_lock_failure[**P, R](command: Callable[P, R]) -> Callable[P, R]:
    @wraps(command)
    def run(*args: P.args, **kwargs: P.kwargs) -> R:
        for attempt in range(3):
            try:
                return command(*args, **kwargs)
            except OperationalError as exc:
                if (
                    getattr(exc.__cause__, "sqlstate", None) not in {"40P01", "40001"}
                    or attempt == 2
                ):
                    raise
                time.sleep(random.uniform(0.01, 0.03) * (attempt + 1))
        raise AssertionError("Unreachable retry loop")

    return run
