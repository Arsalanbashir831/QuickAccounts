from django.db import migrations, models
from django.db.models.expressions import RawSQL


class Migration(migrations.Migration):
    dependencies = [
        ("database", "0008_phase5_partner_details"),
        ("parties", "0001_initial"),
    ]
    operations = [
        migrations.CreateModel(
            name="PartnerAddress",
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
                ("partner_id", models.UUIDField()),
                (
                    "address_kind",
                    models.TextField(
                        choices=[
                            ("billing", "Billing"),
                            ("shipping", "Shipping"),
                            ("registered", "Registered"),
                            ("other", "Other"),
                        ]
                    ),
                ),
                ("line_1", models.TextField()),
                ("line_2", models.TextField(blank=True, null=True)),
                ("city", models.TextField(blank=True, null=True)),
                ("region", models.TextField(blank=True, null=True)),
                ("postal_code", models.TextField(blank=True, null=True)),
                ("country_code", models.CharField(blank=True, max_length=2, null=True)),
                ("is_default", models.BooleanField(db_default=False)),
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
            options={"db_table": '"erp"."partner_addresses"', "managed": False},
        ),
        migrations.CreateModel(
            name="PartnerTaxRegistration",
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
                ("partner_id", models.UUIDField()),
                ("jurisdiction_id", models.UUIDField()),
                ("tax_type_id", models.UUIDField()),
                ("registration_number", models.TextField()),
                ("valid_from", models.DateField()),
                ("valid_to", models.DateField(blank=True, null=True)),
                ("is_verified", models.BooleanField(db_default=False)),
                ("is_active", models.BooleanField(db_default=True)),
                (
                    "created_at",
                    models.DateTimeField(
                        db_default=RawSQL("clock_timestamp()", []), editable=False
                    ),
                ),
            ],
            options={
                "db_table": '"erp"."partner_tax_registrations"',
                "managed": False,
                "unique_together": {
                    (
                        "company_id",
                        "partner_id",
                        "jurisdiction_id",
                        "tax_type_id",
                        "registration_number",
                    ),
                    ("company_id", "id"),
                },
            },
        ),
    ]
