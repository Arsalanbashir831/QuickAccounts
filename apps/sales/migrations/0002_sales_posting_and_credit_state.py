from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("database", "0011_phase5_sales_posting_and_credits"),
        ("sales", "0001_initial"),
    ]
    operations = [
        migrations.AddField(
            model_name="salesinvoiceline",
            name="credit_of_invoice_line_id",
            field=models.UUIDField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="salesinvoiceline",
            name="revenue_account_id_snapshot",
            field=models.UUIDField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="salesinvoiceline",
            name="inventory_account_id_snapshot",
            field=models.UUIDField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="salesinvoiceline",
            name="cogs_account_id_snapshot",
            field=models.UUIDField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="salesinvoicetaxcomponent",
            name="output_tax_account_id_snapshot",
            field=models.UUIDField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="salesinvoicetaxcomponent",
            name="account_role_snapshot",
            field=models.TextField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="salesinvoicetaxcomponent",
            name="polarity_snapshot",
            field=models.TextField(blank=True, null=True),
        ),
    ]
