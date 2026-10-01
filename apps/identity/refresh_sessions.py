"""Redis-backed, per-device opaque refresh sessions."""

import hashlib
import hmac
import secrets
import uuid
from datetime import UTC, datetime
from typing import Any

from django.conf import settings
from django_redis import get_redis_connection  # type: ignore[import-untyped]
from redis.exceptions import RedisError

from common.api.errors import APIError

ROTATE_SCRIPT = """
local session = KEYS[1]
local user_sessions = KEYS[2]
local sid = ARGV[1]
local expected = ARGV[2]
local replacement = ARGV[3]
local used_at = ARGV[4]
local ttl = tonumber(ARGV[5])
local current = redis.call('HGET', session, 'refresh_token_hash')
if not current then return 0 end
if current ~= expected then
  redis.call('DEL', session)
  redis.call('SREM', user_sessions, sid)
  return -1
end
redis.call('HSET', session, 'refresh_token_hash', replacement, 'last_used_at', used_at)
redis.call('EXPIRE', session, ttl)
redis.call('EXPIRE', user_sessions, ttl)
return 1
"""

REVOKE_SCRIPT = """
local user_id = redis.call('HGET', KEYS[1], 'user_id')
if not user_id then return 0 end
redis.call('DEL', KEYS[1])
redis.call('SREM', 'auth:user_sessions:' .. user_id, ARGV[1])
return 1
"""

LOGOUT_SCRIPT = """
local current = redis.call('HGET', KEYS[1], 'refresh_token_hash')
if not current or current ~= ARGV[2] then return 0 end
local user_id = redis.call('HGET', KEYS[1], 'user_id')
redis.call('DEL', KEYS[1])
redis.call('SREM', 'auth:user_sessions:' .. user_id, ARGV[1])
return 1
"""

REVOKE_ALL_SCRIPT = """
local ids = redis.call('SMEMBERS', KEYS[1])
for _, sid in ipairs(ids) do redis.call('DEL', 'auth:session:' .. sid) end
redis.call('DEL', KEYS[1])
return #ids
"""


def _redis() -> Any:
    return get_redis_connection("default")


def _unavailable() -> APIError:
    return APIError(
        code="AUTH_STORE_UNAVAILABLE",
        message="Authentication is temporarily unavailable.",
        status_code=503,
        retryable=True,
    )


def _session_key(session_id: uuid.UUID) -> str:
    return f"auth:session:{session_id}"


def _user_key(user_id: uuid.UUID) -> str:
    return f"auth:user_sessions:{user_id}"


def _decode(raw: dict[bytes, bytes]) -> dict[str, str]:
    return {key.decode(): value.decode() for key, value in raw.items()}


def _digest(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def parse_credential(value: str | None) -> tuple[uuid.UUID, str] | None:
    if not value or len(value) > 200:
        return None
    pieces = value.split(".")
    if len(pieces) != 2 or len(pieces[1]) < 40:
        return None
    try:
        return uuid.UUID(pieces[0]), pieces[1]
    except ValueError:
        return None


def create_session(
    user_id: uuid.UUID, auth_revision: int, user_agent: str
) -> tuple[uuid.UUID, str]:
    session_id = uuid.uuid4()
    family_id = uuid.uuid4()
    secret = secrets.token_urlsafe(48)
    now = datetime.now(UTC).isoformat()
    ttl = settings.AUTH_REFRESH_SESSION_SECONDS
    try:
        redis = _redis()
        with redis.pipeline(transaction=True) as pipe:
            pipe.hset(
                _session_key(session_id),
                mapping={
                    "user_id": str(user_id),
                    "refresh_token_hash": _digest(secret),
                    "token_family_id": str(family_id),
                    "auth_revision": str(auth_revision),
                    "created_at": now,
                    "last_used_at": now,
                    "user_agent": user_agent[:256],
                },
            )
            pipe.expire(_session_key(session_id), ttl)
            pipe.sadd(_user_key(user_id), str(session_id))
            pipe.expire(_user_key(user_id), ttl)
            pipe.execute()
    except RedisError as exc:
        raise _unavailable() from exc
    return session_id, f"{session_id}.{secret}"


def get_session(session_id: uuid.UUID) -> dict[str, str] | None:
    try:
        raw = _redis().hgetall(_session_key(session_id))
    except RedisError as exc:
        raise _unavailable() from exc
    return _decode(raw) if raw else None


def rotate_session(session_id: uuid.UUID, secret: str, session: dict[str, str]) -> str:
    user_id = uuid.UUID(session["user_id"])
    expected = _digest(secret)
    if not hmac.compare_digest(expected, session["refresh_token_hash"]):
        revoke_session(session_id)
        raise APIError(
            code="REFRESH_REUSE_DETECTED", message="Refresh session revoked.", status_code=401
        )
    replacement = secrets.token_urlsafe(48)
    try:
        result = _redis().eval(
            ROTATE_SCRIPT,
            2,
            _session_key(session_id),
            _user_key(user_id),
            str(session_id),
            expected,
            _digest(replacement),
            datetime.now(UTC).isoformat(),
            settings.AUTH_REFRESH_SESSION_SECONDS,
        )
    except RedisError as exc:
        raise _unavailable() from exc
    if result == -1:
        raise APIError(
            code="REFRESH_REUSE_DETECTED", message="Refresh session revoked.", status_code=401
        )
    if result != 1:
        raise APIError(
            code="REFRESH_SESSION_EXPIRED", message="Refresh session expired.", status_code=401
        )
    return f"{session_id}.{replacement}"


def revoke_session(session_id: uuid.UUID) -> None:
    try:
        _redis().eval(REVOKE_SCRIPT, 1, _session_key(session_id), str(session_id))
    except RedisError as exc:
        raise _unavailable() from exc


def logout_session(session_id: uuid.UUID, secret: str) -> None:
    try:
        _redis().eval(LOGOUT_SCRIPT, 1, _session_key(session_id), str(session_id), _digest(secret))
    except RedisError as exc:
        raise _unavailable() from exc


def revoke_all_sessions(user_id: uuid.UUID) -> None:
    try:
        _redis().eval(REVOKE_ALL_SCRIPT, 1, _user_key(user_id))
    except RedisError as exc:
        raise _unavailable() from exc


def list_sessions(user_id: uuid.UUID, current: uuid.UUID | None) -> list[dict[str, Any]]:
    try:
        redis = _redis()
        ids = redis.smembers(_user_key(user_id))
        parsed = [uuid.UUID(raw.decode()) for raw in ids]
        with redis.pipeline() as pipe:
            for sid in parsed:
                pipe.hgetall(_session_key(sid))
            rows = pipe.execute()
        stale = [str(sid) for sid, raw in zip(parsed, rows, strict=True) if not raw]
        if stale:
            redis.srem(_user_key(user_id), *stale)
    except RedisError as exc:
        raise _unavailable() from exc
    result = []
    for sid, raw in zip(parsed, rows, strict=True):
        if not raw:
            continue
        row = _decode(raw)
        if row.get("user_id") != str(user_id):
            continue
        result.append(
            {
                "session_id": str(sid),
                "created_at": row["created_at"],
                "last_used_at": row["last_used_at"],
                "user_agent": row.get("user_agent", ""),
                "is_current": sid == current,
            }
        )
    return sorted(result, key=lambda row: row["created_at"], reverse=True)
