import uuid
from collections.abc import Iterator
from contextlib import contextmanager

from django.db import connection, transaction


def _set_local(name: str, value: uuid.UUID | None) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT set_config(%s, %s, true)",
            [name, "" if value is None else str(value)],
        )


def set_local_context(
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID | None = None,
    service_id: uuid.UUID | None = None,
) -> None:
    if (user_id is None) == (service_id is None):
        raise ValueError("Exactly one of user_id or service_id is required")
    _set_local("app.tenant_id", tenant_id)
    _set_local("app.user_id", user_id)
    _set_local("app.service_id", service_id)


@contextmanager
def authorized_scope(
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID | None = None,
    service_id: uuid.UUID | None = None,
) -> Iterator[None]:
    if (user_id is None) == (service_id is None):
        raise ValueError("Exactly one of user_id or service_id is required")
    with transaction.atomic():
        set_local_context(
            tenant_id=tenant_id,
            user_id=user_id,
            service_id=service_id,
        )
        yield
