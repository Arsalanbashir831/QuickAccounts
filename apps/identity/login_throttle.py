"""Redis-backed failed-login throttling without account enumeration."""

import hashlib

from django.conf import settings
from redis.exceptions import RedisError

from apps.identity.refresh_sessions import _redis, _unavailable

FAIL_SCRIPT = """
local attempts = redis.call('INCR', KEYS[1])
if attempts == 1 then redis.call('EXPIRE', KEYS[1], ARGV[1]) end
return attempts
"""


def _key(email: str, ip: str) -> str:
    digest = hashlib.sha256(f"{email.lower()}|{ip}".encode()).hexdigest()
    return f"auth:login_failures:{digest}"


def blocked(email: str, ip: str) -> bool:
    try:
        count = _redis().get(_key(email, ip))
    except RedisError as exc:
        raise _unavailable() from exc
    return count is not None and int(count) >= settings.AUTH_LOGIN_FAILURE_LIMIT


def record_failure(email: str, ip: str) -> None:
    try:
        _redis().eval(FAIL_SCRIPT, 1, _key(email, ip), settings.AUTH_LOGIN_FAILURE_WINDOW_SECONDS)
    except RedisError as exc:
        raise _unavailable() from exc


def clear_failures(email: str, ip: str) -> None:
    try:
        _redis().delete(_key(email, ip))
    except RedisError as exc:
        raise _unavailable() from exc
