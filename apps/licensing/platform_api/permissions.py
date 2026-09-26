from rest_framework.permissions import BasePermission
from rest_framework.request import Request


class IsPlatformOperator(BasePermission):
    def has_permission(self, request: Request, view: object) -> bool:
        user = request.user
        return bool(user and user.is_authenticated and user.is_active and user.is_superuser)
