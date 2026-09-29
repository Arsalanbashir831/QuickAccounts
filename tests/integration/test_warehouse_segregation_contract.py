import pytest
from django.db import connection


@pytest.mark.integration
@pytest.mark.django_db(transaction=True)
def test_warehouse_segregation_contract() -> None:
    # Later retained stock/credit history intentionally forbids reversing all
    # migrations to 0015. Check the installed warehouse contract in place.
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema='erp' AND table_name='warehouses' "
            "AND column_name IN ('stock_category','row_version')"
        )
        assert {row[0] for row in cursor.fetchall()} == {"stock_category", "row_version"}
        cursor.execute(
            "SELECT tgname FROM pg_trigger WHERE NOT tgisinternal AND tgname IN "
            "('trg_warehouse_configuration','trg_warehouse_audit',"
            "'trg_stock_warehouse','trg_reservation_warehouse')"
        )
        assert len(cursor.fetchall()) == 4
