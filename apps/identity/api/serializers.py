from rest_framework import serializers


class TokenObtainSerializer(serializers.Serializer):
    email = serializers.EmailField(max_length=254)
    password = serializers.CharField(max_length=128, trim_whitespace=False, write_only=True)


class PasswordChangeSerializer(serializers.Serializer):
    old_password = serializers.CharField(trim_whitespace=False, write_only=True)
    new_password = serializers.CharField(trim_whitespace=False, write_only=True)


class AccessTokenResponseSerializer(serializers.Serializer):
    access = serializers.CharField(read_only=True)
    token_type = serializers.CharField(read_only=True)
    expires_in = serializers.IntegerField(read_only=True)


class CSRFResponseSerializer(serializers.Serializer):
    csrf_token = serializers.CharField(read_only=True)


class ActiveSessionSerializer(serializers.Serializer):
    session_id = serializers.UUIDField(read_only=True)
    created_at = serializers.DateTimeField(read_only=True)
    last_used_at = serializers.DateTimeField(read_only=True)
    user_agent = serializers.CharField(read_only=True)
    is_current = serializers.BooleanField(read_only=True)


class SessionListSerializer(serializers.Serializer):
    sessions = ActiveSessionSerializer(many=True, read_only=True)


class MeSerializer(serializers.Serializer):
    id = serializers.UUIDField(read_only=True)
    email = serializers.EmailField(read_only=True)
    first_name = serializers.CharField(read_only=True)
    last_name = serializers.CharField(read_only=True)
    auth_revision = serializers.IntegerField(read_only=True)
