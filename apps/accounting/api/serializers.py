from rest_framework import serializers


class AccountSerializer(serializers.Serializer):
    parent_account_id = serializers.UUIDField(required=False, allow_null=True)
    code = serializers.CharField(max_length=100)
    name = serializers.CharField(max_length=255)
    account_type = serializers.ChoiceField(
        choices=[
            "asset",
            "liability",
            "equity",
            "revenue",
            "expense",
            "cost_of_sales",
            "receivable",
            "payable",
        ]
    )
    normal_balance = serializers.ChoiceField(choices=["debit", "credit"])
    is_control_account = serializers.BooleanField(default=False)
    allow_posting = serializers.BooleanField(default=True)
    is_active = serializers.BooleanField(default=True)


class AccountPatchSerializer(serializers.Serializer):
    parent_account_id = serializers.UUIDField(required=False, allow_null=True)
    name = serializers.CharField(max_length=255, required=False)
    allow_posting = serializers.BooleanField(required=False)
    is_active = serializers.BooleanField(required=False)


class JournalSerializer(serializers.Serializer):
    code = serializers.CharField(max_length=100)
    name = serializers.CharField(max_length=255)
    journal_type = serializers.ChoiceField(
        choices=["general", "sales", "purchase", "cash", "bank", "inventory", "manufacturing"]
    )
    is_active = serializers.BooleanField(default=True)


class PeriodSerializer(serializers.Serializer):
    code = serializers.CharField(max_length=100)
    starts_on = serializers.DateField()
    ends_on = serializers.DateField()

    def validate(self, attrs: dict[str, object]) -> dict[str, object]:
        if attrs["starts_on"] > attrs["ends_on"]:  # type: ignore[operator]
            raise serializers.ValidationError({"ends_on": "Must not precede starts_on."})
        return attrs


class PeriodCommandSerializer(serializers.Serializer):
    reason = serializers.CharField(min_length=1, max_length=1000)


class DimensionTypeSerializer(serializers.Serializer):
    code = serializers.CharField(max_length=100)
    name = serializers.CharField(max_length=255)
    is_required = serializers.BooleanField(default=False)
    is_active = serializers.BooleanField(default=True)


class DimensionValueSerializer(serializers.Serializer):
    dimension_type_id = serializers.UUIDField()
    code = serializers.CharField(max_length=100)
    name = serializers.CharField(max_length=255)
    is_active = serializers.BooleanField(default=True)


class PostingRuleSerializer(serializers.Serializer):
    event_code = serializers.CharField(max_length=100)
    role_code = serializers.CharField(max_length=100)
    account_id = serializers.UUIDField()


class DimensionAssignmentSerializer(serializers.Serializer):
    dimension_type_id = serializers.UUIDField()
    dimension_value_id = serializers.UUIDField()


class JournalLineSerializer(serializers.Serializer):
    account_id = serializers.UUIDField()
    business_partner_id = serializers.UUIDField(required=False, allow_null=True)
    sales_channel_id = serializers.UUIDField(required=False, allow_null=True)
    description = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    transaction_currency = serializers.CharField(min_length=3, max_length=3)
    exchange_rate = serializers.DecimalField(max_digits=20, decimal_places=10, min_value=0)
    transaction_debit = serializers.DecimalField(
        max_digits=20, decimal_places=6, min_value=0, default=0
    )
    transaction_credit = serializers.DecimalField(
        max_digits=20, decimal_places=6, min_value=0, default=0
    )
    debit_amount = serializers.DecimalField(max_digits=20, decimal_places=6, min_value=0)
    credit_amount = serializers.DecimalField(max_digits=20, decimal_places=6, min_value=0)
    dimensions = DimensionAssignmentSerializer(many=True, max_length=50, required=False)

    def validate(self, attrs: dict[str, object]) -> dict[str, object]:
        for debit, credit in (
            (attrs["transaction_debit"], attrs["transaction_credit"]),
            (attrs["debit_amount"], attrs["credit_amount"]),
        ):
            if (debit > 0) == (credit > 0):  # type: ignore[operator]
                raise serializers.ValidationError(
                    "Exactly one debit or credit amount must be greater than zero."
                )
        return attrs


class JournalEntryCreateSerializer(serializers.Serializer):
    journal_id = serializers.UUIDField()
    fiscal_period_id = serializers.UUIDField()
    entry_date = serializers.DateField()
    description = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    lines = JournalLineSerializer(many=True, min_length=2, max_length=500)


class JournalEntryPatchSerializer(serializers.Serializer):
    fiscal_period_id = serializers.UUIDField(required=False)
    entry_date = serializers.DateField(required=False)
    description = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    lines = JournalLineSerializer(many=True, min_length=2, max_length=500, required=False)


class ReversalSerializer(serializers.Serializer):
    fiscal_period_id = serializers.UUIDField()
    entry_date = serializers.DateField()
    reason = serializers.CharField(min_length=1, max_length=1000)
