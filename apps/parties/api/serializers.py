from rest_framework import serializers


def _non_blank(value: str) -> None:
    if not value.strip():
        raise serializers.ValidationError("Must not be blank.")


class PartnerCreateSerializer(serializers.Serializer):
    partner_code = serializers.CharField(max_length=100, validators=[_non_blank])
    display_name = serializers.CharField(max_length=255, validators=[_non_blank])
    legal_name = serializers.CharField(
        max_length=255, required=False, allow_blank=True, allow_null=True
    )
    partner_kind = serializers.ChoiceField(
        choices=["customer", "supplier", "both", "other"], default="other"
    )
    email = serializers.EmailField(
        max_length=320, required=False, allow_blank=True, allow_null=True
    )
    phone = serializers.CharField(max_length=50, required=False, allow_blank=True, allow_null=True)
    is_active = serializers.BooleanField(default=True)


class PartnerPatchSerializer(serializers.Serializer):
    partner_code = serializers.CharField(max_length=100, required=False, validators=[_non_blank])
    display_name = serializers.CharField(max_length=255, required=False, validators=[_non_blank])
    legal_name = serializers.CharField(
        max_length=255, required=False, allow_blank=True, allow_null=True
    )
    partner_kind = serializers.ChoiceField(
        choices=["customer", "supplier", "both", "other"], required=False
    )
    email = serializers.EmailField(
        max_length=320, required=False, allow_blank=True, allow_null=True
    )
    phone = serializers.CharField(max_length=50, required=False, allow_blank=True, allow_null=True)
    is_active = serializers.BooleanField(required=False)

    def validate(self, attrs: dict[str, object]) -> dict[str, object]:
        if not attrs:
            raise serializers.ValidationError("At least one field must be provided.")
        return attrs


class PartnerAddressCreateSerializer(serializers.Serializer):
    address_kind = serializers.ChoiceField(choices=["billing", "shipping", "registered", "other"])
    line_1 = serializers.CharField(max_length=255, validators=[_non_blank])
    line_2 = serializers.CharField(
        max_length=255, required=False, allow_blank=True, allow_null=True
    )
    city = serializers.CharField(max_length=120, required=False, allow_blank=True, allow_null=True)
    region = serializers.CharField(
        max_length=120, required=False, allow_blank=True, allow_null=True
    )
    postal_code = serializers.CharField(
        max_length=30, required=False, allow_blank=True, allow_null=True
    )
    country_code = serializers.RegexField(r"^[A-Za-z]{2}$", required=False, allow_null=True)
    is_default = serializers.BooleanField(default=False)

    def validate_country_code(self, value: str | None) -> str | None:
        return value.upper() if value is not None else None


class PartnerAddressPatchSerializer(PartnerAddressCreateSerializer):
    address_kind = serializers.ChoiceField(
        choices=["billing", "shipping", "registered", "other"], required=False
    )
    line_1 = serializers.CharField(max_length=255, required=False, validators=[_non_blank])
    is_default = serializers.BooleanField(required=False)

    def validate(self, attrs: dict[str, object]) -> dict[str, object]:
        if not attrs:
            raise serializers.ValidationError("At least one field must be provided.")
        return attrs


class PartnerTaxRegistrationCreateSerializer(serializers.Serializer):
    jurisdiction_id = serializers.UUIDField()
    tax_type_id = serializers.UUIDField()
    registration_number = serializers.CharField(max_length=100, validators=[_non_blank])
    valid_from = serializers.DateField()
    valid_to = serializers.DateField(required=False, allow_null=True)
    is_verified = serializers.BooleanField(default=False)
    is_active = serializers.BooleanField(default=True)

    def validate(self, attrs: dict[str, object]) -> dict[str, object]:
        valid_to = attrs.get("valid_to")
        if valid_to is not None and valid_to <= attrs["valid_from"]:  # type: ignore[operator]
            raise serializers.ValidationError({"valid_to": "Must be later than valid_from."})
        return attrs
