"""JWT bearer authentication with user-revision invalidation."""

from typing import cast

from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.exceptions import AuthenticationFailed
from rest_framework_simplejwt.tokens import Token

from apps.identity.models import User


class RevisionJWTAuthentication(JWTAuthentication):
    def get_user(self, validated_token: Token) -> User:  # type: ignore[override]
        user = cast(User, super().get_user(validated_token))
        if validated_token.get("auth_revision") != user.auth_revision:
            raise AuthenticationFailed("The token has been revoked.", code="token_revoked")
        if not validated_token.get("sid") or validated_token.get("sub") != str(user.id):
            raise AuthenticationFailed("Invalid access session.", code="invalid_session")
        return user
