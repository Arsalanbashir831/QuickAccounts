from django.db import migrations, models
from django.db.models.expressions import RawSQL


class Migration(migrations.Migration):
    initial = True
    dependencies = [("database", "0010_phase5_sales_invoice_drafts")]
    operations = [
        migrations.CreateModel(
            name="SalesInvoice",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        primary_key=True,
                        serialize=False,
                        editable=False,
                        db_default=RawSQL("uuidv7()", []),
                    ),
                ),
                ("company_id", models.UUIDField()),
                ("invoice_no", models.TextField()),
                ("document_kind", models.TextField(db_default="invoice")),
                ("partner_id", models.UUIDField(blank=True, null=True)),
                ("sales_order_id", models.UUIDField(blank=True, null=True)),
                ("journal_entry_id", models.UUIDField(blank=True, null=True)),
                ("issue_date", models.DateField()),
                ("tax_point_date", models.DateField()),
                ("tax_jurisdiction_id", models.UUIDField(blank=True, null=True)),
                ("due_date", models.DateField(blank=True, null=True)),
                ("currency_code", models.CharField(max_length=3)),
                (
                    "exchange_rate",
                    models.DecimalField(max_digits=20, decimal_places=10, db_default=1),
                ),
                ("status", models.TextField(db_default="draft")),
                ("row_version", models.BigIntegerField(db_default=1)),
                ("calculated_at", models.DateTimeField(blank=True, null=True)),
                ("credit_of_invoice_id", models.UUIDField(blank=True, null=True)),
                (
                    "created_at",
                    models.DateTimeField(
                        editable=False, db_default=RawSQL("clock_timestamp()", [])
                    ),
                ),
                (
                    "updated_at",
                    models.DateTimeField(
                        editable=False, db_default=RawSQL("clock_timestamp()", [])
                    ),
                ),
                ("posted_at", models.DateTimeField(blank=True, null=True)),
            ],
            options={"managed": False, "db_table": '"erp"."sales_invoices"'},
        ),
        migrations.CreateModel(
            name="SalesInvoiceAddressSnapshot",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        primary_key=True,
                        serialize=False,
                        editable=False,
                        db_default=RawSQL("uuidv7()", []),
                    ),
                ),
                ("company_id", models.UUIDField()),
                ("sales_invoice_id", models.UUIDField()),
                ("address_kind", models.TextField()),
                ("recipient_name", models.TextField()),
                ("tax_registration_no", models.TextField(blank=True, null=True)),
                ("line_1", models.TextField()),
                ("line_2", models.TextField(blank=True, null=True)),
                ("city", models.TextField(blank=True, null=True)),
                ("region", models.TextField(blank=True, null=True)),
                ("postal_code", models.TextField(blank=True, null=True)),
                ("country_code", models.CharField(max_length=2, blank=True, null=True)),
            ],
            options={"managed": False, "db_table": '"erp"."sales_invoice_address_snapshots"'},
        ),
        migrations.CreateModel(
            name="SalesInvoiceLine",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        primary_key=True,
                        serialize=False,
                        editable=False,
                        db_default=RawSQL("uuidv7()", []),
                    ),
                ),
                ("company_id", models.UUIDField()),
                ("sales_invoice_id", models.UUIDField()),
                ("line_no", models.IntegerField()),
                ("item_id", models.UUIDField(blank=True, null=True)),
                ("line_account_id", models.UUIDField(blank=True, null=True)),
                ("description", models.TextField()),
                ("quantity", models.DecimalField(max_digits=20, decimal_places=6)),
                ("unit_price", models.DecimalField(max_digits=20, decimal_places=6)),
                (
                    "discount_amount",
                    models.DecimalField(max_digits=20, decimal_places=6, db_default=0),
                ),
                ("net_amount", models.DecimalField(max_digits=20, decimal_places=6)),
                ("tax_code_id", models.UUIDField(blank=True, null=True)),
                ("tax_amount", models.DecimalField(max_digits=20, decimal_places=6, db_default=0)),
                ("gross_amount", models.DecimalField(max_digits=20, decimal_places=6)),
                (
                    "created_at",
                    models.DateTimeField(
                        editable=False, db_default=RawSQL("clock_timestamp()", [])
                    ),
                ),
            ],
            options={"managed": False, "db_table": '"erp"."sales_invoice_lines"'},
        ),
        migrations.CreateModel(
            name="SalesInvoiceTaxComponent",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        primary_key=True,
                        serialize=False,
                        editable=False,
                        db_default=RawSQL("uuidv7()", []),
                    ),
                ),
                ("company_id", models.UUIDField()),
                ("sales_invoice_id", models.UUIDField()),
                ("sales_invoice_line_id", models.UUIDField()),
                ("tax_code_id", models.UUIDField()),
                ("tax_code_component_id", models.UUIDField()),
                ("tax_jurisdiction_id", models.UUIDField()),
                ("rate_schedule_id", models.UUIDField()),
                ("tax_rate_version_id", models.UUIDField()),
                ("taxable_base_amount", models.DecimalField(max_digits=20, decimal_places=6)),
                ("rate_snapshot", models.DecimalField(max_digits=12, decimal_places=8)),
                ("tax_inclusive_snapshot", models.BooleanField()),
                ("tax_amount", models.DecimalField(max_digits=20, decimal_places=6)),
                ("rate_version_code_snapshot", models.TextField()),
                ("calculation_method_snapshot", models.TextField()),
                ("calculation_base_snapshot", models.TextField()),
                (
                    "fixed_amount_snapshot",
                    models.DecimalField(max_digits=20, decimal_places=6, null=True),
                ),
                ("recovery_percent_snapshot", models.DecimalField(max_digits=9, decimal_places=6)),
                ("rounding_method_snapshot", models.TextField()),
                ("rounding_precision_snapshot", models.SmallIntegerField()),
                ("currency_code_snapshot", models.CharField(max_length=3)),
                (
                    "created_at",
                    models.DateTimeField(
                        editable=False, db_default=RawSQL("clock_timestamp()", [])
                    ),
                ),
            ],
            options={"managed": False, "db_table": '"erp"."sales_invoice_tax_components"'},
        ),
    ]
