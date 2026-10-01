"""Short-lived access tokens; refresh credentials are never JWTs."""

from django.conf import settings
from rest_framework_simplejwt.tokens import AccessToken

from apps.identity.models import User


def access_token(user: User, session_id: str) -> dict[str, str | int]:
    token = AccessToken.for_user(user)
    token["sub"] = str(user.id)
    token["sid"] = session_id
    token["auth_revision"] = user.auth_revision
    return {
        "access": str(token),
        "token_type": "Bearer",
        "expires_in": int(settings.SIMPLE_JWT["ACCESS_TOKEN_LIFETIME"].total_seconds()),
    }
