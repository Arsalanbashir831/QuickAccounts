"""Authentication orchestration, independent of HTTP cookie handling."""

from uuid import UUID

from django.contrib.auth import authenticate
from django.contrib.auth.password_validation import validate_password
from django.db import transaction
from rest_framework.exceptions import ValidationError

from apps.identity import login_throttle, refresh_sessions
from apps.identity.models import User
from apps.identity.tokens import access_token
from common.api.errors import APIError


def login(email: str, password: str, ip: str, user_agent: str) -> tuple[dict[str, str | int], str]:
    email = email.strip().lower()
    if login_throttle.blocked(email, ip):
        raise APIError(
            code="LOGIN_RATE_LIMITED",
            message="Too many login attempts. Try again later.",
            status_code=429,
        )
    user = authenticate(username=email, password=password)
    if user is None or not user.is_active:
        login_throttle.record_failure(email, ip)
        raise APIError(code="INVALID_CREDENTIALS", message="Invalid credentials.", status_code=401)
    login_throttle.clear_failures(email, ip)
    sid, credential = refresh_sessions.create_session(user.id, user.auth_revision, user_agent)
    return access_token(user, str(sid)), credential


def refresh(credential: str | None) -> tuple[dict[str, str | int], str]:
    parsed = refresh_sessions.parse_credential(credential)
    if parsed is None:
        raise APIError(
            code="INVALID_REFRESH", message="Invalid refresh credential.", status_code=401
        )
    sid, secret = parsed
    session = refresh_sessions.get_session(sid)
    if session is None:
        raise APIError(
            code="REFRESH_SESSION_EXPIRED", message="Refresh session expired.", status_code=401
        )
    try:
        user = User.objects.get(id=UUID(session["user_id"]), is_active=True)
    except (User.DoesNotExist, ValueError, KeyError) as exc:
        refresh_sessions.revoke_session(sid)
        raise APIError(
            code="REFRESH_SESSION_REVOKED", message="Refresh session revoked.", status_code=401
        ) from exc
    if str(user.auth_revision) != session.get("auth_revision"):
        refresh_sessions.revoke_session(sid)
        raise APIError(
            code="REFRESH_SESSION_REVOKED", message="Refresh session revoked.", status_code=401
        )
    replacement = refresh_sessions.rotate_session(sid, secret, session)
    return access_token(user, str(sid)), replacement


def logout(credential: str | None) -> None:
    parsed = refresh_sessions.parse_credential(credential)
    if parsed:
        refresh_sessions.logout_session(*parsed)


def change_password(user: User, old_password: str, new_password: str) -> None:
    if not user.check_password(old_password):
        raise APIError(code="INVALID_CREDENTIALS", message="Invalid credentials.", status_code=400)
    try:
        validate_password(new_password, user)
    except Exception as exc:
        raise ValidationError({"new_password": list(getattr(exc, "messages", [str(exc)]))}) from exc
    # Redis revocation precedes commit; auth_revision invalidates every issued JWT.
    with transaction.atomic():
        locked = User.objects.select_for_update().get(pk=user.pk)
        if not locked.check_password(old_password):
            raise APIError(
                code="INVALID_CREDENTIALS", message="Invalid credentials.", status_code=400
            )
        refresh_sessions.revoke_all_sessions(locked.id)
        locked.set_password(new_password)
        locked.auth_revision += 1
        locked.save(update_fields=["password", "auth_revision"])
