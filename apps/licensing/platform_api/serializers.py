from typing import Any

from rest_framework import serializers


class LicenseIssueSerializer(serializers.Serializer[dict[str, Any]]):
    tenant_id = serializers.UUIDField()
    plan_version_id = serializers.UUIDField()
    reason = serializers.CharField(min_length=1, max_length=1000)


class LicenseStatusSerializer(serializers.Serializer[dict[str, Any]]):
    reason = serializers.CharField(min_length=1, max_length=1000)


class PlanPublishSerializer(serializers.Serializer[dict[str, Any]]):
    reason = serializers.CharField(min_length=1, max_length=1000)
