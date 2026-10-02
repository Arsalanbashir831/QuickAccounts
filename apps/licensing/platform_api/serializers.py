from typing import Any

from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers


class LicenseIssueSerializer(serializers.Serializer[dict[str, Any]]):
    tenant_id = serializers.UUIDField()
    plan_version_id = serializers.UUIDField()
    reason = serializers.CharField(min_length=1, max_length=1000)


class LicenseStatusSerializer(serializers.Serializer[dict[str, Any]]):
    reason = serializers.CharField(min_length=1, max_length=1000)


class PlanPublishSerializer(serializers.Serializer[dict[str, Any]]):
    reason = serializers.CharField(min_length=1, max_length=1000)


class PlanFeatureSerializer(serializers.Serializer[dict[str, Any]]):
    feature_code = serializers.CharField(min_length=1, max_length=255)
    is_enabled = serializers.BooleanField(default=True)
    limit_value = serializers.DecimalField(
        max_digits=20, decimal_places=6, min_value=0, required=False, allow_null=True
    )


class PlanVersionCreateSerializer(serializers.Serializer[dict[str, Any]]):
    plan_id = serializers.UUIDField()
    version_number = serializers.IntegerField(min_value=1)
    term_unit = serializers.ChoiceField(choices=["day", "month", "year", "lifetime"])
    term_count = serializers.IntegerField(min_value=1, required=False, allow_null=True)
    max_activations = serializers.IntegerField(min_value=1, default=1)
    permits_offline_use = serializers.BooleanField(default=False)
    price_currency = serializers.CharField(
        min_length=3, max_length=3, required=False, allow_null=True
    )
    price_amount = serializers.DecimalField(
        max_digits=20, decimal_places=6, min_value=0, required=False, allow_null=True
    )
    features = PlanFeatureSerializer(many=True, required=False, default=list)
    reason = serializers.CharField(min_length=1, max_length=1000)

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        term_unit = attrs["term_unit"]
        term_count = attrs.get("term_count")
        if term_unit == "lifetime" and term_count is not None:
            raise serializers.ValidationError(
                {"term_count": "Lifetime plans must not define a term count."}
            )
        if term_unit != "lifetime" and term_count is None:
            raise serializers.ValidationError(
                {"term_count": "A term count is required for fixed-term plans."}
            )
        currency = attrs.get("price_currency")
        attrs["price_currency"] = currency.upper() if currency else None
        codes = [feature["feature_code"] for feature in attrs["features"]]
        if len(codes) != len(set(codes)):
            raise serializers.ValidationError(
                {"features": "Each feature_code may appear only once."}
            )
        return attrs


class TenantProvisionSerializer(serializers.Serializer[dict[str, Any]]):
    name = serializers.CharField(max_length=255)
    company_code = serializers.CharField(max_length=100)
    company_name = serializers.CharField(max_length=255)
    currency = serializers.CharField(min_length=3, max_length=3)
    timezone_name = serializers.CharField(max_length=100, default="UTC")
    business_type = serializers.ChoiceField(
        choices=["retail", "wholesale", "ecommerce", "manufacturing"]
    )
    owner_email = serializers.EmailField()
    owner_password = serializers.CharField(write_only=True, trim_whitespace=False)
    reason = serializers.CharField(min_length=1, max_length=1000)

    def validate_owner_password(self, value: str) -> str:
        try:
            validate_password(value)
        except DjangoValidationError as exc:
            raise serializers.ValidationError(exc.messages) from exc
        return value


class CompanyProvisionSerializer(serializers.Serializer[dict[str, Any]]):
    code = serializers.CharField(max_length=100)
    legal_name = serializers.CharField(max_length=255)
    currency = serializers.CharField(min_length=3, max_length=3)
    timezone_name = serializers.CharField(max_length=100, default="UTC")
    business_type = serializers.ChoiceField(
        choices=["retail", "wholesale", "ecommerce", "manufacturing"]
    )
    owner_user_id = serializers.UUIDField()
    reason = serializers.CharField(min_length=1, max_length=1000)
