import uuid
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import pytest
from django.conf import settings
from django_redis import get_redis_connection
from redis.exceptions import ConnectionError
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from apps.identity.models import User
from apps.identity.refresh_sessions import get_session, rotate_session
from common.api.errors import APIError

PASSWORD = "A-strong-test-password-314159"


@pytest.fixture
def auth_user(accounting_context):
    user = User.objects.get(pk=accounting_context["user"])
    user.set_password(PASSWORD)
    user.save(update_fields=["password"])
    yield user
    redis = get_redis_connection("default")
    user_key = f"auth:user_sessions:{user.id}"
    for sid in redis.smembers(user_key):
        redis.delete(f"auth:session:{sid.decode()}")
    redis.delete(user_key)


def client(csrf=False):
    return APIClient(enforce_csrf_checks=csrf)


def login(c, user, password=PASSWORD):
    return c.post("/api/auth/login/", {"email": user.email, "password": password}, format="json")


def bearer(c, token):
    c.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")


@pytest.mark.django_db
def test_login_claims_cookie_and_no_plaintext_in_redis(auth_user):
    c = client()
    response = login(c, auth_user)
    assert response.status_code == 200
    assert "refresh" not in response.data
    token = AccessToken(response.data["access"])
    assert token["user_id"] == str(auth_user.id)
    assert token["sub"] == str(auth_user.id)
    assert token["sid"]
    assert token["token_type"] == "access"
    assert token["exp"] - token["iat"] == 600
    cookie = response.cookies[settings.AUTH_REFRESH_COOKIE_NAME]
    assert cookie["httponly"] and cookie["secure"]
    assert cookie["samesite"] == "Lax"
    sid, secret = cookie.value.split(".")
    row = get_redis_connection("default").hgetall(f"auth:session:{sid}")
    assert row and secret.encode() not in repr(row).encode()
    bearer(c, response.data["access"])
    with patch("apps.identity.refresh_sessions._redis", side_effect=ConnectionError("down")):
        assert c.get("/api/auth/me/").status_code == 200


@pytest.mark.django_db
def test_refresh_rotation_reuse_revokes_and_logout(auth_user):
    c = client()
    first = login(c, auth_user)
    old = first.cookies[settings.AUTH_REFRESH_COOKIE_NAME].value
    rotated = c.post("/api/auth/refresh/")
    assert rotated.status_code == 200
    fresh = rotated.cookies[settings.AUTH_REFRESH_COOKIE_NAME].value
    assert old != fresh
    c.cookies[settings.AUTH_REFRESH_COOKIE_NAME] = old
    reused = c.post("/api/auth/refresh/")
    assert reused.status_code == 401
    assert reused.data["error"]["code"] == "REFRESH_REUSE_DETECTED"
    c.cookies[settings.AUTH_REFRESH_COOKIE_NAME] = fresh
    assert c.post("/api/auth/refresh/").status_code == 401
    assert c.post("/api/auth/logout/").status_code == 204


@pytest.mark.django_db
def test_logout_requires_matching_secret(auth_user):
    c = client()
    response = login(c, auth_user)
    credential = response.cookies[settings.AUTH_REFRESH_COOKIE_NAME].value
    sid, _ = credential.split(".")
    c.cookies[settings.AUTH_REFRESH_COOKIE_NAME] = f"{sid}.{'x' * 64}"
    assert c.post("/api/auth/logout/").status_code == 204
    c.cookies[settings.AUTH_REFRESH_COOKIE_NAME] = credential
    assert c.post("/api/auth/refresh/").status_code == 200


