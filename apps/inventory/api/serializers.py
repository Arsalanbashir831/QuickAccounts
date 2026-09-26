from rest_framework import serializers


def _non_blank(value: str) -> None:
    if not value.strip():
        raise serializers.ValidationError("Must not be blank.")


class ItemCreateSerializer(serializers.Serializer):
    sku = serializers.CharField(max_length=100, validators=[_non_blank])
    name = serializers.CharField(max_length=255, validators=[_non_blank])
    item_kind = serializers.ChoiceField(choices=["stock", "service", "non_stock"])
    base_uom_id = serializers.UUIDField()
    track_lots = serializers.BooleanField(default=False)
    track_serials = serializers.BooleanField(default=False)
    is_active = serializers.BooleanField(default=True)

    def validate(self, attrs: dict[str, object]) -> dict[str, object]:
        if attrs["track_lots"] and attrs["track_serials"]:
            raise serializers.ValidationError(
                "An item cannot track lots and serial numbers at the same time."
            )
        if attrs["item_kind"] != "stock" and (attrs["track_lots"] or attrs["track_serials"]):
            raise serializers.ValidationError("Only stock items can track lots or serial numbers.")
        return attrs


class ItemPatchSerializer(serializers.Serializer):
    sku = serializers.CharField(max_length=100, required=False, validators=[_non_blank])
    name = serializers.CharField(max_length=255, required=False, validators=[_non_blank])
    item_kind = serializers.ChoiceField(choices=["stock", "service", "non_stock"], required=False)
    base_uom_id = serializers.UUIDField(required=False)
    track_lots = serializers.BooleanField(required=False)
    track_serials = serializers.BooleanField(required=False)
    is_active = serializers.BooleanField(required=False)

    def validate(self, attrs: dict[str, object]) -> dict[str, object]:
        if not attrs:
            raise serializers.ValidationError("At least one field must be provided.")
        if attrs.get("track_lots") and attrs.get("track_serials"):
            raise serializers.ValidationError(
                "An item cannot track lots and serial numbers at the same time."
            )
        return attrs


class ItemAccountingProfileSerializer(serializers.Serializer):
    inventory_account_id = serializers.UUIDField(allow_null=True)
    revenue_account_id = serializers.UUIDField(allow_null=True)
    cogs_account_id = serializers.UUIDField(allow_null=True)
    purchase_account_id = serializers.UUIDField(allow_null=True)
