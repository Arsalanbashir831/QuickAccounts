from typing import Any, cast
from uuid import UUID

from django.conf import settings
from django.middleware.csrf import get_token
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_protect, ensure_csrf_cookie
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.identity import refresh_sessions
from apps.identity.api.serializers import PasswordChangeSerializer, TokenObtainSerializer
from apps.identity.models import User
from apps.identity.permissions import ActiveRefreshSession
from apps.identity.token_services import change_password, login, logout, refresh
from common.api.errors import APIError
from common.api.exceptions import api_exception_handler


def _set_refresh_cookie(response: Response, credential: str) -> None:
    response.set_cookie(
        settings.AUTH_REFRESH_COOKIE_NAME,
        credential,
        max_age=settings.AUTH_REFRESH_SESSION_SECONDS,
        httponly=True,
        secure=settings.AUTH_REFRESH_COOKIE_SECURE,
        samesite=settings.AUTH_REFRESH_COOKIE_SAMESITE,
        path=settings.AUTH_REFRESH_COOKIE_PATH,
    )


def _clear_refresh_cookie(response: Response) -> None:
    response.delete_cookie(
        settings.AUTH_REFRESH_COOKIE_NAME,
        path=settings.AUTH_REFRESH_COOKIE_PATH,
        samesite=settings.AUTH_REFRESH_COOKIE_SAMESITE,
    )


class CSRFView(APIView):
    authentication_classes: list[type] = []
    permission_classes = [AllowAny]

    @method_decorator(ensure_csrf_cookie)
    @extend_schema(responses=OpenApiTypes.OBJECT)
    def get(self, request: Request) -> Response:
        return Response({"csrf_token": get_token(request)})


class LoginView(APIView):
    authentication_classes: list[type] = []
    permission_classes = [AllowAny]

    @extend_schema(request=TokenObtainSerializer, responses=OpenApiTypes.OBJECT)
    def post(self, request: Request) -> Response:
        serializer = TokenObtainSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        payload, credential = login(
            data["email"],
            data["password"],
            request.META.get("REMOTE_ADDR", ""),
            request.META.get("HTTP_USER_AGENT", ""),
        )
        response = Response(payload)
        _set_refresh_cookie(response, credential)
        return response


@method_decorator(csrf_protect, name="dispatch")
class RefreshView(APIView):
    authentication_classes: list[type] = []
    permission_classes = [AllowAny]

    @extend_schema(request=None, responses=OpenApiTypes.OBJECT)
    def post(self, request: Request) -> Response:
        try:
            payload, credential = refresh(request.COOKIES.get(settings.AUTH_REFRESH_COOKIE_NAME))
        except APIError as exc:
            response = api_exception_handler(exc, {"request": request})
            assert response is not None
            if exc.status_code == 401:
                _clear_refresh_cookie(response)
            return response
        response = Response(payload)
        _set_refresh_cookie(response, credential)
        return response


@method_decorator(csrf_protect, name="dispatch")
class LogoutView(APIView):
    authentication_classes: list[type] = []
    permission_classes = [AllowAny]

    @extend_schema(request=None, responses={204: None})
    def post(self, request: Request) -> Response:
        logout(request.COOKIES.get(settings.AUTH_REFRESH_COOKIE_NAME))
        response = Response(status=status.HTTP_204_NO_CONTENT)
        _clear_refresh_cookie(response)
        return response


class LogoutAllView(APIView):
    permission_classes = [IsAuthenticated, ActiveRefreshSession]

    @extend_schema(request=None, responses={204: None})
    def post(self, request: Request) -> Response:
        refresh_sessions.revoke_all_sessions(cast(User, request.user).id)
        response = Response(status=status.HTTP_204_NO_CONTENT)
        _clear_refresh_cookie(response)
        return response


class SessionsView(APIView):
    permission_classes = [IsAuthenticated, ActiveRefreshSession]

    @extend_schema(responses=OpenApiTypes.OBJECT)
    def get(self, request: Request) -> Response:
        sid = cast(Any, request.auth).get("sid") if request.auth else None
        current = UUID(sid) if sid else None
        return Response(
            {"sessions": refresh_sessions.list_sessions(cast(User, request.user).id, current)}
        )


class SessionDetailView(APIView):
    permission_classes = [IsAuthenticated, ActiveRefreshSession]

    @extend_schema(responses={204: None})
    def delete(self, request: Request, session_id: UUID) -> Response:
        session = refresh_sessions.get_session(session_id)
        if not session or session.get("user_id") != str(cast(User, request.user).id):
            raise APIError(code="SESSION_NOT_FOUND", message="Session not found.", status_code=404)
        refresh_sessions.revoke_session(session_id)
        response = Response(status=status.HTTP_204_NO_CONTENT)
        if request.auth and cast(Any, request.auth).get("sid") == str(session_id):
            _clear_refresh_cookie(response)
        return response


class PasswordChangeView(APIView):
    permission_classes = [IsAuthenticated, ActiveRefreshSession]

    @extend_schema(request=PasswordChangeSerializer, responses={204: None})
    def post(self, request: Request) -> Response:
        serializer = PasswordChangeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        change_password(cast(User, request.user), **serializer.validated_data)
        response = Response(status=status.HTTP_204_NO_CONTENT)
        _clear_refresh_cookie(response)
        return response


class MeView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(responses=OpenApiTypes.OBJECT)
    def get(self, request: Request) -> Response:
        user = cast(User, request.user)
        return Response(
            {
                "id": str(user.id),
                "email": user.email,
                "first_name": user.first_name,
                "last_name": user.last_name,
                "auth_revision": user.auth_revision,
            }
        )
