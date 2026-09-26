from pathlib import Path

import pytest
from django.db import connection

from apps.accounting.models import (
    Account,
    AccountingPostingRule,
    ChartOfAccountTemplate,
    ChartOfAccountTemplateAccount,
    DimensionType,
    DimensionValue,
    DocumentSequence,
    FiscalPeriod,
    Journal,
    JournalEntry,
    JournalLine,
    JournalLineDimension,
)


@pytest.mark.integration
@pytest.mark.p0
@pytest.mark.django_db(transaction=True)
def test_schema_release_and_identity_user_exist() -> None:
    if connection.vendor != "postgresql":
        pytest.skip("The authoritative schema requires PostgreSQL 18")
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT to_regclass('identity.users'), to_regclass('platform.schema_releases')"
        )
        assert cursor.fetchone() == ("identity.users", "platform.schema_releases")


@pytest.mark.unit
def test_schema_sql_is_packaged_at_migration_expected_path() -> None:
    migration = Path(__file__).resolve().parents[2] / "apps/database/migrations"
    assert (migration.parents[2] / "erp-accounting-backend-schema.sql").is_file()


@pytest.mark.integration
@pytest.mark.p0
@pytest.mark.django_db(transaction=True)
def test_accounting_models_map_to_postgresql_schema() -> None:
    models = (
        Account,
        AccountingPostingRule,
        ChartOfAccountTemplate,
        ChartOfAccountTemplateAccount,
        DimensionType,
        DimensionValue,
        DocumentSequence,
        FiscalPeriod,
        Journal,
        JournalEntry,
        JournalLine,
        JournalLineDimension,
    )

    assert all(model._meta.managed is False for model in models)
    assert all(model._meta.db_table.startswith('"erp".') for model in models)
    assert DocumentSequence._meta.pk.field_names == (
        "company_id",
        "fiscal_period_id",
        "sequence_code",
    )
    assert JournalLineDimension._meta.pk.field_names == (
        "company_id",
        "journal_line_id",
        "dimension_type_id",
    )

    for model in models:
        model.objects.values_list("pk", flat=True).first()
