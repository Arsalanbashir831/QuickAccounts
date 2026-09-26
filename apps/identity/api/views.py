from typing import cast

from django.contrib.auth import authenticate, login, logout
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

from apps.identity.api.serializers import SessionSerializer
from apps.identity.models import User
from common.api.errors import APIError


class CSRFView(APIView):
    permission_classes = [AllowAny]

    @extend_schema(responses=OpenApiTypes.OBJECT, operation_id="auth_csrf")
    @ensure_csrf_cookie
    def get(self, request: Request) -> Response:
        return Response({"csrf_token": get_token(request)})


@method_decorator(csrf_protect, name="dispatch")
class SessionView(APIView):
    permission_classes = [AllowAny]

    @extend_schema(
        request=SessionSerializer,
        responses=OpenApiTypes.OBJECT,
        operation_id="auth_session_create",
    )
    def post(self, request: Request) -> Response:
        serializer = SessionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = authenticate(
            request,
            username=serializer.validated_data["email"].lower(),
            password=serializer.validated_data["password"],
        )
        if user is None or not user.is_active:
            raise APIError(
                code="INVALID_CREDENTIALS",
                message="The email or password is incorrect.",
                status_code=status.HTTP_401_UNAUTHORIZED,
            )
        login(request, user)
        return Response({"user_id": str(user.id), "email": user.email})

    @extend_schema(responses={204: None}, operation_id="auth_session_delete")
    def delete(self, request: Request) -> Response:
        logout(request)
        return Response(status=status.HTTP_204_NO_CONTENT)


class MeView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(responses=OpenApiTypes.OBJECT, operation_id="auth_me")
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
