"""Opt-in immediate session revocation check for sensitive endpoints."""

from typing import Any, cast
from uuid import UUID

from rest_framework.permissions import BasePermission
from rest_framework.request import Request
from rest_framework.views import APIView

from apps.identity.refresh_sessions import get_session


class ActiveRefreshSession(BasePermission):
    def has_permission(self, request: Request, view: APIView) -> bool:
        if not request.user or not request.user.is_authenticated or not request.auth:
            return False
        try:
            sid = UUID(cast(Any, request.auth).get("sid", ""))
        except (TypeError, ValueError):
            return False
        session = get_session(sid)
        return bool(session and session.get("user_id") == str(request.user.id))
