from django.db import models

from apps.accounting.models.base import clock_timestamp_default, uuidv7_default


class Account(models.Model):
    class AccountType(models.TextChoices):
        ASSET = "asset", "Asset"
        LIABILITY = "liability", "Liability"
        EQUITY = "equity", "Equity"
        REVENUE = "revenue", "Revenue"
        EXPENSE = "expense", "Expense"
        COST_OF_SALES = "cost_of_sales", "Cost of sales"
        RECEIVABLE = "receivable", "Receivable"
        PAYABLE = "payable", "Payable"

    class NormalBalance(models.TextChoices):
        DEBIT = "debit", "Debit"
        CREDIT = "credit", "Credit"

    id = models.UUIDField(primary_key=True, db_default=uuidv7_default(), editable=False)
    company_id = models.UUIDField()
    parent_account_id = models.UUIDField(null=True, blank=True)
    code = models.TextField()
    name = models.TextField()
    account_type = models.TextField(choices=AccountType)
    normal_balance = models.TextField(choices=NormalBalance)
    is_control_account = models.BooleanField(db_default=False)
    allow_posting = models.BooleanField(db_default=True)
    is_active = models.BooleanField(db_default=True)
    created_at = models.DateTimeField(db_default=clock_timestamp_default(), editable=False)
    updated_at = models.DateTimeField(db_default=clock_timestamp_default(), editable=False)

    class Meta:
        managed = False
        db_table = '"erp"."accounts"'
        unique_together = (("company_id", "code"), ("company_id", "id"))

    def __str__(self) -> str:
        return f"{self.code} — {self.name}"


class Journal(models.Model):
    class JournalType(models.TextChoices):
        GENERAL = "general", "General"
        SALES = "sales", "Sales"
        PURCHASE = "purchase", "Purchase"
        CASH = "cash", "Cash"
        BANK = "bank", "Bank"
        INVENTORY = "inventory", "Inventory"
        MANUFACTURING = "manufacturing", "Manufacturing"

    id = models.UUIDField(primary_key=True, db_default=uuidv7_default(), editable=False)
    company_id = models.UUIDField()
    code = models.TextField()
    name = models.TextField()
    journal_type = models.TextField(choices=JournalType)
    is_active = models.BooleanField(db_default=True)
    created_at = models.DateTimeField(db_default=clock_timestamp_default(), editable=False)
    updated_at = models.DateTimeField(db_default=clock_timestamp_default(), editable=False)

    class Meta:
        managed = False
        db_table = '"erp"."journals"'
        unique_together = (("company_id", "code"), ("company_id", "id"))

    def __str__(self) -> str:
        return f"{self.code} — {self.name}"


class AccountingPostingRule(models.Model):
    id = models.UUIDField(primary_key=True, db_default=uuidv7_default(), editable=False)
    company_id = models.UUIDField()
    event_code = models.TextField()
    role_code = models.TextField()
    account_id = models.UUIDField()
    created_at = models.DateTimeField(db_default=clock_timestamp_default(), editable=False)
    updated_at = models.DateTimeField(db_default=clock_timestamp_default(), editable=False)

    class Meta:
        managed = False
        db_table = '"erp"."accounting_posting_rules"'
        unique_together = (("company_id", "event_code", "role_code"),)

    def __str__(self) -> str:
        return f"{self.event_code}:{self.role_code}"


class DimensionType(models.Model):
    id = models.UUIDField(primary_key=True, db_default=uuidv7_default(), editable=False)
    company_id = models.UUIDField()
    code = models.TextField()
    name = models.TextField()
    is_required = models.BooleanField(db_default=False)
    is_active = models.BooleanField(db_default=True)

    class Meta:
        managed = False
        db_table = '"erp"."dimension_types"'
        unique_together = (("company_id", "code"), ("company_id", "id"))

    def __str__(self) -> str:
        return f"{self.code} — {self.name}"


class DimensionValue(models.Model):
    id = models.UUIDField(primary_key=True, db_default=uuidv7_default(), editable=False)
    company_id = models.UUIDField()
    dimension_type_id = models.UUIDField()
    code = models.TextField()
    name = models.TextField()
    is_active = models.BooleanField(db_default=True)

    class Meta:
        managed = False
        db_table = '"erp"."dimension_values"'
        unique_together = (
            ("company_id", "dimension_type_id", "code"),
            ("company_id", "dimension_type_id", "id"),
        )

    def __str__(self) -> str:
        return f"{self.code} — {self.name}"