@pytest.mark.django_db
def test_sessions_logout_all_and_password_change(auth_user):
    c1, c2 = client(), client()
    first, second = login(c1, auth_user), login(c2, auth_user)
    bearer(c1, first.data["access"])
    sessions = c1.get("/api/auth/sessions/")
    assert sessions.status_code == 200
    assert len(sessions.data["sessions"]) == 2
    sid2 = second.cookies[settings.AUTH_REFRESH_COOKIE_NAME].value.split(".")[0]
    assert c1.delete(f"/api/auth/sessions/{sid2}/").status_code == 204
    assert c2.post("/api/auth/refresh/").status_code == 401
    assert c1.post("/api/auth/logout-all/").status_code == 204
    assert c1.post("/api/auth/refresh/").status_code == 401
    third = login(c1, auth_user)
    bearer(c1, third.data["access"])
    changed = c1.post(
        "/api/auth/password/change/",
        {"old_password": PASSWORD, "new_password": "Another-strong-password-314159"},
        format="json",
    )
    assert changed.status_code == 204
    assert c1.post("/api/auth/refresh/").status_code == 401
    assert c1.get("/api/auth/me/").status_code == 401


@pytest.mark.django_db
def test_login_failures_and_redis_unavailable(auth_user, settings):
    settings.AUTH_LOGIN_FAILURE_LIMIT = 2
    c = client()
    for expected in (401, 401, 429):
        assert login(c, auth_user, "wrong").status_code == expected
    assert login(c, auth_user).status_code == 429
    with patch("apps.identity.login_throttle._redis", side_effect=ConnectionError("down")):
        response = login(c, auth_user)
        assert response.status_code == 503


@pytest.mark.django_db
def test_csrf_required_for_cookie_endpoints(auth_user):
    c = client(csrf=True)
    assert login(c, auth_user).status_code == 200
    assert c.post("/api/auth/refresh/").status_code == 403
    assert c.post("/api/auth/logout/").status_code == 403
    csrf = c.get("/api/auth/csrf/")
    assert csrf.status_code == 200
    c.credentials(HTTP_X_CSRFTOKEN=csrf.data["csrf_token"])
    assert c.post("/api/auth/refresh/").status_code == 200
    assert c.post("/api/auth/logout/").status_code == 204


@pytest.mark.django_db
def test_malformed_missing_expired_refresh_and_foreign_session(auth_user):
    c = client()
    first = login(c, auth_user)
    bearer(c, first.data["access"])
    assert c.delete(f"/api/auth/sessions/{uuid.uuid4()}/").status_code == 404
    c.cookies[settings.AUTH_REFRESH_COOKIE_NAME] = "malformed"
    assert c.post("/api/auth/refresh/").status_code == 401
    c.cookies[settings.AUTH_REFRESH_COOKIE_NAME] = first.cookies[
        settings.AUTH_REFRESH_COOKIE_NAME
    ].value
    sid = first.cookies[settings.AUTH_REFRESH_COOKIE_NAME].value.split(".")[0]
    get_redis_connection("default").delete(f"auth:session:{sid}")
    assert c.post("/api/auth/refresh/").status_code == 401


@pytest.mark.django_db
def test_concurrent_rotation_allows_one_success_then_revokes(auth_user):
    response = login(client(), auth_user)
    sid_raw, secret = response.cookies[settings.AUTH_REFRESH_COOKIE_NAME].value.split(".")
    sid = uuid.UUID(sid_raw)
    row = get_session(sid)
    assert row is not None

    def attempt():
        try:
            return rotate_session(sid, secret, row)
        except APIError as exc:
            return exc.default_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: attempt(), range(2)))
    assert sum(isinstance(value, str) and value.startswith(sid_raw + ".") for value in results) == 1
    assert "REFRESH_REUSE_DETECTED" in results
    assert get_session(sid) is None


@pytest.mark.django_db
def test_cannot_revoke_another_users_session(auth_user):
    other = User.objects.create_user(f"other-{uuid.uuid4()}@example.com", PASSWORD)
    own, foreign = client(), client()
    own_login, foreign_login = login(own, auth_user), login(foreign, other)
    bearer(own, own_login.data["access"])
    foreign_sid = foreign_login.cookies[settings.AUTH_REFRESH_COOKIE_NAME].value.split(".")[0]
    assert own.delete(f"/api/auth/sessions/{foreign_sid}/").status_code == 404
    assert foreign.post("/api/auth/refresh/").status_code == 200
    redis = get_redis_connection("default")
    redis.delete(f"auth:session:{foreign_sid}", f"auth:user_sessions:{other.id}")
