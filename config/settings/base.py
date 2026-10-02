from datetime import timedelta
from pathlib import Path

import environ

from common.api.schema_workflow import TAGS as WORKFLOW_TAGS

BASE_DIR = Path(__file__).resolve().parents[2]
env = environ.Env(
    DJANGO_DEBUG=(bool, False),
    DJANGO_ALLOWED_HOSTS=(list, ["localhost", "127.0.0.1"]),
)

SECRET_KEY = env("DJANGO_SECRET_KEY", default="unsafe-development-key-change-me")
DEBUG = env("DJANGO_DEBUG")
ALLOWED_HOSTS = env("DJANGO_ALLOWED_HOSTS")

DJANGO_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
]
THIRD_PARTY_APPS = ["rest_framework", "drf_spectacular"]
LOCAL_APPS = [
    "apps.database",
    "apps.identity",
    "apps.tenancy",
    "apps.module_access",
    "apps.licensing",
    "apps.accounting",
    "apps.parties",
    "apps.sales",
    "apps.purchasing",
    "apps.payments",
    "apps.inventory",
    "apps.manufacturing",
    "apps.taxation",
    "apps.integrations",
    "apps.reporting",
    "apps.audit",
    "apps.jobs",
]
INSTALLED_APPS = DJANGO_APPS + THIRD_PARTY_APPS + LOCAL_APPS

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "common.api.timing.RequestTimingMiddleware",
    "common.api.middleware.RequestIDMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"
AUTH_USER_MODEL = "identity.User"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ]
        },
    }
]

DATABASES = {"default": env.db("DATABASE_URL", default="postgresql://localhost/quickaccounts")}
if env("DATABASE_PLATFORM_URL", default=""):
    DATABASES["platform"] = env.db("DATABASE_PLATFORM_URL")
DATABASES["default"].update(
    {
        "CONN_MAX_AGE": 0,
        "ATOMIC_REQUESTS": False,
        "DISABLE_SERVER_SIDE_CURSORS": True,
    }
)
if "platform" in DATABASES:
    DATABASES["platform"].update(
        CONN_MAX_AGE=0, ATOMIC_REQUESTS=False, DISABLE_SERVER_SIDE_CURSORS=True
    )
DATABASE_ROUTERS = ["common.db.primary_router.FinancialPrimaryRouter"]

CACHES = {
    "default": {
        "BACKEND": "django_redis.cache.RedisCache",
        "LOCATION": env("REDIS_CACHE_URL", default="redis://localhost:6379/0"),
        "OPTIONS": {"CLIENT_CLASS": "django_redis.client.DefaultClient"},
    }
}
CELERY_BROKER_URL = env("REDIS_BROKER_URL", default="redis://localhost:6380/0")
CELERY_RESULT_BACKEND = env("CELERY_RESULT_BACKEND", default="redis://localhost:6380/1")
CELERY_TASK_TRACK_STARTED = True
CELERY_TASK_TIME_LIMIT = 300

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "apps.identity.jwt.RevisionJWTAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
    "EXCEPTION_HANDLER": "common.api.exceptions.api_exception_handler",
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
}
SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=10),
    "ALGORITHM": "HS256",
    "SIGNING_KEY": env("JWT_SIGNING_KEY", default=SECRET_KEY),
    "ISSUER": "quickaccounts",
    "AUDIENCE": "quickaccounts-api",
    "AUTH_HEADER_TYPES": ("Bearer",),
    "USER_ID_FIELD": "id",
    "USER_ID_CLAIM": "user_id",
    "CHECK_USER_IS_ACTIVE": True,
}
AUTH_REFRESH_SESSION_SECONDS = env.int("AUTH_REFRESH_SESSION_SECONDS", default=30 * 24 * 3600)
AUTH_REFRESH_COOKIE_NAME = env("AUTH_REFRESH_COOKIE_NAME", default="qa_refresh")
AUTH_REFRESH_COOKIE_PATH = env("AUTH_REFRESH_COOKIE_PATH", default="/api/")
AUTH_REFRESH_COOKIE_SECURE = env.bool("AUTH_REFRESH_COOKIE_SECURE", default=True)
AUTH_REFRESH_COOKIE_SAMESITE = env("AUTH_REFRESH_COOKIE_SAMESITE", default="Lax")
CSRF_TRUSTED_ORIGINS = env.list("DJANGO_CSRF_TRUSTED_ORIGINS", default=[])
AUTH_LOGIN_FAILURE_LIMIT = env.int("AUTH_LOGIN_FAILURE_LIMIT", default=5)
AUTH_LOGIN_FAILURE_WINDOW_SECONDS = env.int("AUTH_LOGIN_FAILURE_WINDOW_SECONDS", default=900)
SPECTACULAR_SETTINGS = {
    "TITLE": "QuickAccounts ERP Accounting API",
    "VERSION": "1.0.0",
    "DESCRIPTION": (
        "## API workflow\n\n"
        "1. **Platform onboarding (superadmin only):** provision a tenant, initial company, "
        "and owner at `/platform-api/v1/tenants`; add companies, create and publish a plan version, "
        "issue a license, then explicitly assign it. These private commands require "
        "`Idempotency-Key` and an audit reason. Business owners cannot register tenants "
        "or bind licenses.\n"
        "2. **Authenticate** at `/api/auth/`: obtain an access token, then send it as "
        "`Authorization: Bearer <token>`. Refresh and logout use the HttpOnly cookie and "
        "a CSRF token from `/api/auth/csrf/`.\n"
        "3. **Set up the workspace**: choose an assigned tenant/company, enable modules, "
        "then configure "
        "partners, items, warehouses, accounts, and posting rules.\n"
        "4. **Run business flows**: sales order → invoice draft → calculate → post → "
        "delivery; purchase bill draft → calculate → post; receive or move stock as needed.\n"
        "5. **Handle exceptions**: inspect returned goods → post the return → refund, "
        "replace, repair, restock, or dispose; allocate and post payments separately.\n"
        "6. **Close and review**: production execution, journal/period controls, "
        "reconciliations, and reports.\n\n"
        "Company-scoped routes use `/api/v1/tenants/{tenant_id}/companies/{company_id}/`. "
        "Posted financial documents are generally immutable; use linked corrections or "
        "reversals. Legacy auth aliases remain callable but are hidden from this reference."
    ),
    "TAGS": WORKFLOW_TAGS,
    "PREPROCESSING_HOOKS": ["common.api.schema_workflow.canonical_workflow_endpoints"],
    "POSTPROCESSING_HOOKS": [
        "drf_spectacular.hooks.postprocess_schema_enums",
        "common.api.schema_workflow.organize_workflow_schema",
    ],
    "SWAGGER_UI_SETTINGS": {
        "deepLinking": True,
        "docExpansion": "list",
        "displayRequestDuration": True,
        "filter": True,
    },
    "SERVE_INCLUDE_SCHEMA": False,
    "ENUM_NAME_OVERRIDES": {
        "PartnerAddressKindEnum": ["billing", "shipping", "registered", "other"],
        "InvoiceAddressKindEnum": ["billing", "shipping"],
    },
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]
LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True
STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
ERP_PRODUCT_CODE = env("ERP_PRODUCT_CODE", default="quickaccounts")
