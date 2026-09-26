import uuid
from unittest.mock import call, patch

import pytest

from common.db.context import authorized_scope


@pytest.mark.unit
def test_scope_requires_exactly_one_actor() -> None:
    tenant_id = uuid.uuid4()
    with pytest.raises(ValueError, match="Exactly one"):
        with authorized_scope(tenant_id=tenant_id):
            pass
    with pytest.raises(ValueError, match="Exactly one"):
        with authorized_scope(
            tenant_id=tenant_id,
            user_id=uuid.uuid4(),
            service_id=uuid.uuid4(),
        ):
            pass


@pytest.mark.unit
def test_scope_sets_transaction_local_identity_context() -> None:
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    with (
        patch("common.db.context.transaction.atomic"),
        patch("common.db.context._set_local") as set_local,
    ):
        with authorized_scope(tenant_id=tenant_id, user_id=user_id):
            pass
    assert set_local.call_args_list == [
        call("app.tenant_id", tenant_id),
        call("app.user_id", user_id),
        call("app.service_id", None),
    ]
