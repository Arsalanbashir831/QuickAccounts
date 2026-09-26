from django.db import migrations, models
from django.db.models.expressions import RawSQL


class Migration(migrations.Migration):
    dependencies = [
        ("accounting", "0001_accounting_model_state"),
        ("database", "0005_business_chart_templates"),
    ]
    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[],
            state_operations=[
                migrations.AddField(
                    model_name="account",
                    name="source_template_code",
                    field=models.TextField(blank=True, null=True),
                ),
                migrations.AddField(
                    model_name="account",
                    name="source_template_account_code",
                    field=models.TextField(blank=True, null=True),
                ),
                migrations.CreateModel(
                    name="ChartOfAccountTemplate",
                    fields=[
                        ("code", models.TextField(primary_key=True, serialize=False)),
                        ("name", models.TextField()),
                        (
                            "business_profile",
                            models.TextField(
                                choices=[
                                    ("retail_wholesale", "Retail and wholesale"),
                                    ("ecommerce", "E-commerce"),
                                    ("manufacturing", "Manufacturing"),
                                ]
                            ),
                        ),
                        ("version", models.PositiveIntegerField()),
                        ("is_active", models.BooleanField(db_default=True)),
                        (
                            "created_at",
                            models.DateTimeField(
                                db_default=RawSQL("clock_timestamp()", []), editable=False
                            ),
                        ),
                    ],
                    options={
                        "db_table": '"erp"."chart_of_account_templates"',
                        "managed": False,
                        "unique_together": {
                            ("business_profile", "version"),
                            ("code", "version"),
                        },
                    },
                ),
                migrations.CreateModel(
                    name="ChartOfAccountTemplateAccount",
                    fields=[
                        (
                            "pk",
                            models.CompositePrimaryKey(
                                "template_code",
                                "code",
                                blank=True,
                                editable=False,
                                primary_key=True,
                                serialize=False,
                            ),
                        ),
                        ("template_code", models.TextField()),
                        ("code", models.TextField()),
                        ("parent_code", models.TextField(blank=True, null=True)),
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
                            models.TextField(
                                choices=[("debit", "Debit"), ("credit", "Credit")]
                            ),
                        ),
                        ("is_control_account", models.BooleanField(db_default=False)),
                        ("allow_posting", models.BooleanField(db_default=True)),
                        ("sort_order", models.PositiveIntegerField()),
                    ],
                    options={
                        "db_table": '"erp"."chart_of_account_template_accounts"',
                        "managed": False,
                    },
                ),
            ],
        )
    ]
