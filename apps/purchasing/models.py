from django.db import models
from django.db.models.expressions import RawSQL


class PurchaseBill(models.Model):
    id = models.UUIDField(primary_key=True, db_default=RawSQL("uuidv7()", []), editable=False)
    company_id = models.UUIDField()
    bill_no = models.TextField()
    document_kind = models.TextField(db_default="bill")
    supplier_id = models.UUIDField()
    purchase_order_id = models.UUIDField(blank=True, null=True)
    journal_entry_id = models.UUIDField(blank=True, null=True)
    bill_date = models.DateField()
    tax_point_date = models.DateField()
    tax_jurisdiction_id = models.UUIDField(blank=True, null=True)
    due_date = models.DateField(blank=True, null=True)
    currency_code = models.CharField(max_length=3)
    exchange_rate = models.DecimalField(max_digits=20, decimal_places=10, db_default=1)
    status = models.TextField(db_default="draft")
    row_version = models.BigIntegerField(db_default=1)
    calculated_at = models.DateTimeField(blank=True, null=True)
    credit_of_bill_id = models.UUIDField(blank=True, null=True)
    created_at = models.DateTimeField(db_default=RawSQL("clock_timestamp()", []), editable=False)
    updated_at = models.DateTimeField(db_default=RawSQL("clock_timestamp()", []), editable=False)
    posted_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        managed = False
        db_table = '"erp"."purchase_bills"'
        unique_together = (("company_id", "bill_no"), ("company_id", "id"))

    def __str__(self) -> str:
        return self.bill_no


class PurchaseBillAddressSnapshot(models.Model):
    id = models.UUIDField(primary_key=True, db_default=RawSQL("uuidv7()", []), editable=False)
    company_id = models.UUIDField()
    purchase_bill_id = models.UUIDField()
    address_kind = models.TextField()
    recipient_name = models.TextField()
    tax_registration_no = models.TextField(  # noqa: DJ001 -- adopted SQL is nullable
        blank=True, null=True
    )
    line_1 = models.TextField()
    line_2 = models.TextField(blank=True, null=True)  # noqa: DJ001 -- adopted SQL
    city = models.TextField(blank=True, null=True)  # noqa: DJ001 -- adopted SQL
    region = models.TextField(blank=True, null=True)  # noqa: DJ001 -- adopted SQL
    postal_code = models.TextField(blank=True, null=True)  # noqa: DJ001 -- adopted SQL
    country_code = models.CharField(  # noqa: DJ001 -- adopted SQL is nullable
        max_length=2, blank=True, null=True
    )

    class Meta:
        managed = False
        db_table = '"erp"."purchase_bill_address_snapshots"'
        unique_together = (("company_id", "purchase_bill_id", "address_kind"),)

    def __str__(self) -> str:
        return f"{self.address_kind} — {self.recipient_name}"


class PurchaseBillLine(models.Model):
    id = models.UUIDField(primary_key=True, db_default=RawSQL("uuidv7()", []), editable=False)
    company_id = models.UUIDField()
    purchase_bill_id = models.UUIDField()
    line_no = models.IntegerField()
    item_id = models.UUIDField(blank=True, null=True)
    line_account_id = models.UUIDField(blank=True, null=True)
    description = models.TextField()
    quantity = models.DecimalField(max_digits=20, decimal_places=6)
    unit_cost = models.DecimalField(max_digits=20, decimal_places=6)
    net_amount = models.DecimalField(max_digits=20, decimal_places=6)
    tax_code_id = models.UUIDField(blank=True, null=True)
    credit_of_bill_line_id = models.UUIDField(blank=True, null=True)
    purchase_account_id_snapshot = models.UUIDField(blank=True, null=True)
    inventory_account_id_snapshot = models.UUIDField(blank=True, null=True)
    tax_amount = models.DecimalField(max_digits=20, decimal_places=6, db_default=0)
    gross_amount = models.DecimalField(max_digits=20, decimal_places=6)
    created_at = models.DateTimeField(db_default=RawSQL("clock_timestamp()", []), editable=False)

    class Meta:
        managed = False
        db_table = '"erp"."purchase_bill_lines"'
        unique_together = (("company_id", "purchase_bill_id", "line_no"),)

    def __str__(self) -> str:
        return f"Bill {self.purchase_bill_id} line {self.line_no}"


class PurchaseBillTaxComponent(models.Model):
    id = models.UUIDField(primary_key=True, db_default=RawSQL("uuidv7()", []), editable=False)
    company_id = models.UUIDField()
    purchase_bill_id = models.UUIDField()
    purchase_bill_line_id = models.UUIDField()
    tax_code_id = models.UUIDField()
    tax_code_component_id = models.UUIDField()
    tax_jurisdiction_id = models.UUIDField()
    rate_schedule_id = models.UUIDField()
    tax_rate_version_id = models.UUIDField()
    taxable_base_amount = models.DecimalField(max_digits=20, decimal_places=6)
    rate_snapshot = models.DecimalField(max_digits=12, decimal_places=8)
    tax_inclusive_snapshot = models.BooleanField()
    tax_amount = models.DecimalField(max_digits=20, decimal_places=6)
    recoverable_amount = models.DecimalField(max_digits=20, decimal_places=6, db_default=0)
    rate_version_code_snapshot = models.TextField()
    calculation_method_snapshot = models.TextField()
    calculation_base_snapshot = models.TextField()
    fixed_amount_snapshot = models.DecimalField(max_digits=20, decimal_places=6, null=True)
    recovery_percent_snapshot = models.DecimalField(max_digits=9, decimal_places=6)
    rounding_method_snapshot = models.TextField()
    rounding_precision_snapshot = models.SmallIntegerField()
    currency_code_snapshot = models.CharField(max_length=3)
    input_tax_account_id_snapshot = models.UUIDField(blank=True, null=True)
    account_role_snapshot = models.TextField(blank=True, null=True)  # noqa: DJ001
    polarity_snapshot = models.TextField(blank=True, null=True)  # noqa: DJ001
    created_at = models.DateTimeField(db_default=RawSQL("clock_timestamp()", []), editable=False)

    class Meta:
        managed = False
        db_table = '"erp"."purchase_bill_tax_components"'

    def __str__(self) -> str:
        return f"Tax component {self.id}"
