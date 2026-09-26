import pytest
from django.db import connection


@pytest.mark.integration
@pytest.mark.p0
@pytest.mark.django_db(transaction=True)
def test_phase5_partner_schema_contract() -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT column_default, is_nullable
            FROM information_schema.columns
            WHERE table_schema = 'erp'
              AND table_name = 'business_partners'
              AND column_name = 'row_version'
            """
        )
        row_version = cursor.fetchone()
        cursor.execute(
            """
            SELECT tgname
            FROM pg_trigger
            WHERE tgrelid = 'erp.business_partners'::regclass
              AND NOT tgisinternal
            ORDER BY tgname
            """
        )
        triggers = {row[0] for row in cursor.fetchall()}
        cursor.execute(
            "SELECT code FROM identity.permissions WHERE code LIKE 'party.%' ORDER BY code"
        )
        permissions = [row[0] for row in cursor.fetchall()]
        cursor.execute(
            """
            SELECT prosecdef
            FROM pg_proc
            WHERE oid = 'erp.audit_business_partner_change()'::regprocedure
            """
        )
        audit_is_security_definer = cursor.fetchone()[0]

    assert row_version is not None
    assert row_version[0] == "1"
    assert row_version[1] == "NO"
    assert "trg_business_partner_version" in triggers
    assert "trg_business_partner_audit" in triggers
    assert permissions == ["party.manage", "party.view"]
    assert audit_is_security_definer is True


@pytest.mark.integration
@pytest.mark.p0
@pytest.mark.django_db(transaction=True)
def test_phase5_partner_detail_schema_contract() -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT column_name, column_default, is_nullable
            FROM information_schema.columns
            WHERE table_schema = 'erp'
              AND table_name = 'partner_addresses'
              AND column_name IN ('row_version', 'updated_at')
            ORDER BY column_name
            """
        )
        columns = {row[0]: row[1:] for row in cursor.fetchall()}
        cursor.execute(
            """
            SELECT indexname
            FROM pg_indexes
            WHERE schemaname = 'erp' AND tablename = 'partner_addresses'
            """
        )
        indexes = {row[0] for row in cursor.fetchall()}
        cursor.execute(
            """
            SELECT tgname
            FROM pg_trigger
            WHERE tgrelid IN (
                'erp.partner_addresses'::regclass,
                'erp.partner_tax_registrations'::regclass
            ) AND NOT tgisinternal
            """
        )
        triggers = {row[0] for row in cursor.fetchall()}
        cursor.execute(
            """
            SELECT prosecdef
            FROM pg_proc
            WHERE oid = 'erp.audit_partner_detail_change()'::regprocedure
            """
        )
        audit_is_security_definer = cursor.fetchone()[0]

    assert columns["row_version"] == ("1", "NO")
    assert columns["updated_at"][1] == "NO"
    assert "ux_partner_addresses_default_kind" in indexes
    assert "trg_partner_address_version" in triggers
    assert "trg_partner_address_audit" in triggers
    assert "trg_partner_tax_registration_audit" in triggers
    assert audit_is_security_definer is True
