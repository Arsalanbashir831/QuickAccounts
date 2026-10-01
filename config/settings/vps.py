from pathlib import Path

import environ
from django.core.exceptions import ImproperlyConfigured

local_env_file = Path(__file__).resolve().parents[2] / ".env"
if local_env_file.is_file():
    # Load before base settings read secrets; deployed process variables win.
    environ.Env.read_env(local_env_file, overwrite=False)

from config.settings.base import *  # noqa: E402,F403

if not env("JWT_SIGNING_KEY", default="") or len(env("JWT_SIGNING_KEY")) < 40:  # noqa: F405
    raise ImproperlyConfigured("JWT_SIGNING_KEY must be configured for VPS settings.")

SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SESSION_COOKIE_HTTPONLY = True
CSRF_COOKIE_HTTPONLY = True
