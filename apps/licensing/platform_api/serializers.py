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
