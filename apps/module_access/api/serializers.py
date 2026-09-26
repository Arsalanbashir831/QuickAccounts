from rest_framework import serializers


class ModuleChangeSerializer(serializers.Serializer):
    module_code = serializers.CharField(max_length=100)
    mode = serializers.ChoiceField(choices=["enabled", "read_only", "disabled"])


class ModuleChangeBatchSerializer(serializers.Serializer):
    changes = ModuleChangeSerializer(many=True, min_length=1, max_length=24)
    reason = serializers.CharField(min_length=1, max_length=1000)

    def validate_changes(self, changes: list[dict[str, object]]) -> list[dict[str, object]]:
        codes = [str(change["module_code"]) for change in changes]
        if len(codes) != len(set(codes)):
            raise serializers.ValidationError("Each module may appear only once.")
        return changes
