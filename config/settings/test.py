from config.settings.base import *  # noqa: F403

# Test migrations and fixtures use one isolated database even if a developer's
# environment defines a separate platform connection for the running server.
DATABASES.pop("platform", None)  # noqa: F405
PLATFORM_ALLOW_DEFAULT_CONNECTION = True

# Existing domain fixtures use Django's test-only force_login helper. Production
# settings remain JWT-only; targeted JWT tests exercise bearer authentication.
REST_FRAMEWORK = {
    **REST_FRAMEWORK,  # noqa: F405
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "apps.identity.jwt.RevisionJWTAuthentication",
        "rest_framework.authentication.SessionAuthentication",
    ],
}

PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
CELERY_TASK_ALWAYS_EAGER = True
