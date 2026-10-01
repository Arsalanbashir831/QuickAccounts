"""OpenAPI bearer declaration for the revision-aware SimpleJWT subclass."""

from typing import Any

from drf_spectacular.extensions import OpenApiAuthenticationExtension


class RevisionJWTAuthenticationScheme(OpenApiAuthenticationExtension):  # type: ignore[no-untyped-call]
    target_class = "apps.identity.jwt.RevisionJWTAuthentication"
    name = "BearerJWT"

    def get_security_definition(self, auto_schema: Any) -> dict[str, str]:
        return {"type": "http", "scheme": "bearer", "bearerFormat": "JWT"}
