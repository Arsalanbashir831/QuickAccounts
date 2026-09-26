from django.db import migrations, models
from django.db.models.expressions import RawSQL


class Migration(migrations.Migration):
    initial = True
    dependencies = [("database", "0007_phase5_party_foundation")]
    operations = [
        migrations.CreateModel(
            name="BusinessPartner",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        db_default=RawSQL("uuidv7()", []),
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                ("company_id", models.UUIDField()),
                ("partner_code", models.TextField()),
                ("display_name", models.TextField()),
                ("legal_name", models.TextField(blank=True, null=True)),
                (
                    "partner_kind",
                    models.TextField(
                        choices=[
                            ("customer", "Customer"),
                            ("supplier", "Supplier"),
                            ("both", "Both"),
                            ("other", "Other"),
                        ],
                        db_default="other",
                    ),
                ),
                ("email", models.TextField(blank=True, null=True)),
                ("phone", models.TextField(blank=True, null=True)),
                ("is_active", models.BooleanField(db_default=True)),
                ("row_version", models.BigIntegerField(db_default=1)),
                (
                    "created_at",
                    models.DateTimeField(
                        db_default=RawSQL("clock_timestamp()", []), editable=False
                    ),
                ),
                (
                    "updated_at",
                    models.DateTimeField(
                        db_default=RawSQL("clock_timestamp()", []), editable=False
                    ),
                ),
            ],
            options={
                "db_table": '"erp"."business_partners"',
                "managed": False,
                "unique_together": {
                    ("company_id", "partner_code"),
                    ("company_id", "id"),
                },
            },
        )
    ]
