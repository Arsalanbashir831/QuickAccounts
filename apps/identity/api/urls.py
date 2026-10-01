from django.urls import path

from apps.identity.api.views import (
    CSRFView,
    LoginView,
    LogoutAllView,
    LogoutView,
    MeView,
    PasswordChangeView,
    RefreshView,
    SessionDetailView,
    SessionsView,
)

urlpatterns = [
    path("csrf/", CSRFView.as_view(), name="auth-csrf"),
    path("login/", LoginView.as_view(), name="auth-login"),
    path("refresh/", RefreshView.as_view(), name="auth-refresh"),
    path("logout/", LogoutView.as_view(), name="auth-logout"),
    path("logout-all/", LogoutAllView.as_view(), name="auth-logout-all"),
    path("sessions/", SessionsView.as_view(), name="auth-sessions"),
    path("sessions/<uuid:session_id>/", SessionDetailView.as_view(), name="auth-session-detail"),
    path("password/change/", PasswordChangeView.as_view(), name="auth-password-change"),
    path("me/", MeView.as_view(), name="auth-me"),
    path("token", LoginView.as_view(), name="auth-token-legacy"),
    path("token/refresh", RefreshView.as_view(), name="auth-token-refresh-legacy"),
    path("token/revoke", LogoutView.as_view(), name="auth-token-revoke-legacy"),
    path("me", MeView.as_view(), name="auth-me-legacy"),
]
