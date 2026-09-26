from django.db import models

from apps.accounting.models.base import clock_timestamp_default, uuidv7_default


class FiscalPeriod(models.Model):
    class State(models.TextChoices):
        OPEN = "open", "Open"
        CLOSED = "closed", "Closed"
        LOCKED = "locked", "Locked"

    id = models.UUIDField(primary_key=True, db_default=uuidv7_default(), editable=False)
    company_id = models.UUIDField()
    code = models.TextField()
    starts_on = models.DateField()
    ends_on = models.DateField()
    state = models.TextField(choices=State, db_default="open")
    row_version = models.BigIntegerField(db_default=1)
    created_at = models.DateTimeField(db_default=clock_timestamp_default(), editable=False)

    class Meta:
        managed = False
        db_table = '"erp"."fiscal_periods"'
        unique_together = (
            ("company_id", "code"),
            ("company_id", "id"),
            ("company_id", "starts_on"),
        )

    def __str__(self) -> str:
        return self.code


class DocumentSequence(models.Model):
    pk = models.CompositePrimaryKey("company_id", "fiscal_period_id", "sequence_code")
    company_id = models.UUIDField()
    fiscal_period_id = models.UUIDField()
    sequence_code = models.TextField()
    prefix = models.TextField(db_default="")
    next_value = models.BigIntegerField(db_default=1)
    padding_length = models.SmallIntegerField(db_default=6)

    class Meta:
        managed = False
        db_table = '"erp"."document_sequences"'

    def __str__(self) -> str:
        return f"{self.sequence_code}:{self.fiscal_period_id}"
