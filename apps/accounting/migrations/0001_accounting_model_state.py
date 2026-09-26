from django.db import migrations, models
from django.db.models.expressions import RawSQL


class Migration(migrations.Migration):
    initial = True
    dependencies = [("database", "0003_fiscal_period_revisions")]
    operations = [
        migrations.CreateModel(
            name="FiscalPeriod",
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
                ("code", models.TextField()),
                ("starts_on", models.DateField()),
                ("ends_on", models.DateField()),
                (
                    "state",
                    models.TextField(
                        choices=[("open", "Open"), ("closed", "Closed"), ("locked", "Locked")],
                        db_default="open",
                    ),
                ),
                ("row_version", models.BigIntegerField(db_default=1)),
                (
                    "created_at",
                    models.DateTimeField(
                        db_default=RawSQL("clock_timestamp()", []), editable=False
                    ),
                ),
            ],
            options={
                "db_table": '"erp"."fiscal_periods"',
                "managed": False,
                "unique_together": {
                    ("company_id", "code"),
                    ("company_id", "id"),
                    ("company_id", "starts_on"),
                },
            },
        ),
        migrations.CreateModel(
            name="DocumentSequence",
            fields=[
                (
                    "pk",
                    models.CompositePrimaryKey(
                        "company_id",
                        "fiscal_period_id",
                        "sequence_code",
                        blank=True,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                ("company_id", models.UUIDField()),
                ("fiscal_period_id", models.UUIDField()),
                ("sequence_code", models.TextField()),
                ("prefix", models.TextField(db_default="")),
                ("next_value", models.BigIntegerField(db_default=1)),
                ("padding_length", models.SmallIntegerField(db_default=6)),
            ],
            options={"db_table": '"erp"."document_sequences"', "managed": False},
        ),
        migrations.CreateModel(
            name="Account",
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
                ("parent_account_id", models.UUIDField(blank=True, null=True)),
                ("code", models.TextField()),
                ("name", models.TextField()),
                (
                    "account_type",
                    models.TextField(
                        choices=[
                            ("asset", "Asset"),
                            ("liability", "Liability"),
                            ("equity", "Equity"),
                            ("revenue", "Revenue"),
                            ("expense", "Expense"),
                            ("cost_of_sales", "Cost of sales"),
                            ("receivable", "Receivable"),
                            ("payable", "Payable"),
                        ]
                    ),
                ),
                (
                    "normal_balance",
                    models.TextField(choices=[("debit", "Debit"), ("credit", "Credit")]),
                ),
                ("is_control_account", models.BooleanField(db_default=False)),
                ("allow_posting", models.BooleanField(db_default=True)),
                ("is_active", models.BooleanField(db_default=True)),
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
                "db_table": '"erp"."accounts"',
                "managed": False,
                "unique_together": {("company_id", "code"), ("company_id", "id")},
            },
        ),
        migrations.CreateModel(
            name="Journal",
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
                ("code", models.TextField()),
                ("name", models.TextField()),
                (
                    "journal_type",
                    models.TextField(
                        choices=[
                            ("general", "General"),
                            ("sales", "Sales"),
                            ("purchase", "Purchase"),
                            ("cash", "Cash"),
                            ("bank", "Bank"),
                            ("inventory", "Inventory"),
                            ("manufacturing", "Manufacturing"),
                        ]
                    ),
                ),
                ("is_active", models.BooleanField(db_default=True)),
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
                "db_table": '"erp"."journals"',
                "managed": False,
                "unique_together": {("company_id", "code"), ("company_id", "id")},
            },
        ),
        migrations.CreateModel(
            name="AccountingPostingRule",
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
                ("event_code", models.TextField()),
                ("role_code", models.TextField()),
                ("account_id", models.UUIDField()),
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
                "db_table": '"erp"."accounting_posting_rules"',
                "managed": False,
                "unique_together": {("company_id", "event_code", "role_code")},
            },
        ),
        migrations.CreateModel(
            name="DimensionType",
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
                ("code", models.TextField()),
                ("name", models.TextField()),
                ("is_required", models.BooleanField(db_default=False)),
                ("is_active", models.BooleanField(db_default=True)),
            ],
            options={
                "db_table": '"erp"."dimension_types"',
                "managed": False,
                "unique_together": {("company_id", "code"), ("company_id", "id")},
            },
        ),
        migrations.CreateModel(
            name="DimensionValue",
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
                ("dimension_type_id", models.UUIDField()),
                ("code", models.TextField()),
                ("name", models.TextField()),
                ("is_active", models.BooleanField(db_default=True)),
            ],
            options={
                "db_table": '"erp"."dimension_values"',
                "managed": False,
                "unique_together": {
                    ("company_id", "dimension_type_id", "code"),
                    ("company_id", "dimension_type_id", "id"),
                },
            },
        ),
        migrations.CreateModel(
            name="JournalEntry",
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
                ("journal_id", models.UUIDField()),
                ("fiscal_period_id", models.UUIDField()),
                ("entry_number", models.TextField()),
                ("entry_date", models.DateField()),
                (
                    "status",
                    models.TextField(
                        choices=[("draft", "Draft"), ("posted", "Posted"), ("void", "Void")],
                        db_default="draft",
                    ),
                ),
                ("description", models.TextField(blank=True, null=True)),
                ("source_type", models.TextField(blank=True, null=True)),
                ("source_id", models.UUIDField(blank=True, null=True)),
                ("idempotency_key", models.TextField(blank=True, null=True)),
                ("reversal_of_entry_id", models.UUIDField(blank=True, null=True)),
                ("created_by", models.UUIDField(blank=True, null=True)),
                (
                    "created_at",
                    models.DateTimeField(
                        db_default=RawSQL("clock_timestamp()", []), editable=False
                    ),
                ),
                ("posted_at", models.DateTimeField(blank=True, null=True)),
                ("row_version", models.BigIntegerField(db_default=1)),
            ],
            options={
                "db_table": '"erp"."journal_entries"',
                "managed": False,
                "unique_together": {
                    ("company_id", "id"),
                    ("company_id", "journal_id", "entry_number"),
                    ("company_id", "idempotency_key"),
                },
            },
        ),
        migrations.CreateModel(
            name="JournalLine",
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
                ("journal_entry_id", models.UUIDField()),
                ("line_no", models.IntegerField()),
                ("account_id", models.UUIDField()),
                ("business_partner_id", models.UUIDField(blank=True, null=True)),
                ("sales_channel_id", models.UUIDField(blank=True, null=True)),
                ("description", models.TextField(blank=True, null=True)),
                ("transaction_currency", models.CharField(max_length=3)),
                ("exchange_rate", models.DecimalField(decimal_places=10, max_digits=20)),
                (
                    "transaction_debit",
                    models.DecimalField(db_default=0, decimal_places=6, max_digits=20),
                ),
                (
                    "transaction_credit",
                    models.DecimalField(db_default=0, decimal_places=6, max_digits=20),
                ),
                (
                    "debit_amount",
                    models.DecimalField(db_default=0, decimal_places=6, max_digits=20),
                ),
                (
                    "credit_amount",
                    models.DecimalField(db_default=0, decimal_places=6, max_digits=20),
                ),
                (
                    "created_at",
                    models.DateTimeField(
                        db_default=RawSQL("clock_timestamp()", []), editable=False
                    ),
                ),
            ],
            options={
                "db_table": '"erp"."journal_lines"',
                "managed": False,
                "unique_together": {
                    ("company_id", "journal_entry_id", "line_no"),
                    ("company_id", "id"),
                },
            },
        ),
        migrations.CreateModel(
            name="JournalLineDimension",
            fields=[
                (
                    "pk",
                    models.CompositePrimaryKey(
                        "company_id",
                        "journal_line_id",
                        "dimension_type_id",
                        blank=True,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                ("company_id", models.UUIDField()),
                ("journal_line_id", models.UUIDField()),
                ("dimension_type_id", models.UUIDField()),
                ("dimension_value_id", models.UUIDField()),
            ],
            options={"db_table": '"erp"."journal_line_dimensions"', "managed": False},
        ),
    ]
