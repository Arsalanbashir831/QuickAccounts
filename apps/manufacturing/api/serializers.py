from decimal import Decimal
from typing import Any, cast

from rest_framework import serializers

from apps.inventory.api.serializers import _non_blank


class StrictSerializer(serializers.Serializer[dict[str, Any]]):
    def to_internal_value(self, data: Any) -> dict[str, Any]:
        if not isinstance(data, dict) or set(data) - set(self.fields):
            raise serializers.ValidationError(
                {"non_field_errors": ["Unsupported manufacturing fields."]}
            )
        return cast(dict[str, Any], super().to_internal_value(data))


class BomLineSerializer(StrictSerializer):
    component_item_id = serializers.UUIDField()
    quantity_per_output = serializers.DecimalField(
        max_digits=20, decimal_places=6, min_value=Decimal("0.000001")
    )
    scrap_percent = serializers.DecimalField(
        max_digits=9,
        decimal_places=6,
        min_value=Decimal(0),
        max_value=Decimal(100),
        default=Decimal(0),
    )


class BomSerializer(StrictSerializer):
    output_item_id = serializers.UUIDField()
    revision = serializers.CharField(max_length=100, validators=[_non_blank])
    output_quantity = serializers.DecimalField(
        max_digits=20, decimal_places=6, min_value=Decimal("0.000001")
    )
    effective_from = serializers.DateField(allow_null=True, default=None)
    effective_to = serializers.DateField(allow_null=True, default=None)
    lines = BomLineSerializer(many=True, allow_empty=False, max_length=100)

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        if not attrs:
            raise serializers.ValidationError("At least one planning field is required.")
        return attrs


class OrderSerializer(StrictSerializer):
    production_no = serializers.CharField(max_length=100, validators=[_non_blank])
    bom_id = serializers.UUIDField()
    warehouse_id = serializers.UUIDField()
    planned_quantity = serializers.DecimalField(
        max_digits=20, decimal_places=6, min_value=Decimal("0.000001")
    )
    planned_start = serializers.DateField(allow_null=True, default=None)
    planned_end = serializers.DateField(allow_null=True, default=None)

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        if not attrs:
            raise serializers.ValidationError("At least one planning field is required.")
        return attrs


class EmptySerializer(StrictSerializer):
    pass


class ReasonSerializer(StrictSerializer):
    reason = serializers.CharField(max_length=2000, validators=[_non_blank])


class ListQuerySerializer(serializers.Serializer[dict[str, Any]]):
    cursor = serializers.UUIDField(required=False)
    limit = serializers.IntegerField(default=50, min_value=1, max_value=200)
    output_item_id = serializers.UUIDField(required=False)
    status = serializers.CharField(required=False)


class MaterialScopeSerializer(StrictSerializer):
    requirement_id = serializers.UUIDField()
    lot_id = serializers.UUIDField(allow_null=True, default=None)
    quantity = serializers.DecimalField(
        max_digits=20, decimal_places=6, min_value=Decimal("0.000001")
    )


class ReleaseSerializer(StrictSerializer):
    wip_account_id = serializers.UUIDField()
    materials = MaterialScopeSerializer(
        many=True, allow_empty=False, max_length=100, required=False
    )


class ExecutionPostingSerializer(StrictSerializer):
    posting_date = serializers.DateField()
    fiscal_period_id = serializers.UUIDField()
    journal_id = serializers.UUIDField()


class MaterialIssueSerializer(ExecutionPostingSerializer):
    reservation_ids = serializers.ListField(
        child=serializers.UUIDField(), min_length=1, max_length=100
    )


class OutputSerializer(ExecutionPostingSerializer):
    quantity = serializers.DecimalField(
        max_digits=20, decimal_places=6, min_value=Decimal("0.000001")
    )
    lot_id = serializers.UUIDField(allow_null=True, default=None)


class ExecutionCancelSerializer(ReasonSerializer):
    posting_date = serializers.DateField(required=False)
    fiscal_period_id = serializers.UUIDField(required=False)
    journal_id = serializers.UUIDField(required=False)
