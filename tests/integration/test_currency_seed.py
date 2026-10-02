from importlib import import_module
from types import SimpleNamespace

import pytest
from django.db import connection


@pytest.mark.integration
@pytest.mark.django_db(transaction=True)
def test_iso_currency_seed_is_complete_and_preserves_existing_rows() -> None:
    with connection.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM erp.currencies")
        assert cursor.fetchone()[0] >= 165
        cursor.execute(
            "SELECT name,minor_units,is_active FROM erp.currencies WHERE code='PKR'"
        )
        assert cursor.fetchone() == ("Pakistan Rupee", 2, True)
        cursor.execute("SELECT minor_units FROM erp.currencies WHERE code='JPY'")
        assert cursor.fetchone()[0] == 0
        cursor.execute("SELECT minor_units FROM erp.currencies WHERE code='KWD'")
        assert cursor.fetchone()[0] == 3
        cursor.execute("SELECT minor_units FROM erp.currencies WHERE code='CLF'")
        assert cursor.fetchone()[0] == 4
        cursor.execute("UPDATE erp.currencies SET name='Local PKR name' WHERE code='PKR'")

    migration = import_module("apps.database.migrations.0045_seed_iso4217_currencies")
    migration.seed_currencies(None, SimpleNamespace(connection=connection))

    with connection.cursor() as cursor:
        cursor.execute("SELECT name FROM erp.currencies WHERE code='PKR'")
        assert cursor.fetchone()[0] == "Local PKR name"
        cursor.execute("SELECT count(*) FROM erp.currencies")
        assert cursor.fetchone()[0] >= 165
