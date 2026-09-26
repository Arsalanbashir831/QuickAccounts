from rest_framework import serializers


class ActivationCreateSerializer(serializers.Serializer):
    device_fingerprint = serializers.CharField(min_length=16, max_length=1000, write_only=True)
    device_public_key = serializers.CharField(max_length=12000, required=False, allow_blank=False)


class RenewalOrderCreateSerializer(serializers.Serializer):
    plan_version_id = serializers.UUIDField()
    provider = serializers.CharField(min_length=1, max_length=100)
