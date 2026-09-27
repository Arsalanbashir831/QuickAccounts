from decimal import Decimal
from typing import Any, cast

from rest_framework import serializers


def _non_blank(value: str) -> None:
    if not value.strip():
        raise serializers.ValidationError("Must not be blank.")


class SalesInvoiceLineSerializer(serializers.Serializer):
    item_id = serializers.UUIDField(required=False, allow_null=True)
    line_account_id = serializers.UUIDField(required=False, allow_null=True)
    description = serializers.CharField(max_length=1000, validators=[_non_blank])
    quantity = serializers.DecimalField(
        max_digits=20, decimal_places=6, min_value=Decimal("0.000001")
    )
    unit_price = serializers.DecimalField(max_digits=20, decimal_places=6, min_value=Decimal("0"))
    discount_amount = serializers.DecimalField(
        max_digits=20, decimal_places=6, min_value=Decimal("0"), default=Decimal("0")
    )
    tax_code_id = serializers.UUIDField(required=False, allow_null=True)

    def validate(self, attrs: dict[str, object]) -> dict[str, object]:
        if (attrs.get("item_id") is None) == (attrs.get("line_account_id") is None):
            raise serializers.ValidationError(
                "Exactly one of item_id or line_account_id must be provided."
            )
        return attrs


class SalesInvoiceAddressSerializer(serializers.Serializer):
    address_kind = serializers.ChoiceField(choices=["billing", "shipping"])
    source_address_id = serializers.UUIDField()
    recipient_name = serializers.CharField(max_length=255, validators=[_non_blank])
    tax_registration_no = serializers.CharField(
        max_length=100, required=False, allow_blank=True, allow_null=True
    )


class SalesInvoiceCreateSerializer(serializers.Serializer):
    invoice_no = serializers.CharField(max_length=100, validators=[_non_blank])
    partner_id = serializers.UUIDField()
    issue_date = serializers.DateField()
    tax_point_date = serializers.DateField(required=False)
    tax_jurisdiction_id = serializers.UUIDField(required=False, allow_null=True)
    due_date = serializers.DateField(required=False, allow_null=True)
    currency_code = serializers.RegexField(r"^[A-Za-z]{3}$")
    exchange_rate = serializers.DecimalField(
        max_digits=20, decimal_places=10, min_value=Decimal("0.0000000001"), default=Decimal("1")
    )
    addresses = SalesInvoiceAddressSerializer(many=True, required=False, default=list)
    lines = SalesInvoiceLineSerializer(many=True, min_length=1, max_length=50)

    def validate_currency_code(self, value: str) -> str:
        return value.upper()

    def validate(self, attrs: dict[str, object]) -> dict[str, object]:
        attrs.setdefault("tax_point_date", attrs["issue_date"])
        due_date = attrs.get("due_date")
        if due_date is not None and due_date < attrs["issue_date"]:  # type: ignore[operator]
            raise serializers.ValidationError({"due_date": "Must not precede issue_date."})
        addresses = cast(list[dict[str, Any]], attrs.get("addresses", []))
        kinds = [address["address_kind"] for address in addresses]
        if len(kinds) != len(set(kinds)):
            raise serializers.ValidationError({"addresses": "Address kinds must be unique."})
        return attrs


class SalesInvoicePatchSerializer(serializers.Serializer):
    invoice_no = serializers.CharField(max_length=100, required=False, validators=[_non_blank])
    partner_id = serializers.UUIDField(required=False)
    issue_date = serializers.DateField(required=False)
    tax_point_date = serializers.DateField(required=False)
    tax_jurisdiction_id = serializers.UUIDField(required=False, allow_null=True)
    due_date = serializers.DateField(required=False, allow_null=True)
    currency_code = serializers.RegexField(r"^[A-Za-z]{3}$", required=False)
    exchange_rate = serializers.DecimalField(
        max_digits=20, decimal_places=10, min_value=Decimal("0.0000000001"), required=False
    )
    addresses = SalesInvoiceAddressSerializer(many=True, required=False)
    lines = SalesInvoiceLineSerializer(many=True, min_length=1, max_length=50, required=False)

    def validate_currency_code(self, value: str) -> str:
        return value.upper()

    def validate(self, attrs: dict[str, object]) -> dict[str, object]:
        if not attrs:
            raise serializers.ValidationError("At least one field must be provided.")
        if "addresses" in attrs:
            addresses = cast(list[dict[str, Any]], attrs["addresses"])
            kinds = [address["address_kind"] for address in addresses]
            if len(kinds) != len(set(kinds)):
                raise serializers.ValidationError({"addresses": "Address kinds must be unique."})
        return attrs


class SalesInvoicePostSerializer(serializers.Serializer):
    fiscal_period_id = serializers.UUIDField()
    journal_id = serializers.UUIDField()
    warehouse_id = serializers.UUIDField(required=False, allow_null=True)


class SalesCreditNoteCreateSerializer(serializers.Serializer):
    invoice_no = serializers.CharField(max_length=100, validators=[_non_blank])
    issue_date = serializers.DateField()
    source_line_ids = serializers.ListField(
        child=serializers.UUIDField(), min_length=1, max_length=50
    )

    def validate_source_line_ids(self, value: list[object]) -> list[object]:
        if len(value) != len(set(value)):
            raise serializers.ValidationError("Each source line may be credited only once.")
        return value
