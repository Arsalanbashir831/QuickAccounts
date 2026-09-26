from django.db import models
from django.db.models.expressions import RawSQL


class BusinessPartner(models.Model):
    class PartnerKind(models.TextChoices):
        CUSTOMER = "customer", "Customer"
        SUPPLIER = "supplier", "Supplier"
        BOTH = "both", "Both"
        OTHER = "other", "Other"

    id = models.UUIDField(
        primary_key=True,
        db_default=RawSQL("uuidv7()", []),
        editable=False,
    )
    company_id = models.UUIDField()
    partner_code = models.TextField()
    display_name = models.TextField()
    legal_name = models.TextField(blank=True, null=True)  # noqa: DJ001 -- adopted SQL is nullable
    partner_kind = models.TextField(choices=PartnerKind, db_default="other")
    email = models.TextField(blank=True, null=True)  # noqa: DJ001 -- adopted SQL is nullable
    phone = models.TextField(blank=True, null=True)  # noqa: DJ001 -- adopted SQL is nullable
    is_active = models.BooleanField(db_default=True)
    row_version = models.BigIntegerField(db_default=1)
    created_at = models.DateTimeField(db_default=RawSQL("clock_timestamp()", []), editable=False)
    updated_at = models.DateTimeField(db_default=RawSQL("clock_timestamp()", []), editable=False)

    class Meta:
        managed = False
        db_table = '"erp"."business_partners"'
        unique_together = (("company_id", "partner_code"), ("company_id", "id"))

    def __str__(self) -> str:
        return f"{self.partner_code} — {self.display_name}"


class PartnerAddress(models.Model):
    class AddressKind(models.TextChoices):
        BILLING = "billing", "Billing"
        SHIPPING = "shipping", "Shipping"
        REGISTERED = "registered", "Registered"
        OTHER = "other", "Other"

    id = models.UUIDField(
        primary_key=True,
        db_default=RawSQL("uuidv7()", []),
        editable=False,
    )
    company_id = models.UUIDField()
    partner_id = models.UUIDField()
    address_kind = models.TextField(choices=AddressKind)
    line_1 = models.TextField()
    line_2 = models.TextField(blank=True, null=True)  # noqa: DJ001 -- adopted SQL is nullable
    city = models.TextField(blank=True, null=True)  # noqa: DJ001 -- adopted SQL is nullable
    region = models.TextField(blank=True, null=True)  # noqa: DJ001 -- adopted SQL is nullable
    postal_code = models.TextField(blank=True, null=True)  # noqa: DJ001 -- adopted SQL nullable
    country_code = models.CharField(  # noqa: DJ001 -- adopted SQL is nullable
        max_length=2, blank=True, null=True
    )
    is_default = models.BooleanField(db_default=False)
    row_version = models.BigIntegerField(db_default=1)
    created_at = models.DateTimeField(db_default=RawSQL("clock_timestamp()", []), editable=False)
    updated_at = models.DateTimeField(db_default=RawSQL("clock_timestamp()", []), editable=False)

    class Meta:
        managed = False
        db_table = '"erp"."partner_addresses"'

    def __str__(self) -> str:
        return f"{self.address_kind} — {self.line_1}"


class PartnerTaxRegistration(models.Model):
    id = models.UUIDField(
        primary_key=True,
        db_default=RawSQL("uuidv7()", []),
        editable=False,
    )
    company_id = models.UUIDField()
    partner_id = models.UUIDField()
    jurisdiction_id = models.UUIDField()
    tax_type_id = models.UUIDField()
    registration_number = models.TextField()
    valid_from = models.DateField()
    valid_to = models.DateField(blank=True, null=True)
    is_verified = models.BooleanField(db_default=False)
    is_active = models.BooleanField(db_default=True)
    created_at = models.DateTimeField(db_default=RawSQL("clock_timestamp()", []), editable=False)

    class Meta:
        managed = False
        db_table = '"erp"."partner_tax_registrations"'
        unique_together = (
            (
                "company_id",
                "partner_id",
                "jurisdiction_id",
                "tax_type_id",
                "registration_number",
            ),
            ("company_id", "id"),
        )

    def __str__(self) -> str:
        return self.registration_number
