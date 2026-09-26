from django.db import migrations, models
from django.db.models.expressions import RawSQL


class Migration(migrations.Migration):
    initial = True
    dependencies = [("database", "0009_phase5_item_catalog")]
    operations = [
        migrations.CreateModel(
            name="UnitOfMeasureCategory",
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
                ("code", models.TextField(unique=True)),
                ("name", models.TextField()),
            ],
            options={"db_table": '"erp"."uom_categories"', "managed": False},
        ),
        migrations.CreateModel(
            name="UnitOfMeasure",
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
                ("category_id", models.UUIDField()),
                ("code", models.TextField(unique=True)),
                ("name", models.TextField()),
                ("to_base_factor", models.DecimalField(decimal_places=10, max_digits=20)),
            ],
            options={"db_table": '"erp"."uoms"', "managed": False},
        ),
        migrations.CreateModel(
            name="Item",
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
                ("sku", models.TextField()),
                ("name", models.TextField()),
                (
                    "item_kind",
                    models.TextField(
                        choices=[
                            ("stock", "Stock"),
                            ("service", "Service"),
                            ("non_stock", "Non-stock"),
                        ]
                    ),
                ),
                ("base_uom_id", models.UUIDField()),
                ("track_lots", models.BooleanField(db_default=False)),
                ("track_serials", models.BooleanField(db_default=False)),
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
                "db_table": '"erp"."items"',
                "managed": False,
                "unique_together": {("company_id", "sku"), ("company_id", "id")},
            },
        ),
        migrations.CreateModel(
            name="ItemAccountingProfile",
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
                ("item_id", models.UUIDField()),
                ("inventory_account_id", models.UUIDField(blank=True, null=True)),
                ("revenue_account_id", models.UUIDField(blank=True, null=True)),
                ("cogs_account_id", models.UUIDField(blank=True, null=True)),
                ("purchase_account_id", models.UUIDField(blank=True, null=True)),
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
                "db_table": '"erp"."item_accounting_profiles"',
                "managed": False,
                "unique_together": {("company_id", "item_id")},
            },
        ),
    ]
