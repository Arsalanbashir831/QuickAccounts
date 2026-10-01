from django.core.exceptions import ImproperlyConfigured

from config.settings.base import *  # noqa: F403

if not env("JWT_SIGNING_KEY", default="") or len(env("JWT_SIGNING_KEY")) < 40:  # noqa: F405
    raise ImproperlyConfigured("JWT_SIGNING_KEY must be configured for VPS settings.")

SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SESSION_COOKIE_HTTPONLY = True
CSRF_COOKIE_HTTPONLY = True
