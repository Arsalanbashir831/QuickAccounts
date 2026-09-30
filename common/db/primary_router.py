"""Keep accounting and report ORM reads on the primary database.

The raw SQL selectors use Django's default connection directly. This router
also prevents a future read replica from silently serving ORM financial reads.
"""

from django.db.models import Model


class FinancialPrimaryRouter:
    financial_apps = frozenset({
        "accounting", "reporting", "sales", "purchasing", "payments",
        "inventory", "manufacturing", "taxation", "jobs", "audit",
    })

    def db_for_read(self, model: type[Model], **hints: object) -> str | None:
        if model._meta.app_label in self.financial_apps:
            return "default"
        return None

    def db_for_write(self, model: type[Model], **hints: object) -> str | None:
        if model._meta.app_label in self.financial_apps:
            return "default"
        return None
