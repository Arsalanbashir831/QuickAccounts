from decimal import Decimal
from typing import Any

from rest_framework import serializers


class PaymentCreateSerializer(serializers.Serializer[Any]):
    payment_no = serializers.CharField(max_length=100)
    direction = serializers.ChoiceField(choices=["receipt", "disbursement"])
    partner_id = serializers.UUIDField(required=False, allow_null=True)
    cash_account_id = serializers.UUIDField()
    payment_date = serializers.DateField()
    currency_code = serializers.RegexField(r"^[A-Za-z]{3}$")
    exchange_rate = serializers.DecimalField(
        max_digits=20, decimal_places=10, min_value=Decimal("0.0000000001"), default=Decimal("1")
    )
    amount = serializers.DecimalField(
        max_digits=20, decimal_places=6, min_value=Decimal("0.000001")
    )
    payment_method = serializers.CharField(max_length=50)
    external_reference = serializers.CharField(
        max_length=255, required=False, allow_blank=True, allow_null=True
    )

    def validate_currency_code(self, value: str) -> str:
        return value.upper()


class PaymentPatchSerializer(PaymentCreateSerializer):
    payment_no = serializers.CharField(max_length=100, required=False)
    direction = serializers.ChoiceField(choices=["receipt", "disbursement"], required=False)
    cash_account_id = serializers.UUIDField(required=False)
    payment_date = serializers.DateField(required=False)
    currency_code = serializers.RegexField(r"^[A-Za-z]{3}$", required=False)
    exchange_rate = serializers.DecimalField(
        max_digits=20, decimal_places=10, min_value=Decimal("0.0000000001"), required=False
    )
    amount = serializers.DecimalField(
        max_digits=20, decimal_places=6, min_value=Decimal("0.000001"), required=False
    )
    payment_method = serializers.CharField(max_length=50, required=False)

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        if not attrs:
            raise serializers.ValidationError("At least one field is required.")
        return attrs


class PaymentAllocationSerializer(serializers.Serializer[Any]):
    document_id = serializers.UUIDField()
    applied_document_amount = serializers.DecimalField(
        max_digits=20, decimal_places=6, min_value=Decimal("0.000001")
    )
    applied_payment_amount = serializers.DecimalField(
        max_digits=20, decimal_places=6, min_value=Decimal("0.000001")
    )
    allocation_exchange_rate = serializers.DecimalField(
        max_digits=20, decimal_places=10, min_value=Decimal("0.0000000001")
    )


class PaymentAllocationsSerializer(serializers.Serializer[Any]):
    allocations = PaymentAllocationSerializer(many=True)

    def validate_allocations(self, value: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if len(value) > 50 or len({a["document_id"] for a in value}) != len(value):
            raise serializers.ValidationError("At most 50 distinct documents are allowed.")
        return value


class PaymentPostSerializer(serializers.Serializer[Any]):
    fiscal_period_id = serializers.UUIDField()
    journal_id = serializers.UUIDField()


class WithholdingComponentSerializer(serializers.Serializer[Any]):
    tax_code_id = serializers.UUIDField()
    taxable_base_amount = serializers.DecimalField(
        max_digits=20, decimal_places=6, min_value=Decimal("0")
    )
    tax_treatment = serializers.ChoiceField(
        choices=["advance", "adjustable", "final", "minimum", "other"]
    )


class PaymentWithholdingSerializer(serializers.Serializer[Any]):
    components = WithholdingComponentSerializer(many=True)

    def validate_components(self, value: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if len(value) > 20 or len({c["tax_code_id"] for c in value}) != len(value):
            raise serializers.ValidationError("At most 20 distinct tax codes are allowed.")
        return value


class PaymentReverseSerializer(PaymentPostSerializer):
    reversal_date = serializers.DateField()
    reason = serializers.CharField(max_length=1000)
