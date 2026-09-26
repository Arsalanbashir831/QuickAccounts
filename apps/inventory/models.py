from django.db import models
from django.db.models.expressions import RawSQL


class UnitOfMeasureCategory(models.Model):
    id = models.UUIDField(
        primary_key=True,
        db_default=RawSQL("uuidv7()", []),
        editable=False,
    )
    code = models.TextField(unique=True)
    name = models.TextField()

    class Meta:
        managed = False
        db_table = '"erp"."uom_categories"'

    def __str__(self) -> str:
        return f"{self.code} — {self.name}"


class UnitOfMeasure(models.Model):
    id = models.UUIDField(
        primary_key=True,
        db_default=RawSQL("uuidv7()", []),
        editable=False,
    )
    category_id = models.UUIDField()
    code = models.TextField(unique=True)
    name = models.TextField()
    to_base_factor = models.DecimalField(max_digits=20, decimal_places=10)

    class Meta:
        managed = False
        db_table = '"erp"."uoms"'

    def __str__(self) -> str:
        return f"{self.code} — {self.name}"


class Item(models.Model):
    class ItemKind(models.TextChoices):
        STOCK = "stock", "Stock"
        SERVICE = "service", "Service"
        NON_STOCK = "non_stock", "Non-stock"

    id = models.UUIDField(
        primary_key=True,
        db_default=RawSQL("uuidv7()", []),
        editable=False,
    )
    company_id = models.UUIDField()
    sku = models.TextField()
    name = models.TextField()
    item_kind = models.TextField(choices=ItemKind)
    base_uom_id = models.UUIDField()
    track_lots = models.BooleanField(db_default=False)
    track_serials = models.BooleanField(db_default=False)
    is_active = models.BooleanField(db_default=True)
    row_version = models.BigIntegerField(db_default=1)
    created_at = models.DateTimeField(db_default=RawSQL("clock_timestamp()", []), editable=False)
    updated_at = models.DateTimeField(db_default=RawSQL("clock_timestamp()", []), editable=False)

    class Meta:
        managed = False
        db_table = '"erp"."items"'
        unique_together = (("company_id", "sku"), ("company_id", "id"))

    def __str__(self) -> str:
        return f"{self.sku} — {self.name}"


class ItemAccountingProfile(models.Model):
    id = models.UUIDField(
        primary_key=True,
        db_default=RawSQL("uuidv7()", []),
        editable=False,
    )
    company_id = models.UUIDField()
    item_id = models.UUIDField()
    inventory_account_id = models.UUIDField(blank=True, null=True)
    revenue_account_id = models.UUIDField(blank=True, null=True)
    cogs_account_id = models.UUIDField(blank=True, null=True)
    purchase_account_id = models.UUIDField(blank=True, null=True)
    created_at = models.DateTimeField(db_default=RawSQL("clock_timestamp()", []), editable=False)
    updated_at = models.DateTimeField(db_default=RawSQL("clock_timestamp()", []), editable=False)

    class Meta:
        managed = False
        db_table = '"erp"."item_accounting_profiles"'
        unique_together = (("company_id", "item_id"),)

    def __str__(self) -> str:
        return f"Accounting profile for {self.item_id}"
