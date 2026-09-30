from decimal import Decimal
from typing import Any, cast

from rest_framework import serializers


def _non_blank(value: str) -> None:
    if not value.strip():
        raise serializers.ValidationError("Must not be blank.")


class PurchaseBillLineSerializer(serializers.Serializer):
    item_id = serializers.UUIDField(required=False, allow_null=True)
    line_account_id = serializers.UUIDField(required=False, allow_null=True)
    description = serializers.CharField(max_length=1000, validators=[_non_blank])
    quantity = serializers.DecimalField(
        max_digits=20, decimal_places=6, min_value=Decimal("0.000001")
    )
    unit_cost = serializers.DecimalField(max_digits=20, decimal_places=6, min_value=Decimal("0"))
    tax_code_id = serializers.UUIDField(required=False, allow_null=True)

    def validate(self, attrs: dict[str, object]) -> dict[str, object]:
        if (attrs.get("item_id") is None) == (attrs.get("line_account_id") is None):
            raise serializers.ValidationError(
                "Exactly one of item_id or line_account_id must be provided."
            )
        return attrs


class PurchaseBillAddressSerializer(serializers.Serializer):
    address_kind = serializers.ChoiceField(choices=["supplier", "ship_from", "bill_to"])
    source_address_id = serializers.UUIDField()
    recipient_name = serializers.CharField(max_length=255, validators=[_non_blank])
    tax_registration_no = serializers.CharField(
        max_length=100, required=False, allow_blank=True, allow_null=True
    )


class PurchaseBillCreateSerializer(serializers.Serializer):
    bill_no = serializers.CharField(max_length=100, validators=[_non_blank])
    supplier_id = serializers.UUIDField()
    bill_date = serializers.DateField()
    tax_point_date = serializers.DateField(required=False)
    tax_jurisdiction_id = serializers.UUIDField(required=False, allow_null=True)
    due_date = serializers.DateField(required=False, allow_null=True)
    currency_code = serializers.RegexField(r"^[A-Za-z]{3}$")
    exchange_rate = serializers.DecimalField(
        max_digits=20, decimal_places=10, min_value=Decimal("0.0000000001"), default=Decimal("1")
    )
    addresses = PurchaseBillAddressSerializer(many=True, required=False, default=list)
    lines = PurchaseBillLineSerializer(many=True, min_length=1, max_length=50)

    def validate_currency_code(self, value: str) -> str:
        return value.upper()

    def validate(self, attrs: dict[str, object]) -> dict[str, object]:
        attrs.setdefault("tax_point_date", attrs["bill_date"])
        due_date = attrs.get("due_date")
        if due_date is not None and due_date < attrs["bill_date"]:  # type: ignore[operator]
            raise serializers.ValidationError({"due_date": "Must not precede bill_date."})
        addresses = cast(list[dict[str, Any]], attrs.get("addresses", []))
        kinds = [address["address_kind"] for address in addresses]
        if len(kinds) != len(set(kinds)):
            raise serializers.ValidationError({"addresses": "Address kinds must be unique."})
        return attrs


class PurchaseBillPatchSerializer(serializers.Serializer):
    bill_no = serializers.CharField(max_length=100, required=False, validators=[_non_blank])
    supplier_id = serializers.UUIDField(required=False)
    bill_date = serializers.DateField(required=False)
    tax_point_date = serializers.DateField(required=False)
    tax_jurisdiction_id = serializers.UUIDField(required=False, allow_null=True)
    due_date = serializers.DateField(required=False, allow_null=True)
    currency_code = serializers.RegexField(r"^[A-Za-z]{3}$", required=False)
    exchange_rate = serializers.DecimalField(
        max_digits=20, decimal_places=10, min_value=Decimal("0.0000000001"), required=False
    )
    addresses = PurchaseBillAddressSerializer(many=True, required=False)
    lines = PurchaseBillLineSerializer(many=True, min_length=1, max_length=50, required=False)

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


class SerialReceiptSelectionSerializer(serializers.Serializer):
    purchase_bill_line_id = serializers.UUIDField()
    serial_numbers = serializers.ListField(
        child=serializers.CharField(max_length=100, validators=[_non_blank]),
        min_length=1, max_length=2000,
    )


class SerialReturnSelectionSerializer(serializers.Serializer):
    purchase_bill_line_id = serializers.UUIDField()
    serial_ids = serializers.ListField(
        child=serializers.UUIDField(), min_length=1, max_length=2000,
    )


class PurchaseBillPostSerializer(serializers.Serializer):
    fiscal_period_id = serializers.UUIDField()
    journal_id = serializers.UUIDField()
    warehouse_id = serializers.UUIDField(required=False, allow_null=True)
    serial_receipts = SerialReceiptSelectionSerializer(many=True, required=False, max_length=50)
    serial_returns = SerialReturnSelectionSerializer(many=True, required=False, max_length=50)


class SupplierCreditCreateSerializer(serializers.Serializer):
    bill_no = serializers.CharField(max_length=100, validators=[_non_blank])
    bill_date = serializers.DateField()
    source_line_ids = serializers.ListField(
        child=serializers.UUIDField(), min_length=1, max_length=50
    )
    partial_quantities = serializers.ListField(
        child=serializers.DecimalField(
            max_digits=20, decimal_places=6, min_value=Decimal("0.000001")
        ),
        min_length=1,
        max_length=50,
        required=False,
    )

    def validate_source_line_ids(self, value: list[object]) -> list[object]:
        if len(value) != len(set(value)):
            raise serializers.ValidationError("Each source line may be credited only once.")
        return value

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        if "partial_quantities" in attrs and len(attrs["partial_quantities"]) != len(
            attrs["source_line_ids"]
        ):
            raise serializers.ValidationError(
                "partial_quantities must match source_line_ids order."
            )
        return attrs
