import hashlib
from pathlib import Path

from django.db import migrations

EXPECTED_SHA256 = "836f1907f1617b9cd285d79b43e6a82a0e54b0ef9f66f20c3a479632451c8fb8"


def install_schema(apps, schema_editor):  # type: ignore[no-untyped-def]
    source = Path(__file__).resolve().parents[3] / "erp-accounting-backend-schema.sql"
    payload = source.read_bytes()
    actual = hashlib.sha256(payload).hexdigest()
    if actual != EXPECTED_SHA256:
        raise RuntimeError(
            "The authoritative schema changed; review the contract and create a new migration "
            f"(expected {EXPECTED_SHA256}, got {actual})."
        )

    statements = []
    for line in payload.decode("utf-8").splitlines():
        if line.strip() in {"BEGIN;", "COMMIT;"}:
            continue
        statements.append(line)
    # Bypass SchemaEditor's parameter composer: the authoritative SQL contains
    # PostgreSQL modulo operators and RAISE format strings with literal `%`.
    with schema_editor.connection.cursor() as cursor:
        cursor.execute("\n".join(statements))


class Migration(migrations.Migration):
    atomic = False
    initial = True
    dependencies = []
    operations = [migrations.RunPython(install_schema, migrations.RunPython.noop)]
