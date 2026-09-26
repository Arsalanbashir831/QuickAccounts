from django.urls import path

from apps.identity.api.views import CSRFView, MeView, SessionView

urlpatterns = [
    path("csrf", CSRFView.as_view(), name="auth-csrf"),
    path("session", SessionView.as_view(), name="auth-session"),
    path("me", MeView.as_view(), name="auth-me"),
]
