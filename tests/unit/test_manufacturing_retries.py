from unittest.mock import patch

import psycopg.errors
import pytest
from django.db import OperationalError

from apps.manufacturing.retries import retry_lock_failure

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("state", ["deadlock", "serialization"])
def test_known_rolled_back_failures_retry_original_arguments(state):
    calls = []
    cause = (
        psycopg.errors.DeadlockDetected()
        if state == "deadlock"
        else psycopg.errors.SerializationFailure()
    )

    @retry_lock_failure
    def command(key):
        calls.append(key)
        if len(calls) < 3:
            raise OperationalError("Retry") from cause
        return key

    with patch("apps.manufacturing.retries.time.sleep"):
        assert command("original-key") == "original-key"
    assert calls == ["original-key"] * 3


def test_unknown_commit_errors_are_not_retried():
    calls = []

    @retry_lock_failure
    def command():
        calls.append(1)
        raise OperationalError("Disconnected")

    with pytest.raises(OperationalError):
        command()
    assert len(calls) == 1
