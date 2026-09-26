from django.db import models

from apps.accounting.models.base import clock_timestamp_default, uuidv7_default


class JournalEntry(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        POSTED = "posted", "Posted"
        VOID = "void", "Void"

    id = models.UUIDField(primary_key=True, db_default=uuidv7_default(), editable=False)
    company_id = models.UUIDField()
    journal_id = models.UUIDField()
    fiscal_period_id = models.UUIDField()
    entry_number = models.TextField()
    entry_date = models.DateField()
    status = models.TextField(choices=Status, db_default="draft")
    description = models.TextField(null=True, blank=True)  # noqa: DJ001 - mirrors PostgreSQL
    source_type = models.TextField(null=True, blank=True)  # noqa: DJ001 - mirrors PostgreSQL
    source_id = models.UUIDField(null=True, blank=True)
    idempotency_key = models.TextField(null=True, blank=True)  # noqa: DJ001 - mirrors PostgreSQL
    reversal_of_entry_id = models.UUIDField(null=True, blank=True)
    created_by = models.UUIDField(null=True, blank=True)
    created_at = models.DateTimeField(db_default=clock_timestamp_default(), editable=False)
    posted_at = models.DateTimeField(null=True, blank=True)
    row_version = models.BigIntegerField(db_default=1)

    class Meta:
        managed = False
        db_table = '"erp"."journal_entries"'
        unique_together = (
            ("company_id", "id"),
            ("company_id", "journal_id", "entry_number"),
            ("company_id", "idempotency_key"),
        )

    def __str__(self) -> str:
        return self.entry_number


class JournalLine(models.Model):
    id = models.UUIDField(primary_key=True, db_default=uuidv7_default(), editable=False)
    company_id = models.UUIDField()
    journal_entry_id = models.UUIDField()
    line_no = models.IntegerField()
    account_id = models.UUIDField()
    business_partner_id = models.UUIDField(null=True, blank=True)
    sales_channel_id = models.UUIDField(null=True, blank=True)
    description = models.TextField(null=True, blank=True)  # noqa: DJ001 - mirrors PostgreSQL
    transaction_currency = models.CharField(max_length=3)
    exchange_rate = models.DecimalField(max_digits=20, decimal_places=10)
    transaction_debit = models.DecimalField(max_digits=20, decimal_places=6, db_default=0)
    transaction_credit = models.DecimalField(max_digits=20, decimal_places=6, db_default=0)
    debit_amount = models.DecimalField(max_digits=20, decimal_places=6, db_default=0)
    credit_amount = models.DecimalField(max_digits=20, decimal_places=6, db_default=0)
    created_at = models.DateTimeField(db_default=clock_timestamp_default(), editable=False)

    class Meta:
        managed = False
        db_table = '"erp"."journal_lines"'
        unique_together = (("company_id", "journal_entry_id", "line_no"), ("company_id", "id"))

    def __str__(self) -> str:
        return f"{self.journal_entry_id}:{self.line_no}"


class JournalLineDimension(models.Model):
    pk = models.CompositePrimaryKey("company_id", "journal_line_id", "dimension_type_id")
    company_id = models.UUIDField()
    journal_line_id = models.UUIDField()
    dimension_type_id = models.UUIDField()
    dimension_value_id = models.UUIDField()

    class Meta:
        managed = False
        db_table = '"erp"."journal_line_dimensions"'

    def __str__(self) -> str:
        return f"{self.journal_line_id}:{self.dimension_type_id}"
