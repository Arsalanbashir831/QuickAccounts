from django.apps import AppConfig


class IdentityConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.identity"

    def ready(self) -> None:
        from apps.identity import schema  # noqa: F401
